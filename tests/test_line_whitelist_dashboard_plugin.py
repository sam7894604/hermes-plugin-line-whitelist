"""Tests for the LINE whitelist dashboard plugin backend
(plugins/platforms/line/dashboard/plugin_api.py).

The plugin mounts as /api/plugins/line-whitelist/ inside the dashboard's
FastAPI app; here we attach its router to a bare FastAPI instance so we can
test the REST surface in isolation.

The Phase-1 ``WhitelistStore`` may not exist in this worktree, so we inject a
fake ``plugins.platforms.line.whitelist_store`` module into ``sys.modules``.
The plugin imports the store lazily inside its handlers, so the fake is what
gets used.
"""

from __future__ import annotations

import importlib.util
import sys
import types
from pathlib import Path

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient


# ---------------------------------------------------------------------------
# Fake WhitelistStore (Phase 1 contract)
# ---------------------------------------------------------------------------


class _FakeWhitelistError(Exception):
    pass


class _FakeStore:
    """In-memory stand-in for the P1 WhitelistStore.

    Shared class-level state so every ``WhitelistStore()`` the handler
    constructs sees the same data within a test.
    """

    _data = {"users": [], "groups": [], "rooms": []}
    _admin = ("user", "Uadmin")

    _BUCKET = {"user": "users", "group": "groups", "room": "rooms"}

    @classmethod
    def reset(cls):
        cls._data = {"users": [], "groups": [], "rooms": []}

    def list(self, scope=None):
        if scope is None:
            return {k: list(v) for k, v in self._data.items()}
        bucket = self._BUCKET[scope]
        return {"users": [], "groups": [], "rooms": [], bucket: list(self._data[bucket])}

    def add(self, scope, id, added_by=None, note=None):
        bucket = self._BUCKET[scope]
        if any(e["id"] == id for e in self._data[bucket]):
            raise _FakeWhitelistError(f"{id} already present")
        entry = {"id": id, "note": note, "added_by": added_by, "added_at": 0}
        self._data[bucket].append(entry)
        return entry

    def remove(self, scope, id):
        if (scope, id) == self._admin:
            raise _FakeWhitelistError("cannot remove the admin entry")
        bucket = self._BUCKET[scope]
        before = len(self._data[bucket])
        self._data[bucket] = [e for e in self._data[bucket] if e["id"] != id]
        return len(self._data[bucket]) < before


@pytest.fixture(autouse=True)
def fake_store(monkeypatch):
    """Install a fake whitelist_store module and reset its state each test."""
    _FakeStore.reset()
    mod = types.ModuleType("plugins.platforms.line.whitelist_store")
    mod.WhitelistStore = _FakeStore
    mod.WhitelistError = _FakeWhitelistError
    monkeypatch.setitem(
        sys.modules, "plugins.platforms.line.whitelist_store", mod,
    )
    yield


def _load_plugin_router():
    # In this standalone repo the shipped code lives under ./src, mirroring its
    # in-Hermes path (plugins/platforms/line/dashboard/plugin_api.py).
    repo_root = Path(__file__).resolve().parents[1]
    plugin_file = (
        repo_root / "src" / "plugins" / "platforms" / "line"
        / "dashboard" / "plugin_api.py"
    )
    assert plugin_file.exists(), f"plugin file missing: {plugin_file}"
    spec = importlib.util.spec_from_file_location(
        "hermes_dashboard_plugin_line_whitelist_test", plugin_file,
    )
    assert spec is not None and spec.loader is not None
    mod = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = mod
    spec.loader.exec_module(mod)
    return mod.router


@pytest.fixture
def client():
    app = FastAPI()
    app.include_router(_load_plugin_router(), prefix="/api/plugins/line-whitelist")
    return TestClient(app)


API = "/api/plugins/line-whitelist"


# ---------------------------------------------------------------------------
# Whitelist CRUD
# ---------------------------------------------------------------------------


def test_list_empty(client):
    r = client.get(f"{API}/whitelist")
    assert r.status_code == 200
    assert r.json() == {"users": [], "groups": [], "rooms": []}


def test_add_and_list(client):
    r = client.post(f"{API}/whitelist", json={"scope": "user", "id": "U123", "note": "me"})
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["ok"] is True
    assert body["entry"]["id"] == "U123"
    assert body["entry"]["note"] == "me"

    r = client.get(f"{API}/whitelist")
    users = r.json()["users"]
    assert len(users) == 1 and users[0]["id"] == "U123"


def test_add_invalid_scope(client):
    r = client.post(f"{API}/whitelist", json={"scope": "bogus", "id": "X"})
    assert r.status_code == 400


def test_add_blank_id(client):
    r = client.post(f"{API}/whitelist", json={"scope": "user", "id": "  "})
    assert r.status_code == 400


def test_add_duplicate_is_4xx(client):
    client.post(f"{API}/whitelist", json={"scope": "group", "id": "C1"})
    r = client.post(f"{API}/whitelist", json={"scope": "group", "id": "C1"})
    assert r.status_code == 400


def test_remove(client):
    client.post(f"{API}/whitelist", json={"scope": "room", "id": "R9"})
    r = client.delete(f"{API}/whitelist/room/R9")
    assert r.status_code == 200
    assert r.json()["ok"] is True
    # gone now
    assert client.get(f"{API}/whitelist").json()["rooms"] == []


def test_remove_unknown_is_404(client):
    r = client.delete(f"{API}/whitelist/user/Unope")
    assert r.status_code == 404


def test_remove_admin_is_409(client):
    # The fake store refuses to delete the admin entry with WhitelistError,
    # which the handler surfaces as 409.
    r = client.delete(f"{API}/whitelist/user/Uadmin")
    assert r.status_code == 409


# ---------------------------------------------------------------------------
# Records — filters SessionDB rows to line: keys
# ---------------------------------------------------------------------------


def test_records_filters_line_sessions(client, monkeypatch):
    import hermes_state

    class _FakeDB:
        def list_sessions_rich(self, **kw):
            return [
                {"session_id": "s1", "session_key": "line:U123", "title": "LINE chat",
                 "message_count": 3, "last_active": 100, "started_at": 90},
                {"session_id": "s2", "session_key": "discord:42", "title": "Discord",
                 "message_count": 5, "last_active": 200, "started_at": 190},
                {"session_id": "s3", "session_key": "line:C55", "title": "LINE group",
                 "message_count": 1, "last_active": 300, "started_at": 290},
            ]

        def session_count(self, **kw):
            return 3

        def resolve_session_id(self, sid):
            return sid

        def resolve_resume_session_id(self, sid):
            return sid

        def get_messages(self, sid):
            return [{"role": "user", "content": f"hi from {sid}"}]

        def close(self):
            pass

    monkeypatch.setattr(hermes_state, "SessionDB", _FakeDB)

    r = client.get(f"{API}/records")
    assert r.status_code == 200, r.text
    body = r.json()
    keys = [rec["session_key"] for rec in body["records"]]
    assert keys == ["line:U123", "line:C55"]  # discord filtered out
    assert body["total_line_sessions"] == 2


def test_records_session_messages(client, monkeypatch):
    import hermes_state

    class _FakeDB:
        def resolve_session_id(self, sid):
            return sid if sid == "s1" else None

        def resolve_resume_session_id(self, sid):
            return sid

        def get_messages(self, sid):
            return [{"role": "user", "content": "hello"}]

        def close(self):
            pass

    monkeypatch.setattr(hermes_state, "SessionDB", _FakeDB)

    r = client.get(f"{API}/records", params={"session_id": "s1"})
    assert r.status_code == 200
    assert r.json()["messages"][0]["content"] == "hello"

    r = client.get(f"{API}/records", params={"session_id": "nope"})
    assert r.status_code == 404


# ---------------------------------------------------------------------------
# Resolve — falls back to raw id when no token / on error
# ---------------------------------------------------------------------------


def test_resolve_bad_type(client):
    r = client.get(f"{API}/resolve", params={"type": "channel", "id": "U1"})
    assert r.status_code == 400


def test_resolve_no_token_falls_back(client, monkeypatch):
    monkeypatch.delenv("LINE_CHANNEL_ACCESS_TOKEN", raising=False)
    # Also ensure config lookup returns nothing.
    import hermes_cli.config as cfg_mod
    monkeypatch.setattr(cfg_mod, "load_config", lambda: {})
    r = client.get(f"{API}/resolve", params={"type": "user", "id": "U777"})
    assert r.status_code == 200
    body = r.json()
    assert body["name"] == "U777"
    assert body["resolved"] is False


def test_resolve_success_and_cache(client, monkeypatch):
    monkeypatch.setenv("LINE_CHANNEL_ACCESS_TOKEN", "tok")

    calls = {"n": 0}

    class _FakeResp:
        status_code = 200

        def json(self):
            return {"displayName": "Alice"}

    class _FakeAsyncClient:
        def __init__(self, *a, **k):
            pass

        async def __aenter__(self):
            return self

        async def __aexit__(self, *a):
            return False

        async def get(self, url, headers=None):
            calls["n"] += 1
            return _FakeResp()

    import httpx
    monkeypatch.setattr(httpx, "AsyncClient", _FakeAsyncClient)

    r = client.get(f"{API}/resolve", params={"type": "user", "id": "Ualice"})
    assert r.status_code == 200
    body = r.json()
    assert body["name"] == "Alice" and body["resolved"] is True and body["cached"] is False

    # second call hits the TTL cache — no extra HTTP call.
    r2 = client.get(f"{API}/resolve", params={"type": "user", "id": "Ualice"})
    assert r2.json()["cached"] is True
    assert calls["n"] == 1
