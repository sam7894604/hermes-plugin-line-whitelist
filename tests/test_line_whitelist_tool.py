"""Tests for tools/line_whitelist_tool.py (Phase 3 agent approval tool).

The Phase-1 WhitelistStore may not exist in this worktree, so we MOCK it
entirely: the tool reaches the store only through ``_get_store()`` (deferred
factory) and the caller identity only through ``_caller_user_id()``. Both are
monkeypatched here, so these tests exercise the tool's permission gating,
argument validation, and store-call wiring without importing Phase 1.
"""

import json

import pytest

from tools import line_whitelist_tool as lwt


class _WhitelistError(Exception):
    """Stand-in for plugins.platforms.line.whitelist_store.WhitelistError."""


class FakeStore:
    """In-memory mock of the Phase-1 WhitelistStore surface."""

    def __init__(self, admins=("admin-user",), entries=None):
        self._admins = set(admins)
        self._entries = list(entries or [])
        self.add_calls = []
        self.remove_calls = []

    def is_admin(self, user_id):
        return user_id in self._admins

    def list(self, scope=None):
        if scope is None:
            return list(self._entries)
        return [e for e in self._entries if e.get("scope") == scope]

    def add(self, scope, id, added_by=None, note=None):
        self.add_calls.append(
            {"scope": scope, "id": id, "added_by": added_by, "note": note}
        )
        entry = {"scope": scope, "id": id, "added_by": added_by, "note": note}
        self._entries.append(entry)
        return entry

    def remove(self, scope, id):
        self.remove_calls.append({"scope": scope, "id": id})
        # Emulate the store's admin-guard: removing an admin id raises.
        if id == "admin-user":
            raise _WhitelistError("cannot remove an admin from the whitelist")
        before = len(self._entries)
        self._entries = [
            e for e in self._entries
            if not (e.get("scope") == scope and e.get("id") == id)
        ]
        return len(self._entries) < before


@pytest.fixture
def store():
    return FakeStore()


@pytest.fixture(autouse=True)
def _patch_store_and_error(monkeypatch, store):
    monkeypatch.setattr(lwt, "_get_store", lambda: store)
    monkeypatch.setattr(lwt, "_whitelist_error_cls", lambda: _WhitelistError)
    # Default caller = admin; individual tests override for the non-admin path.
    monkeypatch.setattr(lwt, "_caller_user_id", lambda: "admin-user")
    yield


def _as_admin(monkeypatch):
    monkeypatch.setattr(lwt, "_caller_user_id", lambda: "admin-user")


def _as_nonadmin(monkeypatch):
    monkeypatch.setattr(lwt, "_caller_user_id", lambda: "rando-user")


# --------------------------------------------------------------------------
# approve
# --------------------------------------------------------------------------

def test_admin_approve_calls_store_add_with_right_args(monkeypatch, store):
    _as_admin(monkeypatch)
    out = json.loads(
        lwt.line_whitelist("approve", scope="group", id="C123", note="team chat")
    )
    assert out["success"] is True
    assert len(store.add_calls) == 1
    call = store.add_calls[0]
    assert call == {
        "scope": "group",
        "id": "C123",
        "added_by": "admin-user",
        "note": "team chat",
    }


def test_nonadmin_approve_is_refused(monkeypatch, store):
    _as_nonadmin(monkeypatch)
    out = json.loads(lwt.line_whitelist("approve", scope="group", id="C123"))
    assert out.get("success") is False
    assert out.get("permission_denied") is True
    assert store.add_calls == []  # store never touched


def test_approve_scope_is_normalized(monkeypatch, store):
    _as_admin(monkeypatch)
    out = json.loads(lwt.line_whitelist("approve", scope="  GROUP ", id=" C9 "))
    assert out["success"] is True
    assert store.add_calls[0]["scope"] == "group"
    assert store.add_calls[0]["id"] == "C9"


# --------------------------------------------------------------------------
# remove
# --------------------------------------------------------------------------

def test_admin_remove_admin_id_surfaces_error(monkeypatch, store):
    _as_admin(monkeypatch)
    out = json.loads(lwt.line_whitelist("remove", scope="dm", id="admin-user"))
    assert out.get("success") is False
    assert "cannot remove an admin" in out["error"].lower()


def test_admin_remove_existing_entry(monkeypatch):
    _as_admin(monkeypatch)
    s = FakeStore(entries=[{"scope": "group", "id": "C123"}])
    monkeypatch.setattr(lwt, "_get_store", lambda: s)
    out = json.loads(lwt.line_whitelist("remove", scope="group", id="C123"))
    assert out["success"] is True
    assert s.remove_calls == [{"scope": "group", "id": "C123"}]


def test_remove_missing_entry_reports_not_found(monkeypatch):
    _as_admin(monkeypatch)
    s = FakeStore(entries=[])
    monkeypatch.setattr(lwt, "_get_store", lambda: s)
    out = json.loads(lwt.line_whitelist("remove", scope="group", id="nope"))
    assert out.get("success") is False
    assert "nope" in out["error"]


def test_nonadmin_remove_is_refused(monkeypatch, store):
    _as_nonadmin(monkeypatch)
    out = json.loads(lwt.line_whitelist("remove", scope="dm", id="C1"))
    assert out.get("success") is False
    assert store.remove_calls == []


# --------------------------------------------------------------------------
# list
# --------------------------------------------------------------------------

def test_admin_list_returns_store_data(monkeypatch):
    _as_admin(monkeypatch)
    entries = [{"scope": "group", "id": "C1"}, {"scope": "dm", "id": "U2"}]
    s = FakeStore(entries=entries)
    monkeypatch.setattr(lwt, "_get_store", lambda: s)
    out = json.loads(lwt.line_whitelist("list"))
    assert out["success"] is True
    assert out["count"] == 2
    assert out["entries"] == entries


def test_list_scope_filter(monkeypatch):
    _as_admin(monkeypatch)
    entries = [{"scope": "group", "id": "C1"}, {"scope": "dm", "id": "U2"}]
    s = FakeStore(entries=entries)
    monkeypatch.setattr(lwt, "_get_store", lambda: s)
    out = json.loads(lwt.line_whitelist("list", scope="dm"))
    assert out["success"] is True
    assert out["entries"] == [{"scope": "dm", "id": "U2"}]


def test_nonadmin_list_is_refused(monkeypatch):
    _as_nonadmin(monkeypatch)
    out = json.loads(lwt.line_whitelist("list"))
    assert out.get("success") is False
    assert out.get("permission_denied") is True


# --------------------------------------------------------------------------
# validation
# --------------------------------------------------------------------------

def test_bad_action_handled(monkeypatch, store):
    _as_admin(monkeypatch)
    out = json.loads(lwt.line_whitelist("frobnicate"))
    assert out.get("success") is False
    assert "unknown action" in out["error"].lower()


def test_bad_scope_handled(monkeypatch, store):
    _as_admin(monkeypatch)
    out = json.loads(lwt.line_whitelist("approve", scope="galaxy", id="C1"))
    assert out.get("success") is False
    assert "invalid scope" in out["error"].lower()
    assert store.add_calls == []


def test_approve_missing_id_handled(monkeypatch, store):
    _as_admin(monkeypatch)
    out = json.loads(lwt.line_whitelist("approve", scope="group"))
    assert out.get("success") is False
    assert "id" in out["error"].lower()
    assert store.add_calls == []


def test_missing_scope_handled(monkeypatch, store):
    _as_admin(monkeypatch)
    out = json.loads(lwt.line_whitelist("approve", id="C1"))
    assert out.get("success") is False
    assert "scope" in out["error"].lower()


# --------------------------------------------------------------------------
# non-admin proposal flow (optional feature)
# --------------------------------------------------------------------------

def test_nonadmin_propose_stages_then_admin_approves(monkeypatch, store, tmp_path):
    # Redirect the pending dir into tmp so we don't touch the real HERMES_HOME.
    monkeypatch.setattr(lwt, "_pending_dir", lambda: tmp_path / "line_wl")

    _as_nonadmin(monkeypatch)
    proposed = json.loads(
        lwt.line_whitelist("propose", scope="group", id="C777", note="please add")
    )
    assert proposed["success"] is True
    assert proposed["staged"] is True
    pid = proposed["pending_id"]
    assert store.add_calls == []  # not whitelisted yet

    # Admin lists then approves.
    _as_admin(monkeypatch)
    listed = json.loads(lwt.line_whitelist("list_pending"))
    assert listed["count"] == 1
    approved = json.loads(lwt.line_whitelist("approve_pending", pending_id=pid))
    assert approved["success"] is True
    assert store.add_calls == [
        {"scope": "group", "id": "C777", "added_by": "admin-user", "note": "please add"}
    ]
    # Proposal consumed.
    assert json.loads(lwt.line_whitelist("list_pending"))["count"] == 0


def test_nonadmin_cannot_list_pending(monkeypatch, tmp_path):
    monkeypatch.setattr(lwt, "_pending_dir", lambda: tmp_path / "line_wl")
    _as_nonadmin(monkeypatch)
    out = json.loads(lwt.line_whitelist("list_pending"))
    assert out.get("success") is False
    assert out.get("permission_denied") is True


def test_admin_reject_proposal(monkeypatch, store, tmp_path):
    monkeypatch.setattr(lwt, "_pending_dir", lambda: tmp_path / "line_wl")
    _as_nonadmin(monkeypatch)
    pid = json.loads(
        lwt.line_whitelist("propose", scope="dm", id="U1")
    )["pending_id"]
    _as_admin(monkeypatch)
    out = json.loads(lwt.line_whitelist("reject", pending_id=pid))
    assert out["success"] is True
    assert json.loads(lwt.line_whitelist("list_pending"))["count"] == 0
