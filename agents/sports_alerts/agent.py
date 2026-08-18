"""Sports score watcher: polls ESPN scoreboards for favorited teams. When a game
goes final it does two INDEPENDENT, fully generic things — no command-center
code knows anything about sports:

1. Emits a ``game.final`` Signal — the reusable FACT (renders in ambient voice
   context so "did Purdue win?" answers with no tool call; a future automation
   could react to it). Producer pattern mirrors calendar_alerts.
2. Posts each following household member an informational final-score card via
   the SDK's generic ``JarvisInbox`` (command-center's existing
   ``/api/v0/node/inbox-item`` path — per-user targeting + push). This is the
   SAME path any package uses to raise a card; it needs zero core changes.

Dedup: the signal re-emits only on a CORRECTED final (``_emitted_finals``,
event_id -> score). Cards are one-shot per (game, fan), deduped DURABLY via a
per-game marker in the node's command-data store (``_store_*_card``) so a
same-day node restart does not re-notify fans — independent of the attention
broker (off by default). ``_carded_fans`` is an in-memory fast path over that
marker; a card's ``dedupe_key`` is a belt-and-suspenders hint the broker honors
when a household has enabled it.
"""

import asyncio
from datetime import datetime, timedelta, timezone
from typing import Any, Dict, List
from zoneinfo import ZoneInfo

from jarvis_command_sdk import (
    AgentSchedule,
    IJarvisAgent,
    IJarvisSecret,
    JarvisInbox,
    JarvisSignals,
    JarvisStorage,
)

try:
    from jarvis_log_client import JarvisLogger
except ImportError:
    import logging

    class JarvisLogger:  # type: ignore[no-redef]
        def __init__(self, **kwargs):
            self._log = logging.getLogger(kwargs.get("service", __name__))

        def info(self, msg, **kw):
            self._log.info(msg)

        def warning(self, msg, **kw):
            self._log.warning(msg)

        def error(self, msg, **kw):
            self._log.error(msg)

        def debug(self, msg, **kw):
            self._log.debug(msg)


try:
    from sports_shared.espn_sports_service import ESPNSportsService, League
    from sports_shared.favorites import (
        fans_for_game,
        league_label,
        load_favorites,
        resolve_favorites,
    )
except ImportError:
    ESPNSportsService = None
    League = None
    fans_for_game = None
    league_label = None
    load_favorites = None
    resolve_favorites = None

logger = JarvisLogger(service="agent.sports_alerts")

SIGNAL_KIND = "game.final"
SOURCE_AGENT = "sports_alerts"
CARD_SOURCE = "sports_alerts"  # command_name passed to JarvisInbox (source attribution)
CARD_CATEGORY = "sports"
# Durable per-game "who's been carded" markers live in the node's own command-data
# store, so a card is at-most-once across restarts WITHOUT depending on the
# attention broker (which is off by default per household). Markers auto-expire.
CARD_STORE = "sports_alerts_cards"
CARD_MARKER_TTL = timedelta(hours=48)

# Signal lives long enough for evening "did Purdue win?" ambient answers, then
# expires (a NULL ttl would accumulate stale scores forever — CC hard-deletes
# expired rows on a 30-min sweep).
SIGNAL_TTL_SECONDS = 6 * 3600

# Adaptive cadence: the schedule property is re-read every scheduler tick, so
# run() steers its own interval (reminder_agent pattern). Scoreboard calls are
# ~24 KB gzipped, but the Pi Zero pays for the JSON parse — stay slow unless a
# favorited team is actually playing.
IDLE_INTERVAL_SECONDS = 1800
PREGAME_INTERVAL_SECONDS = 300
LIVE_INTERVAL_SECONDS = 120
PREGAME_WINDOW = timedelta(hours=2)

# Drop dedup/tracking entries this long after last touch so the maps stay
# bounded; far outside any real game window so pruning can't cause a re-emit.
PRUNE_AFTER = timedelta(hours=36)

# ESPN groups scoreboards by US/Eastern calendar day; a game tipping at 11pm ET
# finishes on the next UTC (and maybe ET) day but stays on its start date's
# board, so unresolved games keep their discovery date polled.
_EASTERN = ZoneInfo("America/New_York")


class SportsAlertsAgent(IJarvisAgent):
    """Watches favorited teams' games and signals final scores."""

    def __init__(self) -> None:
        # No I/O here: package validate + install pre-flight instantiate this
        # class in a bare subprocess.
        self._espn = None
        self._interval = IDLE_INTERVAL_SECONDS
        # event_id -> (home_score, away_score) of the emitted final signal
        self._emitted_finals: Dict[str, tuple] = {}
        # "event_id:user_id" keys already carded (one card per fan per game)
        self._carded_fans: set[str] = set()
        # event_id -> ESPN date (YYYYMMDD) the unresolved game was found under
        self._tracked_dates: Dict[str, str] = {}
        # event_id -> last-touched (emitted or tracked), for pruning
        self._touched_at: Dict[str, datetime] = {}

    # ── IJarvisAgent contract ─────────────────────────────────────────────

    @property
    def name(self) -> str:
        return "sports_alerts"

    @property
    def description(self) -> str:
        return (
            "Watches favorited teams' games (Node Settings -> Stored Data -> "
            "Sports Scores) and signals final scores to Jarvis."
        )

    @property
    def schedule(self) -> AgentSchedule:
        return AgentSchedule(interval_seconds=self._interval, run_on_startup=True)

    @property
    def required_secrets(self) -> List[IJarvisSecret]:
        return []

    @property
    def include_in_context(self) -> bool:
        return False

    def get_context_data(self) -> Dict[str, Any]:
        return {}

    async def run(self) -> None:
        # Never raise: 3 consecutive raises persistently auto-disable the agent.
        try:
            await self._poll()
        except Exception as e:  # noqa: BLE001
            logger.error("sports_alerts poll failed", error=str(e))

    # ── Polling ───────────────────────────────────────────────────────────

    def _service(self):
        if self._espn is None and ESPNSportsService is not None:
            self._espn = ESPNSportsService()
        return self._espn

    @staticmethod
    def _now() -> datetime:
        return datetime.now(timezone.utc)

    @staticmethod
    def _today_espn_date() -> str:
        return datetime.now(_EASTERN).strftime("%Y%m%d")

    def _dates_to_poll(self) -> List[str]:
        dates = {self._today_espn_date()}
        dates.update(self._tracked_dates.values())
        return sorted(dates)

    async def _poll(self) -> None:
        if ESPNSportsService is None or load_favorites is None:
            return

        favorites = await asyncio.to_thread(load_favorites)
        resolved = resolve_favorites(favorites) if favorites else []
        if not resolved:
            self._interval = IDLE_INTERVAL_SECONDS
            self._prune()
            return

        leagues = sorted({rf.favorite.league for rf in resolved})
        dates = self._dates_to_poll()
        service = self._service()

        next_interval = IDLE_INTERVAL_SECONDS
        seen_event_ids: set = set()
        for league_value in leagues:
            league = League(league_value)
            for espn_date in dates:
                try:
                    games = await asyncio.to_thread(
                        service.get_scores, league, espn_date
                    )
                except Exception as e:  # noqa: BLE001
                    logger.warning(
                        "scoreboard fetch failed",
                        league=league_value,
                        date=espn_date,
                        error=str(e),
                    )
                    continue
                for game in games:
                    if game.id in seen_event_ids:
                        continue  # same game can appear under two polled dates
                    names, fans = fans_for_game(resolved, game)
                    if not fans:
                        continue
                    seen_event_ids.add(game.id)
                    interval = await self._process_game(game, espn_date, names, fans)
                    next_interval = min(next_interval, interval)

        self._interval = next_interval
        self._prune()

    async def _process_game(
        self, game, espn_date: str, favorite_names: List[str], fan_user_ids: List[int]
    ) -> int:
        """Handle one favorited game; returns the poll interval it wants."""
        if self._is_final(game):
            # Touch so a still-on-board final isn't pruned out from under an
            # active date (pruning would harmlessly re-emit the signal; the card
            # stays deduped durably regardless).
            self._touched_at[game.id] = self._now()
            await self._handle_final(game, favorite_names, fan_user_ids)
            self._tracked_dates.pop(game.id, None)
            return IDLE_INTERVAL_SECONDS

        if game.state == "post":
            # Postponed/suspended (not final): do NOT keep tracking it, or its
            # date would be polled forever. If it's rescheduled it reappears on a
            # polled date (today is always polled); its stale tracking ages out.
            self._tracked_dates.pop(game.id, None)
            return IDLE_INTERVAL_SECONDS

        # Pre-game or in-progress: keep it (and its date) alive for polling.
        self._touched_at[game.id] = self._now()
        self._tracked_dates[game.id] = espn_date

        if game.state == "in":
            return LIVE_INTERVAL_SECONDS
        # Pre-game (or unknown state): speed up close to start; if the start
        # time is unknown, stay warm — a favorite plays today.
        start = game.start_time
        if start is None:
            return PREGAME_INTERVAL_SECONDS
        now = datetime.now(start.tzinfo) if start.tzinfo else datetime.now()
        if start - now <= PREGAME_WINDOW:
            return PREGAME_INTERVAL_SECONDS
        return IDLE_INTERVAL_SECONDS

    @staticmethod
    def _is_final(game) -> bool:
        # completed is the reliable "over with a result" bit (postponed games
        # are state="post" with completed=False); the substring check covers
        # payloads where completed is missing.
        return bool(game.completed) or "FINAL" in (game.status or "").upper()

    # ── Final handling: signal (fact) + cards (notification) ───────────────

    async def _handle_final(
        self, game, favorite_names: List[str], fan_user_ids: List[int]
    ) -> None:
        if game.home_score is None or game.away_score is None:
            return  # final without scores — corrupt payload, retry next poll
        await self._emit_signal(game)
        await self._post_cards(game, favorite_names, fan_user_ids)

    async def _emit_signal(self, game) -> None:
        """Emit the household-wide game.final FACT (ambient / future automations).

        The signal carries only the game itself — WHO to notify is a delivery
        concern handled by the card path, not part of the fact.
        """
        score = (game.home_score, game.away_score)
        if self._emitted_finals.get(game.id) == score:
            return  # unchanged; a corrected final (score change) re-emits

        league_value = game.league.value
        winner = self._winner(game)
        facts = {
            "event_id": str(game.id),
            "league": league_value,
            "home_team": game.home_team,
            "away_team": game.away_team,
            "home_score": game.home_score,
            "away_score": game.away_score,
            "winner": winner,
        }
        try:
            # emit() POSTs to command-center synchronously — offload so a slow/
            # unreachable CC (esp. the restart re-emit burst) can't stall the
            # shared agent loop and starve sibling agents.
            tag = await asyncio.to_thread(
                lambda: JarvisSignals(SOURCE_AGENT).emit(
                    kind=SIGNAL_KIND,
                    source_key=f"game:{league_value}:{game.id}",
                    subject=str(game.id),
                    summary=self._summary(game, winner),
                    facts=facts,
                    ttl_seconds=SIGNAL_TTL_SECONDS,
                    cacheable=False,
                )
            )
        except Exception as e:  # noqa: BLE001
            logger.error("game.final emit raised", event_id=game.id, error=str(e))
            return
        if tag in ("ok", "no_backend"):
            # Record only on accept so a transient http_error retries next poll
            # ("no_backend" counts as success so tests/dev without a backend
            # don't re-emit forever — same rule as calendar_alerts).
            self._emitted_finals[game.id] = score
            self._touched_at[game.id] = self._now()
        else:
            logger.warning("game.final emit not accepted", event_id=game.id, tag=tag)

    async def _post_cards(
        self, game, favorite_names: List[str], fan_user_ids: List[int]
    ) -> None:
        """Post each following member an informational final-score card ONCE.

        Generic delivery via JarvisInbox — the same node->card path any package
        uses, so command-center needs no sports-specific code. Dedup is durable:
        a per-game marker in the node's command-data store survives restarts, so
        a same-day node restart (common on a Pi Zero) does NOT re-notify fans —
        this does not depend on the attention broker, which is off by default.
        The in-memory ``_carded_fans`` set is just a fast path over that marker;
        a fan whose post failed stays unmarked and retries next poll.

        NOTE: cards are one-shot per game. A later ESPN score CORRECTION re-emits
        the signal (so ambient "what was the score?" updates) but deliberately
        does NOT re-push a card — a duplicate notification for a stat fix is worse
        than a slightly stale card.
        """
        if not fan_user_ids:
            return
        winner = self._winner(game)
        title, summary, body = self._card_text(game, winner, favorite_names)
        for user_id in fan_user_ids:
            key = f"{game.id}:{user_id}"
            if key in self._carded_fans:
                continue
            try:
                # Durable-check + post + durable-record all run in one worker
                # thread (SQLCipher reads and the HTTP post are both blocking).
                carded = await asyncio.to_thread(
                    self._card_one_fan, game.id, user_id, title, summary, body
                )
            except Exception as e:  # noqa: BLE001
                logger.error("game.final card raised", event_id=game.id, error=str(e))
                continue
            if carded:
                self._carded_fans.add(key)

    def _card_one_fan(
        self, game_id: str, user_id: int, title: str, summary: str, body: str
    ) -> bool:
        """Card one fan unless already carded (durable). Runs in a worker thread."""
        if self._store_has_card(game_id, user_id):
            return True  # carded in a prior process — survives restart
        tag = JarvisInbox(CARD_SOURCE).post(
            title=title,
            summary=summary,
            body=body,
            category=CARD_CATEGORY,
            user_id=user_id,
            target_type="user",
            create_push_notification=True,
            metadata={"dedupe_key": f"gamecard:{game_id}:{user_id}"},
        )
        if tag in ("ok", "no_backend"):
            self._store_add_card(game_id, user_id)
            return True
        logger.warning(
            "game.final card not accepted", event_id=game_id, user_id=user_id, tag=tag
        )
        return False

    @staticmethod
    def _store_has_card(game_id: str, user_id: int) -> bool:
        try:
            rec = JarvisStorage(CARD_STORE).get(f"gamecard:{game_id}")
            return bool(rec) and user_id in (rec.get("fans") or [])
        except Exception:  # noqa: BLE001 — storage down: fall back to in-memory only
            return False

    @staticmethod
    def _store_add_card(game_id: str, user_id: int) -> None:
        try:
            store = JarvisStorage(CARD_STORE)
            existing = store.get(f"gamecard:{game_id}")
            fans = set(existing.get("fans") or []) if existing else set()
            fans.add(user_id)
            store.save(
                f"gamecard:{game_id}",
                {"game_id": game_id, "fans": sorted(fans)},
                expires_at=datetime.now(timezone.utc) + CARD_MARKER_TTL,
            )
        except Exception:  # noqa: BLE001 — best-effort; worst case a dup after restart
            logger.debug("game.final card marker save failed", exc_info=True)

    @staticmethod
    def _card_text(game, winner: str, favorite_names: List[str]) -> tuple:
        label = league_label(game.league.value) if league_label else game.league.value
        title = (
            f"Final: {game.away_team} {game.away_score}, "
            f"{game.home_team} {game.home_score}"
        )
        summary = f"{winner} win ({label})" if winner else f"Tie game ({label})"
        body_lines = [
            f"{game.away_team} {game.away_score} — {game.home_team} {game.home_score}",
            label,
        ]
        if favorite_names:
            body_lines.append("Following: " + ", ".join(favorite_names))
        return title, summary, "\n".join(body_lines)

    @staticmethod
    def _winner(game) -> str:
        if game.home_score > game.away_score:
            return game.home_team
        if game.away_score > game.home_score:
            return game.away_team
        return ""  # tie

    @staticmethod
    def _summary(game, winner: str) -> str:
        label = league_label(game.league.value) if league_label else game.league.value
        line = (
            f"Final: {game.away_team} {game.away_score}, "
            f"{game.home_team} {game.home_score} ({label})"
        )
        if winner:
            line += f" — {winner} win"
        return line

    # ── Housekeeping ──────────────────────────────────────────────────────

    def _prune(self) -> None:
        cutoff = self._now() - PRUNE_AFTER
        stale = [eid for eid, ts in self._touched_at.items() if ts < cutoff]
        for eid in stale:
            self._touched_at.pop(eid, None)
            self._emitted_finals.pop(eid, None)
            self._tracked_dates.pop(eid, None)
            prefix = f"{eid}:"
            for key in [k for k in self._carded_fans if k.startswith(prefix)]:
                self._carded_fans.discard(key)
