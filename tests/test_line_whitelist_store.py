"""Tests for the LINE whitelist store + notify helper (Phase 1).

These are hermetic: no real config is touched. A tiny in-memory fake backs
the store's injectable ``loader``/``writer`` so we can also assert hot-reload
semantics (a write is visible on the next query without a fresh store).
"""

from __future__ import annotations

import asyncio
import copy
import time
from typing import Any, Dict

import pytest

from plugins.platforms.line.whitelist_store import WhitelistError, WhitelistStore
from plugins.platforms.line import whitelist_notify


# ---------------------------------------------------------------------------
# fake config backend
# ---------------------------------------------------------------------------
class FakeConfigBackend:
    """Mimics load_config/write_platform_config_field over an in-mem dict.

    ``loader()`` returns a deepcopy (like the real ``load_config``), so the
    store cannot accidentally mutate shared state — mirrors production and
    forces mutations to go through ``writer``.
    """

    def __init__(self, line: Dict[str, Any] | None = None) -> None:
        self.config: Dict[str, Any] = {"platforms": {"line": line or {}}}

    def loader(self) -> Dict[str, Any]:
        return copy.deepcopy(self.config)

    def writer(self, field_key: str, value: Any) -> None:
        self.config.setdefault("platforms", {}).setdefault("line", {})[field_key] = value


def make_store(line: Dict[str, Any] | None = None):
    backend = FakeConfigBackend(line)
    store = WhitelistStore(loader=backend.loader, writer=backend.writer)
    return store, backend


# ---------------------------------------------------------------------------
# scope isolation
# ---------------------------------------------------------------------------
def test_scope_isolation_group_not_allowed_as_dm():
    store, _ = make_store({"whitelist": {"groups": ["Cabc"], "users": [], "rooms": []}})
    assert store.is_allowed("group", "Cabc") is True
    # same id must NOT authorize a dm or room
    assert store.is_allowed("dm", "Cabc") is False
    assert store.is_allowed("room", "Cabc") is False


def test_scope_isolation_user_not_allowed_as_group():
    store, _ = make_store({"whitelist": {"users": ["Uabc"], "groups": [], "rooms": []}})
    assert store.is_allowed("dm", "Uabc") is True
    assert store.is_allowed("group", "Uabc") is False


def test_unknown_source_type_denied():
    store, _ = make_store({"whitelist": {"users": ["Uabc"]}})
    assert store.is_allowed("bogus", "Uabc") is False


def test_missing_config_defaults_empty():
    store, _ = make_store(None)
    assert store.is_allowed("dm", "Uabc") is False
    assert store.list() == {"users": [], "groups": [], "rooms": [], "meta": {}}


# ---------------------------------------------------------------------------
# allow_all short-circuit
# ---------------------------------------------------------------------------
def test_allow_all_users_shortcircuits_dm():
    store, _ = make_store({"allow_all_users": True, "whitelist": {"users": []}})
    assert store.is_allowed("dm", "UneverAdded") is True
    # allow_all is a user-scope flag: does NOT open groups/rooms
    assert store.is_allowed("group", "CneverAdded") is False


# ---------------------------------------------------------------------------
# requires_mention default + override
# ---------------------------------------------------------------------------
def test_requires_mention_default_true():
    store, _ = make_store({})
    assert store.requires_mention("Cabc") is True


def test_requires_mention_global_override():
    store, _ = make_store({"requires_mention": False})
    assert store.requires_mention("Cabc") is False


def test_requires_mention_per_group_override_beats_global():
    store, _ = make_store(
        {
            "requires_mention": False,
            "meta": {"groups": {"Cabc": {"requires_mention": True}}},
        }
    )
    assert store.requires_mention("Cabc") is True  # per-group wins
    assert store.requires_mention("Cother") is False  # falls back to global


# ---------------------------------------------------------------------------
# admin
# ---------------------------------------------------------------------------
def test_is_admin():
    store, _ = make_store({"admins": ["Uadmin"]})
    assert store.is_admin("Uadmin") is True
    assert store.is_admin("Uother") is False


def test_admin_no_delete_raises():
    store, _ = make_store(
        {"admins": ["Uadmin"], "whitelist": {"users": ["Uadmin"]}}
    )
    with pytest.raises(WhitelistError):
        store.remove("dm", "Uadmin")
    # still present after the failed remove
    assert store.is_allowed("dm", "Uadmin") is True


# ---------------------------------------------------------------------------
# add / remove + hot reload
# ---------------------------------------------------------------------------
def test_add_is_visible_on_next_query_hot_reload():
    store, backend = make_store({"whitelist": {"groups": []}})
    assert store.is_allowed("group", "Cnew") is False
    store.add("group", "Cnew", added_by="Uadmin", name="Team")
    # same store instance, no re-instantiation -> proves live re-read
    assert store.is_allowed("group", "Cnew") is True
    # meta recorded
    meta = store.list("group")["meta"]
    assert meta["groups"]["Cnew"]["added_by"] == "Uadmin"
    assert meta["groups"]["Cnew"]["name"] == "Team"
    assert "added_at" in meta["groups"]["Cnew"]


def test_add_dm_maps_to_users():
    store, _ = make_store({})
    store.add("dm", "Uxyz", added_by="Uadmin", note="friend")
    assert store.is_allowed("dm", "Uxyz") is True
    assert "Uxyz" in store.list()["users"]
    assert store.list("dm")["meta"]["users"]["Uxyz"]["note"] == "friend"


def test_add_idempotent_no_duplicate():
    store, _ = make_store({"whitelist": {"groups": ["Cdup"]}})
    store.add("group", "Cdup", added_by="Uadmin")
    assert store.list("group")["groups"].count("Cdup") == 1


def test_add_bad_scope_raises():
    store, _ = make_store({})
    with pytest.raises(WhitelistError):
        store.add("channel", "X", added_by="U")


def test_add_clears_unauthorized_seen():
    store, backend = make_store(
        {"unauthorized_seen": {"Cnew": {"first_seen": 1.0, "last_notified": 1.0, "count": 3}}}
    )
    # precondition: dedup state exists -> would suppress notify
    assert store.should_notify_unauthorized("Cnew") is False
    store.add("group", "Cnew", added_by="Uadmin")
    # unauthorized_seen for this id cleared
    assert "Cnew" not in backend.config["platforms"]["line"].get("unauthorized_seen", {})
    assert store.should_notify_unauthorized("Cnew") is True


def test_remove_deletes_from_scope():
    store, _ = make_store({"whitelist": {"groups": ["Cabc", "Cdef"]}})
    store.remove("group", "Cabc")
    assert store.is_allowed("group", "Cabc") is False
    assert store.is_allowed("group", "Cdef") is True


# ---------------------------------------------------------------------------
# dedup / throttle
# ---------------------------------------------------------------------------
def test_should_notify_once_then_false():
    store, _ = make_store({})
    assert store.should_notify_unauthorized("Cx") is True
    store.mark_unauthorized_notified("Cx")
    assert store.should_notify_unauthorized("Cx") is False  # default: once only


def test_should_notify_window_reallows_after_elapsed():
    past = time.time() - 100
    store, _ = make_store(
        {"unauthorized_seen": {"Cx": {"last_notified": past}}}
    )
    assert store.should_notify_unauthorized("Cx", window_sec=1000) is False
    assert store.should_notify_unauthorized("Cx", window_sec=10) is True


def test_should_reply_respects_window():
    store, _ = make_store({})
    assert store.should_reply_unauthorized("Cx") is True
    store.mark_unauthorized_replied("Cx")
    assert store.should_reply_unauthorized("Cx", window_sec=86400) is False
    # a tiny window re-permits
    assert store.should_reply_unauthorized("Cx", window_sec=0) is True


def test_record_unauthorized_attempt_bumps_count():
    store, backend = make_store({})
    store.record_unauthorized_attempt("Uspam", {"note": "dm stranger"})
    store.record_unauthorized_attempt("Uspam", {"note": "dm stranger"})
    entry = backend.config["platforms"]["line"]["unauthorized_seen"]["Uspam"]
    assert entry["count"] == 2
    assert entry["note"] == "dm stranger"
    assert "first_seen" in entry


def test_clear_unauthorized():
    store, backend = make_store(
        {"unauthorized_seen": {"Cx": {"count": 1}}}
    )
    store.clear_unauthorized("Cx")
    assert "Cx" not in backend.config["platforms"]["line"]["unauthorized_seen"]


# ---------------------------------------------------------------------------
# retention
# ---------------------------------------------------------------------------
def test_retention_default():
    store, _ = make_store({})
    assert store.retention_days() == 3


def test_retention_global_override():
    store, _ = make_store({"retention_days": 7})
    assert store.retention_days() == 7
    assert store.retention_days("Cabc") == 7  # no per-source override -> global


def test_retention_per_source_override():
    store, _ = make_store(
        {"retention_days": 7, "meta": {"groups": {"Cabc": {"retention_days": 30}}}}
    )
    assert store.retention_days("Cabc") == 30
    assert store.retention_days("Cother") == 7


# ---------------------------------------------------------------------------
# notify helper
# ---------------------------------------------------------------------------
class FakeHomeChannel:
    def __init__(self, chat_id):
        self.chat_id = chat_id


class FakeGatewayConfig:
    def __init__(self, chat_id="Uadmin_home"):
        self._chat_id = chat_id
        self.platforms = {}

    def get_home_channel(self, platform):
        return FakeHomeChannel(self._chat_id)


def _run(coro):
    return asyncio.get_event_loop().run_until_complete(coro)


def test_notify_unauthorized_sends_once(monkeypatch):
    store, backend = make_store({})
    sent = []

    async def fake_send(platform, pconfig, chat_id, message, **kw):
        sent.append((chat_id, message))

    import tools.send_message_tool as smt

    monkeypatch.setattr(smt, "_send_to_platform", fake_send, raising=False)

    cfg = FakeGatewayConfig("Uadmin_home")

    ok = _run(
        whitelist_notify.notify_unauthorized(
            store, cfg, source_type="group", source_id="Cnew", display="Team"
        )
    )
    assert ok is True
    assert len(sent) == 1
    assert sent[0][0] == "Uadmin_home"

    # second call is deduped -> no send
    ok2 = _run(
        whitelist_notify.notify_unauthorized(
            store, cfg, source_type="group", source_id="Cnew"
        )
    )
    assert ok2 is False
    assert len(sent) == 1


def test_notify_unauthorized_uses_explicit_override_target(monkeypatch):
    store, _ = make_store({"unauthorized_notify": "Uoverride"})
    sent = []

    async def fake_send(platform, pconfig, chat_id, message, **kw):
        sent.append(chat_id)

    import tools.send_message_tool as smt

    monkeypatch.setattr(smt, "_send_to_platform", fake_send, raising=False)

    ok = _run(
        whitelist_notify.notify_unauthorized(
            store, FakeGatewayConfig(), source_type="dm", source_id="Ustranger"
        )
    )
    assert ok is True
    assert sent == ["Uoverride"]


def test_notify_unauthorized_cross_platform_target(monkeypatch):
    # ``telegram:521703862`` must route via the Telegram platform to chat
    # ``521703862`` — not send a LINE message to a literal "telegram:..." id.
    store, _ = make_store({"unauthorized_notify": "telegram:521703862"})
    sent = []

    async def fake_send(platform, pconfig, chat_id, message, **kw):
        sent.append((getattr(platform, "value", str(platform)), chat_id))

    import tools.send_message_tool as smt

    monkeypatch.setattr(smt, "_send_to_platform", fake_send, raising=False)

    ok = _run(
        whitelist_notify.notify_unauthorized(
            store, FakeGatewayConfig(), source_type="group", source_id="Cnew"
        )
    )
    assert ok is True
    assert len(sent) == 1
    plat, chat = sent[0]
    assert "telegram" in str(plat).lower()
    assert chat == "521703862"


def test_notify_unauthorized_swallows_send_errors(monkeypatch):
    store, _ = make_store({})

    async def boom(*a, **k):
        raise RuntimeError("network down")

    import tools.send_message_tool as smt

    monkeypatch.setattr(smt, "_send_to_platform", boom, raising=False)

    # must not raise, returns False
    ok = _run(
        whitelist_notify.notify_unauthorized(
            store, FakeGatewayConfig(), source_type="group", source_id="Cx"
        )
    )
    assert ok is False


def test_notify_unauthorized_no_target_returns_false(monkeypatch):
    store, _ = make_store({})

    class NoHomeConfig:
        platforms = {}

        def get_home_channel(self, platform):
            return None

    ok = _run(
        whitelist_notify.notify_unauthorized(
            store, NoHomeConfig(), source_type="group", source_id="Cx"
        )
    )
    assert ok is False
