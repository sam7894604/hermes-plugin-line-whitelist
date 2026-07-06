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
    # store scope vocabulary (dm/group/room) — the dashboard handler translates
    # the URL's "user" -> "dm" before calling the store, so the fake mirrors the
    # REAL store's scope keys.
    _admin = ("dm", "Uadmin")

    _BUCKET = {"dm": "users", "group": "groups", "room": "rooms"}

    # Pending queue (Phase-A contract). Class-level so every WhitelistStore()
    # the handler constructs shares the same queue within a test.
    _pending = [
        {
            "platform": "line", "source_type": "user", "id": "Upend1",
            "name": "Ada", "first_seen": 10, "last_seen": 20, "count": 3,
            "last_notified": 15, "last_replied": None, "status": "pending",
        },
    ]
    # Records which ids were approved/ignored so tests can assert the calls.
    approved_calls = []
    ignored_calls = []

    @classmethod
    def reset(cls):
        cls._data = {"users": [], "groups": [], "rooms": []}
        cls._pending = [
            {
                "platform": "line", "source_type": "user", "id": "Upend1",
                "name": "Ada", "first_seen": 10, "last_seen": 20, "count": 3,
                "last_notified": 15, "last_replied": None, "status": "pending",
            },
        ]
        cls.approved_calls = []
        cls.ignored_calls = []

    # --- pending queue ---------------------------------------------------
    def list_pending(self):
        return [dict(p) for p in self._pending]

    def approve_pending(self, id, added_by=""):
        type(self).approved_calls.append((id, added_by))
        known = any(p["id"] == id for p in self._pending)
        type(self)._pending = [p for p in self._pending if p["id"] != id]
        if not known:
            # Idempotent: unknown / already-approved id resolves cleanly.
            return {"approved": False, "id": id, "reason": "unknown or already resolved"}
        return {"approved": True, "scope": "user", "id": id}

    def ignore_pending(self, id):
        type(self).ignored_calls.append(id)
        before = len(self._pending)
        type(self)._pending = [p for p in self._pending if p["id"] != id]
        return len(self._pending) < before

    def is_admin(self, id):
        return id == self._admin[1]

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
    repo_root = Path(__file__).resolve().parents[1]
    plugin_file = (
        repo_root / "src" / "plugins" / "platforms" / "line" / "dashboard" / "plugin_api.py"
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


def test_add_user_scope_translates_to_dm(client):
    # Regression: the dashboard sends scope="user" but the store's scope is
    # "dm"; the handler used to pass "user" straight through, so store.add
    # raised "unknown scope: 'user'" -> 400 and no user could ever be added
    # from the dashboard. The handler now translates user->dm.
    r = client.post(f"{API}/whitelist", json={"scope": "user", "id": "Uadd1"})
    assert r.status_code == 200, r.text
    assert r.json()["ok"] is True
    assert "Uadd1" in [u["id"] for u in client.get(f"{API}/whitelist").json()["users"]]
    # and remove of a user works too (user -> dm)
    r = client.delete(f"{API}/whitelist/user/Uadd1")
    assert r.status_code == 200 and r.json()["removed"] is True
    assert client.get(f"{API}/whitelist").json()["users"] == []


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
    body = r.json()
    assert body["ok"] is True
    assert body["removed"] is True          # was present → actually removed
    assert body["already_absent"] is False
    # gone now
    assert client.get(f"{API}/whitelist").json()["rooms"] == []


def test_remove_unknown_is_idempotent_200(client):
    # Removing an id that isn't in the whitelist is idempotent: the desired end
    # state ("not in whitelist") already holds, so it's a 200, not a 404.
    # Regression: a genuine delete used to be mis-reported to the UI as a 404
    # while the config was in fact written (store.remove returned None, which
    # the handler read as "not found").
    r = client.delete(f"{API}/whitelist/user/Unope")
    assert r.status_code == 200
    body = r.json()
    assert body["ok"] is True
    assert body["removed"] is False
    assert body["already_absent"] is True


def test_remove_admin_is_409(client):
    # The fake store refuses to delete the admin entry with WhitelistError,
    # which the handler surfaces as 409.
    r = client.delete(f"{API}/whitelist/user/Uadmin")
    assert r.status_code == 409


# ---------------------------------------------------------------------------
# Authorized — merged store ∪ env overlay, admin + env lock, delete protection
# ---------------------------------------------------------------------------


def _find(rows, id):
    for r in rows:
        if r["id"] == id:
            return r
    return None


def test_authorized_merges_store_and_env(client, monkeypatch):
    # A normal store user, an admin store user, and an env-only user.
    client.post(f"{API}/whitelist", json={"scope": "user", "id": "U123", "note": "me"})
    client.post(f"{API}/whitelist", json={"scope": "user", "id": "Uadmin"})
    monkeypatch.setenv("LINE_ALLOWED_USERS", "Uenv1, U123")  # env-only + overlap
    monkeypatch.setenv("LINE_ALLOWED_GROUPS", "")
    monkeypatch.setenv("LINE_ALLOWED_ROOMS", "")

    r = client.get(f"{API}/authorized")
    assert r.status_code == 200, r.text
    body = r.json()
    assert set(body.keys()) == {"users", "groups", "rooms"}
    users = body["users"]

    normal = _find(users, "U123")
    assert normal is not None
    assert normal["source"] == "store"
    assert normal["admin"] is False
    assert normal["locked"] is False
    assert normal["note"] == "me"
    # U123 is also present in the env overlay → flagged, but stays store-managed.
    assert normal.get("also_in_env") is True

    admin = _find(users, "Uadmin")
    assert admin is not None
    assert admin["source"] == "store"
    assert admin["admin"] is True
    assert admin["locked"] is True

    envonly = _find(users, "Uenv1")
    assert envonly is not None
    assert envonly["source"] == "env"
    assert envonly["locked"] is True
    # env-only ids appear once, not duplicated into the store list.
    assert len([u for u in users if u["id"] == "Uenv1"]) == 1


def test_authorized_env_only_groups_rooms(client, monkeypatch):
    monkeypatch.setenv("LINE_ALLOWED_USERS", "")
    monkeypatch.setenv("LINE_ALLOWED_GROUPS", "Cenv")
    monkeypatch.setenv("LINE_ALLOWED_ROOMS", "Renv")

    body = client.get(f"{API}/authorized").json()
    g = _find(body["groups"], "Cenv")
    assert g is not None and g["source"] == "env" and g["locked"] is True
    room = _find(body["rooms"], "Renv")
    assert room is not None and room["source"] == "env" and room["locked"] is True


def test_delete_env_only_id_is_env_managed_200(client, monkeypatch):
    # An env-overlay id is not in the store; delete does nothing but returns a
    # clean 200 with env_managed:true so the UI can explain it's env-managed.
    monkeypatch.setenv("LINE_ALLOWED_USERS", "Uenv1")
    r = client.delete(f"{API}/whitelist/user/Uenv1")
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["ok"] is True
    assert body["removed"] is False
    assert body["already_absent"] is True
    assert body["env_managed"] is True


def test_delete_plain_absent_id_not_env_managed(client, monkeypatch):
    monkeypatch.delenv("LINE_ALLOWED_USERS", raising=False)
    r = client.delete(f"{API}/whitelist/user/Unope")
    assert r.status_code == 200
    assert r.json()["env_managed"] is False


# ---------------------------------------------------------------------------
# Pending queue — list / approve (idempotent) / ignore
# ---------------------------------------------------------------------------


def test_pending_list(client):
    r = client.get(f"{API}/pending")
    assert r.status_code == 200, r.text
    body = r.json()
    assert "pending" in body
    assert len(body["pending"]) == 1
    p = body["pending"][0]
    assert p["id"] == "Upend1"
    assert p["platform"] == "line"
    assert p["source_type"] == "user"
    assert p["count"] == 3


def test_pending_approve_calls_store(client):
    r = client.post(f"{API}/pending/Upend1/approve", json={"added_by": "sam"})
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["ok"] is True
    assert body["approved"] is True
    assert body["id"] == "Upend1"
    # store.approve_pending was called with the id + added_by.
    assert _FakeStore.approved_calls == [("Upend1", "sam")]
    # gone from the queue now.
    assert client.get(f"{API}/pending").json()["pending"] == []


def test_pending_approve_defaults_added_by(client):
    # No body → added_by falls back to "dashboard".
    r = client.post(f"{API}/pending/Upend1/approve")
    assert r.status_code == 200, r.text
    assert _FakeStore.approved_calls == [("Upend1", "dashboard")]


def test_pending_approve_unknown_is_idempotent_200(client):
    # Approving an id that isn't queued (already approved / never seen) is a
    # clean 200 — NOT a 404. Regression guard: we just fixed a 404-on-success
    # bug on DELETE /whitelist; don't reintroduce it on approve.
    r = client.post(f"{API}/pending/Unknown999/approve")
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["ok"] is True
    assert body["approved"] is False
    assert _FakeStore.approved_calls == [("Unknown999", "dashboard")]


def test_pending_ignore(client):
    r = client.post(f"{API}/pending/Upend1/ignore")
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["ok"] is True
    assert body["ignored"] is True
    assert body["id"] == "Upend1"
    assert _FakeStore.ignored_calls == ["Upend1"]
    # gone from the queue.
    assert client.get(f"{API}/pending").json()["pending"] == []


def test_pending_ignore_unknown_is_200(client):
    # Ignoring an id that isn't queued is still a clean 200 (idempotent).
    r = client.post(f"{API}/pending/Unknown999/ignore")
    assert r.status_code == 200
    assert r.json()["ok"] is True


# ---------------------------------------------------------------------------
# Records — filters SessionDB rows to line: keys
# ---------------------------------------------------------------------------


def test_records_filters_line_sessions(client, monkeypatch):
    import hermes_state

    class _FakeDB:
        def list_sessions_rich(self, **kw):
            # REAL session keys have the platform as a MIDDLE segment
            # (agent:main:line:…), not a prefix — the regression the old
            # startswith("line:") filter missed.
            return [
                {"session_id": "s1", "session_key": "agent:main:line:dm:U123",
                 "title": "LINE chat", "message_count": 3, "last_active": 100, "started_at": 90},
                {"session_id": "s2", "session_key": "agent:main:discord:channel:42",
                 "title": "Discord", "message_count": 5, "last_active": 200, "started_at": 190},
                {"session_id": "s3", "session_key": "agent:main:line:group:C55",
                 "title": "LINE group", "message_count": 1, "last_active": 300, "started_at": 290},
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
    assert keys == ["agent:main:line:dm:U123", "agent:main:line:group:C55"]  # discord filtered out
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
