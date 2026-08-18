"""Favorite-team CRUD via the mobile data browser (GetSportsCommand surface).

The browser stamps user_id server-side from the authenticated caller; the
command validates the team resolves within the declared league so a typo'd
favorite can't silently watch nothing.
"""

import importlib.util
import os
import sys

import pytest

_HERE = os.path.dirname(os.path.abspath(__file__))
_ROOT = os.path.abspath(os.path.join(_HERE, ".."))


def _load_command_module():
    path = os.path.join(_ROOT, "commands", "get_sports", "command.py")
    spec = importlib.util.spec_from_file_location("sports_browser_test_command", path)
    module = importlib.util.module_from_spec(spec)
    sys.modules["sports_browser_test_command"] = module
    spec.loader.exec_module(module)
    return module


@pytest.fixture
def command():
    return _load_command_module().SportsCommand()


class TestSchema:
    def test_editable_fields(self, command):
        from sports_shared.favorites import league_values

        fields = {f.name: f for f in command.editable_fields()}
        assert set(fields) == {"team_name", "league", "user_id"}
        assert fields["team_name"].required is True
        assert fields["league"].type == "enum"
        assert fields["league"].enum_values == league_values()
        assert fields["user_id"].editable is False

    def test_supports_create(self, command):
        assert command.data_browser_supports_create is True

    def test_display_summary(self, command):
        summary = command.display_summary(
            {"team_name": "Purdue", "league": "college-basketball", "user_id": 3}
        )
        assert summary.title == "Purdue"
        assert summary.subtitle == "College Basketball"

    def test_display_summary_tolerates_partial_record(self, command):
        summary = command.display_summary({})
        assert summary.title  # never raises, never empty


class TestCreate:
    def test_valid_create_stamps_owner(self, command, storage_backend):
        key, record = command.data_browser_create(
            {"team_name": " Purdue ", "league": "college-basketball"},
            requesting_user_id=3,
        )
        assert record["team_name"] == "Purdue"
        assert record["league"] == "college-basketball"
        assert record["user_id"] == 3
        assert record["id"] == key
        stored = storage_backend.get("get_sports", key)
        assert stored is not None and stored["user_id"] == 3

    def test_empty_team_rejected(self, command, storage_backend):
        with pytest.raises(ValueError, match="team_name"):
            command.data_browser_create(
                {"team_name": "  ", "league": "mlb"}, requesting_user_id=3
            )

    def test_unknown_league_rejected(self, command, storage_backend):
        with pytest.raises(ValueError, match="league"):
            command.data_browser_create(
                {"team_name": "Mets", "league": "kbo"}, requesting_user_id=3
            )

    def test_team_not_in_league_rejected(self, command, storage_backend):
        with pytest.raises(ValueError, match="team_name"):
            command.data_browser_create(
                {"team_name": "Purdue", "league": "nba"}, requesting_user_id=3
            )

    def test_duplicate_favorite_rejected(self, command, storage_backend):
        command.data_browser_create(
            {"team_name": "Purdue", "league": "college-basketball"},
            requesting_user_id=3,
        )
        with pytest.raises(ValueError, match="already"):
            command.data_browser_create(
                {"team_name": "purdue", "league": "college-basketball"},
                requesting_user_id=3,
            )
        # Same team is fine for a DIFFERENT user
        command.data_browser_create(
            {"team_name": "Purdue", "league": "college-basketball"},
            requesting_user_id=5,
        )

    def test_unknown_user_fails_closed(self, command, storage_backend):
        with pytest.raises(ValueError):
            command.data_browser_create(
                {"team_name": "Mets", "league": "mlb"}, requesting_user_id=None
            )
        assert storage_backend.get_all("get_sports") == []
