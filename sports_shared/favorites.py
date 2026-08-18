"""Favorite-team records shared by the get_sports command and sports_alerts agent.

Favorites live in the node's command-data store under the "get_sports" storage
name, one record per (user, team):

    {"team_name": "Purdue Boilermakers", "league": "college-basketball",
     "id": "<uuid hex>", "user_id": <auth user id>}

Records are created from the mobile data browser (Node Settings -> Stored Data),
which stamps ``user_id`` server-side from the authenticated caller — the same
integer id space used for inbox-card targeting and Signal Bus scope. The agent
reads every user's rows via JarvisStorage.get_all(); rows missing any of the
three fields (legacy/malformed) are skipped rather than guessed at.
"""

from dataclasses import dataclass
from typing import Any

try:
    from sports_shared.espn_sports_service import League, TeamNameResolver
except ImportError:
    League = None
    TeamNameResolver = None

STORAGE_NAME = "get_sports"

LEAGUE_LABELS: dict[str, str] = {
    "nfl": "NFL",
    "nba": "NBA",
    "mlb": "MLB",
    "nhl": "NHL",
    "college-football": "College Football",
    "college-basketball": "College Basketball",
}

_resolver = None


def _get_resolver():
    global _resolver
    if _resolver is None and TeamNameResolver is not None:
        _resolver = TeamNameResolver()
    return _resolver


@dataclass(frozen=True)
class Favorite:
    user_id: int
    team_name: str
    league: str  # a League.value string


def league_values() -> list[str]:
    if League is None:
        return list(LEAGUE_LABELS)
    return [league.value for league in League]


def league_label(value: str | None) -> str:
    return LEAGUE_LABELS.get(value or "", value or "")


def parse_favorites(records: list[dict[str, Any]] | None) -> list[Favorite]:
    """Valid favorites from raw JarvisStorage rows; malformed rows are skipped.

    Every field is type-guarded before use: a record whose ``league`` is a
    non-string (e.g. a double-encoded ``[]``) must be skipped, not raise — a
    single bad row would otherwise take the ``in`` membership test down and,
    via ``load_favorites``'s catch-all, silently disable all alerts.
    """
    valid_leagues = set(league_values())
    favorites: list[Favorite] = []
    for record in records or []:
        if not isinstance(record, dict):
            continue
        team_name = record.get("team_name")
        league = record.get("league")
        user_id = record.get("user_id")
        if not team_name or not isinstance(team_name, str):
            continue
        if not isinstance(league, str) or league not in valid_leagues:
            continue
        if not isinstance(user_id, int) or isinstance(user_id, bool):
            continue
        favorites.append(
            Favorite(user_id=user_id, team_name=team_name.strip(), league=league)
        )
    return favorites


def load_favorites() -> list[Favorite]:
    """All users' favorites from the node's command-data store.

    JarvisStorage.get_all() has no per-user filtering — that's the point: the
    background agent watches every household member's teams.
    """
    from jarvis_command_sdk import JarvisStorage

    try:
        return parse_favorites(JarvisStorage(STORAGE_NAME).get_all())
    except Exception:
        return []


def resolve_in_league(team_name: str, league: str) -> list:
    """Teams matching ``team_name`` restricted to the declared league.

    The resolver's alias table maps a bare city/school word to a NICKNAME
    ("kentucky" -> "Wildcats"), which then expands to every same-nickname school
    (Villanova, Northwestern, ...). That over-match would card a Kentucky fan for
    a Villanova game. So when the input actually names a city/school, narrow to
    the teams whose city or full name contains it; a pure-nickname input
    ("Lakers") keeps its unique match unchanged.
    """
    resolver = _get_resolver()
    if resolver is None or not team_name:
        return []
    teams = [t for t in resolver.resolve_team(team_name) if t.league.value == league]
    needle = team_name.casefold().strip()
    specific = [
        t
        for t in teams
        if needle in (t.city or "").casefold()
        or needle in (t.full_name or "").casefold()
    ]
    return specific or teams


@dataclass(frozen=True)
class ResolvedFavorite:
    """A favorite with its resolved teams — computed once per poll, not per game."""

    favorite: Favorite
    teams: tuple  # resolved Team objects (in the favorite's league)


def resolve_favorites(favorites: list[Favorite]) -> list[ResolvedFavorite]:
    """Resolve every favorite's teams ONCE (O(favorites x team-DB)), so game
    matching is a cheap in-memory scan rather than re-resolving per game."""
    resolved: list[ResolvedFavorite] = []
    for favorite in favorites:
        teams = tuple(resolve_in_league(favorite.team_name, favorite.league))
        if teams:
            resolved.append(ResolvedFavorite(favorite=favorite, teams=teams))
    return resolved


def _team_in_side(team, side_name: str | None, side_display: str | None) -> bool:
    """Does a resolved Team identify this game side?

    ESPN's ``side_name`` is the BARE nickname ("Wildcats"), which collides across
    schools, so nickname EQUALITY (not substring — "Cardinal" != "Cardinals") is
    the floor. For college, many schools share a nickname, so the team's city
    must also appear in the full ``side_display`` ("Kentucky Wildcats"). Pro
    nicknames are unique in-league, so the city token is present there too
    ("Cincinnati" in "Cincinnati Reds") — a single rule covers both.
    """
    nickname = (team.nickname or "").casefold()
    if not nickname or nickname != (side_name or "").casefold():
        return False
    display = (side_display or side_name or "").casefold()
    city = (team.city or "").casefold()
    return not city or city in display


def fans_for_game(
    resolved_favorites: list[ResolvedFavorite], game
) -> tuple[list[str], list[int]]:
    """(favorited team names involved in ``game``, fan user ids) for one Game.

    A favorite counts only when its resolved team identity (nickname + city)
    matches a game side — NOT a bare-nickname substring, which would card fans
    of a different same-nickname school (Kentucky vs Villanova "Wildcats").
    """
    names: set[str] = set()
    fans: set[int] = set()
    game_league = game.league.value if game.league is not None else None
    for rf in resolved_favorites:
        if rf.favorite.league != game_league:
            continue
        for team in rf.teams:
            if _team_in_side(team, game.home_team, getattr(game, "home_display", None)) or \
               _team_in_side(team, game.away_team, getattr(game, "away_display", None)):
                names.add(rf.favorite.team_name)
                fans.add(rf.favorite.user_id)
                break
    return sorted(names), sorted(fans)
