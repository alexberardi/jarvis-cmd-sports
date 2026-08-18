"""Sports score watcher: polls ESPN scoreboards for favorited teams and emits a
``game.final`` Signal when a game goes final.

Producer side of the game.final feature (see sports_shared/favorites.py for the
favorites contract). The judgment about what to DO with a final score — card,
automation, ambient answer — lives server-side in command-center; this agent
only reports the fact, mirroring the calendar_alerts producer pattern.

Dedup is in-memory keyed event_id -> final (home, away) score, so a CORRECTED
final re-emits (command-center upserts on source_key) while an unchanged final
does not. A node restart re-emits finals still on today's scoreboard; that is
safe by design: the signal upserts and command-center's card reaction holds a
durable per-(game, fan) claim.
"""

import asyncio
from datetime import datetime, timedelta, timezone
from typing import Any, Dict, List
from zoneinfo import ZoneInfo

from jarvis_command_sdk import AgentSchedule, IJarvisAgent, IJarvisSecret, JarvisSignals

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
        # event_id -> (home_score, away_score) of the emitted final
        self._emitted_finals: Dict[str, tuple] = {}
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
        # Touch on EVERY sighting so a still-visible game (final or not) is never
        # pruned out from under an active date — pruning a still-on-board final
        # would drop its dedup entry and re-emit it.
        self._touched_at[game.id] = self._now()

        if self._is_final(game):
            await self._maybe_emit_final(game, favorite_names, fan_user_ids)
            self._tracked_dates.pop(game.id, None)
            return IDLE_INTERVAL_SECONDS

        self._tracked_dates[game.id] = espn_date

        if game.state == "in":
            return LIVE_INTERVAL_SECONDS
        if game.state == "post":
            # Postponed/suspended: nothing imminent.
            return IDLE_INTERVAL_SECONDS
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

    # ── Emission ──────────────────────────────────────────────────────────

    async def _maybe_emit_final(
        self, game, favorite_names: List[str], fan_user_ids: List[int]
    ) -> None:
        if game.home_score is None or game.away_score is None:
            return  # final without scores — corrupt payload, retry next poll
        score = (game.home_score, game.away_score)
        if self._emitted_finals.get(game.id) == score:
            return  # unchanged; a corrected final (score change) re-emits

        league_value = game.league.value
        winner = self._winner(game)
        summary = self._summary(game, winner)
        facts = {
            "event_id": str(game.id),
            "league": league_value,
            "home_team": game.home_team,
            "away_team": game.away_team,
            "home_score": game.home_score,
            "away_score": game.away_score,
            "winner": winner,
            "favorite_teams": favorite_names,
            "fan_user_ids": fan_user_ids,
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
                    summary=summary,
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
