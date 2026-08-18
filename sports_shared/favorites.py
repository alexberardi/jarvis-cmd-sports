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
    """Valid favorites from raw JarvisStorage rows; malformed rows are skipped."""
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
        if league not in valid_leagues:
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
    """Teams matching ``team_name`` restricted to the declared league."""
    resolver = _get_resolver()
    if resolver is None or not team_name:
        return []
    return [t for t in resolver.resolve_team(team_name) if t.league.value == league]


def fans_for_game(favorites: list[Favorite], game) -> tuple[list[str], list[int]]:
    """(favorited team names involved in ``game``, fan user ids) for one Game.

    Matching mirrors ESPNSportsService.get_team_scores: a favorite counts when
    one of its resolved nicknames appears in the game's home or away name.
    """
    names: set[str] = set()
    fans: set[int] = set()
    game_league = game.league.value if game.league is not None else None
    for favorite in favorites:
        if favorite.league != game_league:
            continue
        for team in resolve_in_league(favorite.team_name, favorite.league):
            if team.nickname in game.home_team or team.nickname in game.away_team:
                names.add(favorite.team_name)
                fans.add(favorite.user_id)
                break
    return sorted(names), sorted(fans)
