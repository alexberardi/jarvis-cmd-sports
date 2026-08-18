"""Favorites record parsing + game→fan mapping (sports_shared/favorites.py)."""

from datetime import datetime

from sports_shared.espn_sports_service import Game, League
from sports_shared.favorites import (
    Favorite,
    fans_for_game,
    league_label,
    league_values,
    parse_favorites,
)


def _game(league=League.COLLEGE_BASKETBALL, home="Hoosiers", away="Boilermakers"):
    return Game(
        id="401",
        home_team=home,
        away_team=away,
        home_score=70,
        away_score=78,
        status="STATUS_FINAL",
        start_time=datetime(2026, 3, 14, 19, 0),
        league=league,
        completed=True,
        state="post",
    )


class TestParseFavorites:
    def test_valid_record(self):
        favs = parse_favorites(
            [{"team_name": " Purdue ", "league": "college-basketball", "user_id": 3,
              "id": "abc", "_data_key": "abc", "_expires_at": None}]
        )
        assert favs == [Favorite(user_id=3, team_name="Purdue", league="college-basketball")]

    def test_malformed_records_skipped(self):
        records = [
            {"league": "nba", "user_id": 3},                       # no team
            {"team_name": "Mets", "user_id": 3},                    # no league
            {"team_name": "Mets", "league": "mlb"},                 # no user
            {"team_name": "Mets", "league": "kbo", "user_id": 3},   # unknown league
            {"team_name": "Mets", "league": "mlb", "user_id": True},  # bool ≠ user id
            {"team_name": "", "league": "mlb", "user_id": 3},       # empty team
            "not-a-dict",
        ]
        assert parse_favorites(records) == []

    def test_none_input(self):
        assert parse_favorites(None) == []


class TestLeagues:
    def test_league_values_match_enum(self):
        assert set(league_values()) == {league.value for league in League}

    def test_league_label(self):
        assert league_label("college-basketball") == "College Basketball"
        assert league_label("nfl") == "NFL"
        assert league_label(None) == ""
        # Unknown values pass through rather than erroring
        assert league_label("kbo") == "kbo"


class TestFansForGame:
    def test_fans_of_both_teams_union(self):
        favorites = [
            Favorite(3, "Purdue", "college-basketball"),
            Favorite(5, "Indiana", "college-basketball"),
            Favorite(5, "Purdue", "college-basketball"),  # fan of both
        ]
        names, fans = fans_for_game(favorites, _game())
        assert fans == [3, 5]
        assert names == ["Indiana", "Purdue"]

    def test_wrong_league_excluded(self):
        # A Purdue *football* favorite must not match a basketball game
        favorites = [Favorite(3, "Purdue", "college-football")]
        names, fans = fans_for_game(favorites, _game())
        assert (names, fans) == ([], [])

    def test_unrelated_team_excluded(self):
        favorites = [Favorite(3, "Gonzaga", "college-basketball")]
        names, fans = fans_for_game(favorites, _game())
        assert (names, fans) == ([], [])

    def test_no_favorites(self):
        assert fans_for_game([], _game()) == ([], [])
