"""sports_alerts agent: final detection, game.final emission, dedup, cadence.

The emit payload asserted here is the cross-repo contract command-center's
final-score reaction consumes — field changes must land in both repos.
"""

import asyncio
import importlib.util
import os
import sys
from datetime import datetime, timedelta

import pytest

from sports_shared.espn_sports_service import Game, League
from sports_shared.favorites import Favorite

_HERE = os.path.dirname(os.path.abspath(__file__))
_ROOT = os.path.abspath(os.path.join(_HERE, ".."))


def _load_agent_module():
    path = os.path.join(_ROOT, "agents", "sports_alerts", "agent.py")
    spec = importlib.util.spec_from_file_location("sports_alerts_test_agent", path)
    module = importlib.util.module_from_spec(spec)
    sys.modules["sports_alerts_test_agent"] = module
    spec.loader.exec_module(module)
    return module


@pytest.fixture
def agent_mod():
    return _load_agent_module()


class FakeESPN:
    """Scripted scoreboard: games per league value; counts calls."""

    def __init__(self, games_by_league=None):
        self.games_by_league = games_by_league or {}
        self.calls: list[tuple[str, str]] = []

    def get_scores(self, league, date):
        self.calls.append((league.value, date))
        return list(self.games_by_league.get(league.value, []))


def _game(
    event_id="401",
    league=League.COLLEGE_BASKETBALL,
    home="Hoosiers",
    away="Boilermakers",
    home_score=70,
    away_score=78,
    status="STATUS_FINAL",
    completed=True,
    state="post",
    start_time=None,
    home_display="Indiana Hoosiers",
    away_display="Purdue Boilermakers",
):
    return Game(
        id=event_id,
        home_team=home,
        away_team=away,
        home_score=home_score,
        away_score=away_score,
        status=status,
        start_time=start_time,
        league=league,
        completed=completed,
        state=state,
        home_display=home_display,
        away_display=away_display,
    )


def _make_agent(agent_mod, monkeypatch, favorites, games_by_league):
    agent = agent_mod.SportsAlertsAgent()
    agent._espn = FakeESPN(games_by_league)
    monkeypatch.setattr(agent_mod, "load_favorites", lambda: list(favorites))
    return agent


_PURDUE_FAN = Favorite(user_id=3, team_name="Purdue", league="college-basketball")
_IU_FAN = Favorite(user_id=5, team_name="Indiana", league="college-basketball")


class TestEmission:
    def test_final_emits_clean_fact(self, agent_mod, monkeypatch, signals_backend):
        agent = _make_agent(
            agent_mod, monkeypatch, [_PURDUE_FAN, _IU_FAN],
            {"college-basketball": [_game()]},
        )
        asyncio.run(agent.run())

        assert len(signals_backend.payloads) == 1
        payload = signals_backend.payloads[0]
        assert "command" not in payload  # OPEN signal, never a directed proposal
        signal = payload["signal"]
        assert signal["kind"] == "game.final"
        assert signal["source_key"] == "game:college-basketball:401"
        assert signal["subject"] == "401"
        assert signal["source_agent"] == "sports_alerts"
        assert signal["ttl_seconds"] == agent_mod.SIGNAL_TTL_SECONDS
        assert signal["cacheable"] is False
        assert "scope" not in signal  # household-wide fact

        facts = payload["data"]
        assert facts["event_id"] == "401"
        assert facts["league"] == "college-basketball"
        assert facts["home_team"] == "Hoosiers"
        assert facts["away_team"] == "Boilermakers"
        assert facts["home_score"] == 70
        assert facts["away_score"] == 78
        assert facts["winner"] == "Boilermakers"
        # The signal is the pure FACT — delivery targeting (which fans) is NOT
        # part of it; that lives in the card path.
        assert "fan_user_ids" not in facts
        assert "favorite_teams" not in facts

    def test_unchanged_final_not_re_emitted(self, agent_mod, monkeypatch, signals_backend):
        agent = _make_agent(
            agent_mod, monkeypatch, [_PURDUE_FAN], {"college-basketball": [_game()]}
        )
        asyncio.run(agent.run())
        asyncio.run(agent.run())
        assert len(signals_backend.payloads) == 1

    def test_corrected_final_re_emits(self, agent_mod, monkeypatch, signals_backend):
        agent = _make_agent(
            agent_mod, monkeypatch, [_PURDUE_FAN], {"college-basketball": [_game()]}
        )
        asyncio.run(agent.run())
        agent._espn.games_by_league["college-basketball"] = [_game(home_score=71)]
        asyncio.run(agent.run())
        assert len(signals_backend.payloads) == 2
        assert signals_backend.payloads[1]["data"]["home_score"] == 71

    def test_rejected_emit_retries_next_poll(self, agent_mod, monkeypatch, signals_backend):
        agent = _make_agent(
            agent_mod, monkeypatch, [_PURDUE_FAN], {"college-basketball": [_game()]}
        )
        signals_backend.tags = ["http_error"]
        asyncio.run(agent.run())
        asyncio.run(agent.run())  # backend healthy again -> re-emit
        assert len(signals_backend.payloads) == 2

    def test_final_without_scores_not_emitted(self, agent_mod, monkeypatch, signals_backend):
        agent = _make_agent(
            agent_mod, monkeypatch, [_PURDUE_FAN],
            {"college-basketball": [_game(home_score=None, away_score=None)]},
        )
        asyncio.run(agent.run())
        assert signals_backend.payloads == []

    def test_tie_has_empty_winner(self, agent_mod, monkeypatch, signals_backend):
        agent = _make_agent(
            agent_mod, monkeypatch, [_PURDUE_FAN],
            {"college-basketball": [_game(home_score=70, away_score=70)]},
        )
        asyncio.run(agent.run())
        assert signals_backend.payloads[0]["data"]["winner"] == ""


class TestCards:
    def test_cards_each_fan_via_generic_inbox(self, agent_mod, monkeypatch, signals_backend, inbox_backend, storage_backend):
        agent = _make_agent(
            agent_mod, monkeypatch, [_PURDUE_FAN, _IU_FAN],
            {"college-basketball": [_game()]},
        )
        asyncio.run(agent.run())

        assert len(inbox_backend.posts) == 2
        targeted = {p["user_id"] for p in inbox_backend.posts}
        assert targeted == {3, 5}
        p = inbox_backend.posts[0]
        assert p["command_name"] == "sports_alerts"
        assert p["category"] == "sports"
        assert p["target_type"] == "user"
        assert p["create_push_notification"] is True
        assert p["metadata"]["dedupe_key"] == f"gamecard:401:{p['user_id']}"
        assert p["title"] == "Final: Boilermakers 78, Hoosiers 70"
        assert "Boilermakers win" in p["summary"]

    def test_card_posted_once_per_fan_per_game(self, agent_mod, monkeypatch, signals_backend, inbox_backend, storage_backend):
        agent = _make_agent(
            agent_mod, monkeypatch, [_PURDUE_FAN], {"college-basketball": [_game()]}
        )
        asyncio.run(agent.run())
        asyncio.run(agent.run())  # game still final on the board
        assert len(inbox_backend.posts) == 1

    def test_card_dedup_survives_restart(self, agent_mod, monkeypatch, signals_backend, inbox_backend, storage_backend):
        # The whole point of the durable marker: a fresh agent instance (== a
        # node restart, in-memory _carded_fans empty) must NOT re-card fans.
        games = {"college-basketball": [_game()]}
        agent1 = _make_agent(agent_mod, monkeypatch, [_PURDUE_FAN], games)
        asyncio.run(agent1.run())
        assert len(inbox_backend.posts) == 1

        agent2 = _make_agent(agent_mod, monkeypatch, [_PURDUE_FAN], games)
        assert agent2._carded_fans == set()  # fresh process, no in-memory state
        asyncio.run(agent2.run())
        assert len(inbox_backend.posts) == 1  # durable marker blocked the re-card

    def test_failed_card_retries_next_poll(self, agent_mod, monkeypatch, signals_backend, inbox_backend, storage_backend):
        agent = _make_agent(
            agent_mod, monkeypatch, [_PURDUE_FAN], {"college-basketball": [_game()]}
        )
        inbox_backend.tags = ["http_error"]
        asyncio.run(agent.run())
        asyncio.run(agent.run())  # inbox healthy again -> retry the unposted fan
        assert len(inbox_backend.posts) == 2

    def test_tie_card_text(self, agent_mod, monkeypatch, signals_backend, inbox_backend, storage_backend):
        agent = _make_agent(
            agent_mod, monkeypatch, [_PURDUE_FAN],
            {"college-basketball": [_game(home_score=70, away_score=70)]},
        )
        asyncio.run(agent.run())
        assert "Tie game" in inbox_backend.posts[0]["summary"]

    def test_no_fans_no_card(self, agent_mod, monkeypatch, signals_backend, inbox_backend, storage_backend):
        # Unfavorited game: neither signal nor card.
        agent = _make_agent(
            agent_mod, monkeypatch, [_PURDUE_FAN],
            {"college-basketball": [_game(home="Bulldogs", away="Wildcats",
                                          home_display="Georgia Bulldogs",
                                          away_display="Kansas State Wildcats")]},
        )
        asyncio.run(agent.run())
        assert inbox_backend.posts == []
        assert signals_backend.payloads == []


class TestPruneDoesNotResurrectFinals:
    def test_still_on_board_final_survives_prune(self, agent_mod, monkeypatch, signals_backend):
        # Regression: a final that stays on the ESPN board must keep its dedup
        # entry refreshed on every sighting, so a co-tracked game keeping the
        # date polled can't let prune evict it and cause a spurious re-emit.
        agent = _make_agent(
            agent_mod, monkeypatch, [_PURDUE_FAN], {"college-basketball": [_game()]}
        )
        asyncio.run(agent.run())
        assert len(signals_backend.payloads) == 1

        # Simulate 40h passing with no refresh of the frozen entry...
        from datetime import timedelta
        old = agent._now() - timedelta(hours=40)
        agent._touched_at["401"] = old
        # ...then the final is still on the board on the next poll.
        asyncio.run(agent.run())
        # Touched-on-sighting keeps it alive → prune leaves it → no re-emit.
        assert len(signals_backend.payloads) == 1
        assert "401" in agent._emitted_finals


class TestNonFinals:
    def test_postponed_never_emits(self, agent_mod, monkeypatch, signals_backend):
        agent = _make_agent(
            agent_mod, monkeypatch, [_PURDUE_FAN],
            {"college-basketball": [
                _game(status="STATUS_POSTPONED", completed=False, state="post")
            ]},
        )
        asyncio.run(agent.run())
        assert signals_backend.payloads == []

    def test_unfavorited_game_ignored(self, agent_mod, monkeypatch, signals_backend):
        agent = _make_agent(
            agent_mod, monkeypatch, [_PURDUE_FAN],
            {"college-basketball": [_game(home="Bulldogs", away="Wildcats")]},
        )
        asyncio.run(agent.run())
        assert signals_backend.payloads == []

    def test_no_favorites_skips_espn_entirely(self, agent_mod, monkeypatch, signals_backend):
        agent = _make_agent(agent_mod, monkeypatch, [], {"college-basketball": [_game()]})
        asyncio.run(agent.run())
        assert agent._espn.calls == []
        assert signals_backend.payloads == []


class TestCadence:
    def test_idle_without_games(self, agent_mod, monkeypatch, signals_backend):
        agent = _make_agent(agent_mod, monkeypatch, [_PURDUE_FAN], {})
        asyncio.run(agent.run())
        assert agent.schedule.interval_seconds == agent_mod.IDLE_INTERVAL_SECONDS

    def test_live_game_polls_fast(self, agent_mod, monkeypatch, signals_backend):
        agent = _make_agent(
            agent_mod, monkeypatch, [_PURDUE_FAN],
            {"college-basketball": [
                _game(status="STATUS_IN_PROGRESS", completed=False, state="in")
            ]},
        )
        asyncio.run(agent.run())
        assert agent.schedule.interval_seconds == agent_mod.LIVE_INTERVAL_SECONDS

    def test_imminent_start_polls_warm(self, agent_mod, monkeypatch, signals_backend):
        soon = datetime.now() + timedelta(minutes=30)
        agent = _make_agent(
            agent_mod, monkeypatch, [_PURDUE_FAN],
            {"college-basketball": [
                _game(status="STATUS_SCHEDULED", completed=False, state="pre",
                      start_time=soon, home_score=0, away_score=0)
            ]},
        )
        asyncio.run(agent.run())
        assert agent.schedule.interval_seconds == agent_mod.PREGAME_INTERVAL_SECONDS

    def test_distant_start_stays_idle(self, agent_mod, monkeypatch, signals_backend):
        tonight = datetime.now() + timedelta(hours=9)
        agent = _make_agent(
            agent_mod, monkeypatch, [_PURDUE_FAN],
            {"college-basketball": [
                _game(status="STATUS_SCHEDULED", completed=False, state="pre",
                      start_time=tonight, home_score=0, away_score=0)
            ]},
        )
        asyncio.run(agent.run())
        assert agent.schedule.interval_seconds == agent_mod.IDLE_INTERVAL_SECONDS

    def test_tracked_game_date_stays_polled(self, agent_mod, monkeypatch, signals_backend):
        agent = _make_agent(
            agent_mod, monkeypatch, [_PURDUE_FAN],
            {"college-basketball": [
                _game(status="STATUS_IN_PROGRESS", completed=False, state="in")
            ]},
        )
        asyncio.run(agent.run())
        # Pin the tracked date to "yesterday" and confirm the next poll asks
        # ESPN for it (covers past-midnight-ET finishes).
        agent._tracked_dates["401"] = "20260817"
        asyncio.run(agent.run())
        polled_dates = {d for (_, d) in agent._espn.calls}
        assert "20260817" in polled_dates


class TestRunSafety:
    def test_run_never_raises(self, agent_mod, monkeypatch, signals_backend):
        agent = _make_agent(agent_mod, monkeypatch, [_PURDUE_FAN], {})

        def _boom():
            raise RuntimeError("storage down")

        monkeypatch.setattr(agent_mod, "load_favorites", _boom)
        asyncio.run(agent.run())  # must not raise (3 raises would auto-disable)
