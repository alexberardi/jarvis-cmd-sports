"""Shared test scaffolding for jarvis-cmd-sports.

Tests run against the node repo's venv (which has jarvis_command_sdk installed);
the package's own modules are imported with the repo root on sys.path, and
jarvis_log_client is stubbed so modules import off-node.
"""

import os
import sys
import types

import pytest

_ROOT = os.path.abspath(os.path.join(os.path.dirname(os.path.abspath(__file__)), ".."))
if _ROOT not in sys.path:
    sys.path.insert(0, _ROOT)


def _stub_log_client() -> None:
    if "jarvis_log_client" in sys.modules:
        return
    stub = types.ModuleType("jarvis_log_client")

    class _Logger:
        def __init__(self, **kwargs): ...
        def info(self, *a, **k): ...
        def warning(self, *a, **k): ...
        def error(self, *a, **k): ...
        def debug(self, *a, **k): ...

    stub.JarvisLogger = _Logger
    sys.modules["jarvis_log_client"] = stub


_stub_log_client()


class CapturingSignalsBackend:
    """SDK SignalsBackend that records emit payloads; scriptable return tag."""

    def __init__(self) -> None:
        self.payloads: list = []
        self.tags: list[str] = []  # pop-from-front script; default "ok"

    def emit_signal(self, payload):
        self.payloads.append(payload)
        return self.tags.pop(0) if self.tags else "ok"


@pytest.fixture
def signals_backend():
    from jarvis_command_sdk.signals import set_signals_backend

    backend = CapturingSignalsBackend()
    set_signals_backend(backend)
    yield backend
    set_signals_backend(None)


class CapturingInboxBackend:
    """SDK InboxBackend that records posted cards; scriptable return tag."""

    def __init__(self) -> None:
        self.posts: list[dict] = []
        self.tags: list[str] = []  # pop-from-front script; default "ok"

    def post_inbox_item(self, command_name, **kwargs):
        self.posts.append({"command_name": command_name, **kwargs})
        return self.tags.pop(0) if self.tags else "ok"


@pytest.fixture
def inbox_backend():
    from jarvis_command_sdk.inbox import set_inbox_backend

    backend = CapturingInboxBackend()
    set_inbox_backend(backend)
    yield backend
    set_inbox_backend(None)


class FakeStorageBackend:
    """In-memory SDK StorageBackend covering the data + secret surface."""

    def __init__(self) -> None:
        self.rows: dict[tuple[str, str], dict] = {}

    def save(self, command_name, data_key, data, expires_at=None):
        self.rows[(command_name, data_key)] = dict(data)

    def get(self, command_name, data_key):
        row = self.rows.get((command_name, data_key))
        return dict(row) if row is not None else None

    def get_all(self, command_name):
        return [dict(v) for (c, _), v in self.rows.items() if c == command_name]

    def delete(self, command_name, data_key):
        return self.rows.pop((command_name, data_key), None) is not None

    def delete_all(self, command_name):
        keys = [k for k in self.rows if k[0] == command_name]
        for k in keys:
            del self.rows[k]
        return len(keys)

    def get_secret(self, key, scope, user_id=None):
        return None

    def set_secret(self, key, value, scope, value_type="string", user_id=None):
        return None

    def delete_secret(self, key, scope, user_id=None):
        return None


@pytest.fixture
def storage_backend():
    from jarvis_command_sdk import set_backend

    backend = FakeStorageBackend()
    set_backend(backend)
    yield backend
    set_backend(None)
