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


def test_is_notify_target_and_card_admin():
    # Cross-platform card admin: the unauthorized_notify recipient can act on
    # the decision card even though their platform id isn't a LINE admin id.
    store, _ = make_store(
        {"admins": ["Uddf"], "unauthorized_notify": "telegram:521703862"}
    )
    assert store.is_notify_target("telegram", "521703862") is True
    assert store.is_notify_target("telegram", "999") is False
    assert store.is_notify_target("discord", "521703862") is False  # wrong platform
    # is_card_admin = LINE admin OR notify recipient on that platform
    assert store.is_card_admin("telegram", "521703862") is True     # notify recipient
    assert store.is_card_admin("telegram", "Uddf") is True          # LINE admin
    assert store.is_card_admin("telegram", "Ustranger") is False
    # no notify target configured -> only LINE admins pass
    store2, _ = make_store({"admins": ["Uddf"]})
    assert store2.is_notify_target("telegram", "521703862") is False
    assert store2.is_card_admin("telegram", "521703862") is False


def test_card_admins_table_and_is_card_admin():
    store, _ = make_store({
        "admins": ["Uddf"],
        "card_admins": {"line": ["Uddf"], "telegram": ["521703862"], "discord": ["Dc1"]},
    })
    assert store.card_admins()["telegram"] == ["521703862"]
    # in the per-platform table -> admin, even if not a LINE admin / notify target
    assert store.is_card_admin("telegram", "521703862") is True
    assert store.is_card_admin("discord", "Dc1") is True
    assert store.is_card_admin("telegram", "Uddf") is True   # LINE admin fallback
    assert store.is_card_admin("telegram", "stranger") is False


def test_set_card_admins_persists():
    store, backend = make_store({})
    store.set_card_admins("telegram", ["111", "222", "  "])   # blanks dropped
    assert store.card_admins()["telegram"] == ["111", "222"]
    assert store.is_card_admin("telegram", "222") is True


def test_get_and_set_settings():
    store, _ = make_store({"requires_mention": False, "retention_days": 7})
    s = store.get_settings()
    assert s["requires_mention"] is False and s["retention_days"] == 7
    assert s["observe_unmentioned"] is True   # default
    # write an editable setting -> hot reload visible
    store.set_setting("retention_days", 14)
    assert store.get_settings()["retention_days"] == 14
    # non-editable key rejected
    import pytest as _pytest
    with _pytest.raises(WhitelistError):
        store.set_setting("admins", ["Uhack"])


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
    # remove of a present id returns True (was present, now removed)
    assert store.remove("group", "Cabc") is True
    assert store.is_allowed("group", "Cabc") is False
    assert store.is_allowed("group", "Cdef") is True


def test_remove_absent_is_idempotent_false():
    # Removing something that isn't there is a no-op that returns False — never
    # an error. (Regression: the store used to return None here, which the
    # Dashboard handler mis-read as a 404 even on a successful delete.)
    store, _ = make_store({"whitelist": {"groups": ["Cdef"]}})
    assert store.remove("group", "Cnope") is False
    # and a real removal still reports True + shrinks the list
    assert store.remove("group", "Cdef") is True
    assert store.list()["groups"] == []


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
# pending queue: record_attempt upsert
# ---------------------------------------------------------------------------
def _seen(backend):
    return backend.config["platforms"]["line"].get("unauthorized_seen", {})


def test_record_attempt_new_entry_sets_defaults():
    store, backend = make_store({})
    store.record_attempt("Cnew", source_type="group", name="Team")
    entry = _seen(backend)["Cnew"]
    assert entry["status"] == "pending"
    assert entry["count"] == 1
    assert entry["platform"] == "line"
    assert entry["source_type"] == "group"
    assert entry["name"] == "Team"
    assert "first_seen" in entry
    assert "last_seen" in entry


def test_record_attempt_repeat_bumps_count_keeps_first_seen():
    store, backend = make_store({})
    store.record_attempt("Cx", source_type="group")
    first = _seen(backend)["Cx"]["first_seen"]
    time.sleep(0.01)
    store.record_attempt("Cx", source_type="group")
    entry = _seen(backend)["Cx"]
    assert entry["count"] == 2
    assert entry["first_seen"] == first  # unchanged
    assert entry["last_seen"] >= first  # advanced


def test_record_attempt_name_non_blank_overwrite_rule():
    store, backend = make_store({})
    store.record_attempt("Cx", source_type="group", name="Real Name")
    # a subsequent blank name must NOT wipe the resolved name
    store.record_attempt("Cx", source_type="group", name="")
    assert _seen(backend)["Cx"]["name"] == "Real Name"
    # a new non-blank name overwrites
    store.record_attempt("Cx", source_type="group", name="Renamed")
    assert _seen(backend)["Cx"]["name"] == "Renamed"


def test_record_attempt_does_not_touch_notify_reply():
    store, backend = make_store({})
    store.mark_unauthorized_notified("Cx")
    ln = _seen(backend)["Cx"].get("last_notified")
    store.record_attempt("Cx", source_type="group")
    assert _seen(backend)["Cx"]["last_notified"] == ln


# ---------------------------------------------------------------------------
# pending queue: list_pending
# ---------------------------------------------------------------------------
def test_list_pending_excludes_ignored_and_whitelisted_sorted():
    store, _ = make_store({"whitelist": {"groups": ["Cwl"]}})
    store.record_attempt("Cold", source_type="group", name="Old")
    time.sleep(0.01)
    store.record_attempt("Cnew", source_type="group", name="New")
    # whitelisted id present in seen but should be filtered out
    store.record_attempt("Cwl", source_type="group")
    # ignored id filtered out
    store.record_attempt("Cign", source_type="group")
    store.ignore_pending("Cign")

    pending = store.list_pending()
    ids = [p["id"] for p in pending]
    assert ids == ["Cnew", "Cold"]  # newest first, ignored+whitelisted excluded
    assert pending[0]["name"] == "New"
    assert set(pending[0].keys()) == {
        "platform", "source_type", "id", "name", "first_seen",
        "last_seen", "count", "last_notified", "last_replied", "status",
    }


# ---------------------------------------------------------------------------
# pending queue: approve_pending
# ---------------------------------------------------------------------------
def test_approve_pending_adds_to_scope_and_clears():
    store, backend = make_store({})
    store.record_attempt("Croom1", source_type="room", name="RoomX")
    res = store.approve_pending("Croom1", added_by="Uadmin")
    assert res == {"approved": True, "scope": "room", "id": "Croom1"}
    assert store.is_allowed("room", "Croom1") is True
    # stored name carried into meta
    assert store.list("room")["meta"]["rooms"]["Croom1"]["name"] == "RoomX"
    # cleared from pending queue
    assert "Croom1" not in _seen(backend)
    assert store.list_pending() == []


def test_approve_pending_unknown_returns_false():
    store, _ = make_store({})
    res = store.approve_pending("Cnope")
    assert res == {"approved": False, "reason": "not found", "id": "Cnope"}


def test_approve_pending_dm_maps_to_users():
    store, _ = make_store({})
    store.record_attempt("Uxyz", source_type="dm")
    res = store.approve_pending("Uxyz", added_by="Uadmin")
    assert res["approved"] is True and res["scope"] == "dm"
    assert "Uxyz" in store.list()["users"]


def test_approve_pending_legacy_entry_infers_scope_from_id():
    # Regression: a legacy unauthorized_seen row (written before the pending
    # feature) has NO source_type — the dashboard "approve" button was a no-op
    # (approved:False, nothing added). Now the scope is inferred from the LINE
    # id prefix (C -> group), so approve actually whitelists it.
    store, backend = make_store(
        {"unauthorized_seen": {
            "Cbb218legacyid": {"first_seen": 1.0, "last_notified": 1.0, "count": 1}
        }}
    )
    # it shows in pending with an inferred source_type
    pend = store.list_pending()
    assert len(pend) == 1 and pend[0]["id"] == "Cbb218legacyid"
    assert pend[0]["source_type"] == "group"
    # and approve now works (was False before the fix)
    res = store.approve_pending("Cbb218legacyid", added_by="Uadmin")
    assert res == {"approved": True, "scope": "group", "id": "Cbb218legacyid"}
    assert store.is_allowed("group", "Cbb218legacyid") is True
    assert store.list_pending() == []       # cleared from pending


def test_approve_pending_unresolvable_id_returns_false():
    # An id with no recognisable prefix and no source_type stays a clean no-op.
    store, _ = make_store(
        {"unauthorized_seen": {"weirdid": {"first_seen": 1.0, "count": 1}}}
    )
    res = store.approve_pending("weirdid")
    assert res["approved"] is False and res["reason"] == "unresolved scope"


# ---------------------------------------------------------------------------
# pending queue: ignore_pending
# ---------------------------------------------------------------------------
def test_ignore_pending_suppresses_notify_and_list():
    store, backend = make_store({})
    store.record_attempt("Cspam", source_type="group")
    assert store.should_notify_unauthorized("Cspam") is True
    assert store.ignore_pending("Cspam") is True
    assert _seen(backend)["Cspam"]["status"] == "ignored"
    assert store.should_notify_unauthorized("Cspam") is False
    assert [p["id"] for p in store.list_pending()] == []


def test_ignore_pending_creates_minimal_entry_when_absent():
    store, backend = make_store({})
    assert store.ignore_pending("Cghost") is True
    assert _seen(backend)["Cghost"]["status"] == "ignored"
    assert store.should_notify_unauthorized("Cghost") is False


# ---------------------------------------------------------------------------
# pending queue: set_name backfill
# ---------------------------------------------------------------------------
def test_set_name_backfills_existing_entry():
    store, backend = make_store({})
    store.record_attempt("Cx", source_type="group")
    store.set_name("Cx", "Resolved Name")
    assert _seen(backend)["Cx"]["name"] == "Resolved Name"


def test_set_name_blank_is_noop():
    store, backend = make_store({})
    store.record_attempt("Cx", source_type="group", name="Keep")
    store.set_name("Cx", "")
    assert _seen(backend)["Cx"]["name"] == "Keep"


def test_record_unauthorized_attempt_delegates_and_preserves_meta():
    store, backend = make_store({})
    store.record_unauthorized_attempt("Uspam", {"note": "dm stranger", "name": "Bob"})
    entry = _seen(backend)["Uspam"]
    assert entry["source_type"] == "dm"  # forced by delegate
    assert entry["name"] == "Bob"
    assert entry["note"] == "dm stranger"  # extra meta preserved
    assert entry["count"] == 1
    assert entry["status"] == "pending"


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
