"""Favorites record parsing + game→fan mapping (sports_shared/favorites.py)."""

from datetime import datetime

from sports_shared.espn_sports_service import Game, League
from sports_shared.favorites import (
    Favorite,
    fans_for_game,
    league_label,
    league_values,
    parse_favorites,
    resolve_favorites,
)


def _game(
    league=League.COLLEGE_BASKETBALL,
    home="Hoosiers",
    away="Boilermakers",
    home_display="Indiana Hoosiers",
    away_display="Purdue Boilermakers",
):
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
        home_display=home_display,
        away_display=away_display,
    )


def _fans(favorites, game):
    return fans_for_game(resolve_favorites(favorites), game)


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
        names, fans = _fans(favorites, _game())
        assert fans == [3, 5]
        assert names == ["Indiana", "Purdue"]

    def test_wrong_league_excluded(self):
        # A Purdue *football* favorite must not match a basketball game
        favorites = [Favorite(3, "Purdue", "college-football")]
        names, fans = _fans(favorites, _game())
        assert (names, fans) == ([], [])

    def test_unrelated_team_excluded(self):
        favorites = [Favorite(3, "Gonzaga", "college-basketball")]
        names, fans = _fans(favorites, _game())
        assert (names, fans) == ([], [])

    def test_no_favorites(self):
        assert _fans([], _game()) == ([], [])

    def test_shared_nickname_different_school_excluded(self):
        # Kentucky Wildcats fan must NOT be carded for a Villanova/Northwestern
        # game (both ESPN nicknames are "Wildcats") — the collision the identity
        # match fixes.
        game = _game(
            home="Wildcats", away="Wildcats",
            home_display="Villanova Wildcats", away_display="Northwestern Wildcats",
        )
        names, fans = _fans([Favorite(3, "Kentucky", "college-basketball")], game)
        assert (names, fans) == ([], [])

    def test_shared_nickname_same_school_matches(self):
        game = _game(
            home="Wildcats", away="Boilermakers",
            home_display="Kentucky Wildcats", away_display="Purdue Boilermakers",
        )
        names, fans = _fans([Favorite(3, "Kentucky", "college-basketball")], game)
        assert fans == [3]

    def test_substring_nickname_not_matched(self):
        # "Cardinal" (Stanford) must not match "Cardinals" (Louisville) — the
        # old substring test did; exact nickname equality does not.
        game = _game(
            home="Cardinals", away="Blue Devils",
            home_display="Louisville Cardinals", away_display="Duke Blue Devils",
        )
        names, fans = _fans([Favorite(3, "Stanford", "college-basketball")], game)
        assert (names, fans) == ([], [])

    def test_pro_league_unique_nickname_matches(self):
        game = Game(
            id="9", home_team="Reds", away_team="Cardinals",
            home_score=4, away_score=2, status="STATUS_FINAL",
            start_time=None, league=League.MLB, completed=True, state="post",
            home_display="Cincinnati Reds", away_display="St. Louis Cardinals",
        )
        names, fans = _fans([Favorite(7, "Cardinals", "mlb")], game)
        assert fans == [7]
        assert names == ["Cardinals"]
