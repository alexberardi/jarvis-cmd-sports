"""Scoreboard status parsing + college-basketball team resolution.

Live-verified ESPN contract (2026-08-18): status.type.name is the raw ESPN
label ("STATUS_FINAL" etc.), status.type.completed is True only for a final
WITH a result, and postponed games are state="post" with completed=False —
so `state == "post"` alone must never be treated as "game over".
"""

from sports_shared.espn_sports_service import ESPNSportsService, League


def _event(event_id, name, state, completed, home="Hoosiers", away="Boilermakers",
           home_score="70", away_score="78"):
    return {
        "id": event_id,
        "date": "2026-03-14T23:00Z",
        "status": {"type": {"name": name, "state": state, "completed": completed}},
        "competitions": [
            {
                "competitors": [
                    {"team": {"name": home}, "score": home_score, "homeAway": "home"},
                    {"team": {"name": away}, "score": away_score, "homeAway": "away"},
                ],
                "venue": {"fullName": "Mackey Arena"},
                "broadcasts": [{"names": ["CBS"]}],
            }
        ],
    }


def _parse(events):
    service = ESPNSportsService()
    return service._parse_scoreboard_response(
        {"events": events}, League.COLLEGE_BASKETBALL
    )


class TestStatusParsing:
    def test_final_game_parses_completed_and_state(self):
        (game,) = _parse([_event("401", "STATUS_FINAL", "post", True)])
        assert game.status == "STATUS_FINAL"
        assert game.completed is True
        assert game.state == "post"
        assert (game.home_score, game.away_score) == (70, 78)

    def test_postponed_is_post_but_not_completed(self):
        (game,) = _parse([_event("402", "STATUS_POSTPONED", "post", False)])
        assert game.state == "post"
        assert game.completed is False
        assert "FINAL" not in game.status

    def test_scheduled_game(self):
        (game,) = _parse(
            [_event("403", "STATUS_SCHEDULED", "pre", False, home_score="0", away_score="0")]
        )
        assert game.completed is False
        assert game.state == "pre"
        # ESPN sends score "0" pre-game — parsed as 0, NOT a real result
        assert (game.home_score, game.away_score) == (0, 0)

    def test_missing_status_block_falls_back(self):
        event = _event("404", "STATUS_SCHEDULED", "pre", False)
        del event["status"]
        (game,) = _parse([event])
        assert game.status == "scheduled"
        assert game.completed is False
        assert game.state is None


class TestCollegeBasketballResolution:
    def test_power_conference_school_resolves_to_basketball_too(self):
        service = ESPNSportsService()
        leagues = {t.league for t in service.resolve_team("Purdue")}
        assert League.COLLEGE_BASKETBALL in leagues
        assert League.COLLEGE_FOOTBALL in leagues

    def test_basketball_only_school_still_resolves(self):
        service = ESPNSportsService()
        leagues = {t.league for t in service.resolve_team("Gonzaga")}
        assert leagues == {League.COLLEGE_BASKETBALL}
