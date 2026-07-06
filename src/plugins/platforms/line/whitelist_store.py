"""LINE whitelist subsystem — persistent access-control store (Phase 1).

The :class:`WhitelistStore` is the single source of truth for LINE access
control. It is shared verbatim by the Dashboard REST routes and the agent
approval tool, so the public method signatures below are contractual — do
not change them without coordinating every caller.

Design invariants
-----------------
* **Hot reload.** Every query re-reads the live config through the injected
  ``loader`` (defaults to :func:`hermes_cli.config.load_config`, which
  invalidates its cache on file ``(mtime_ns, size)`` change). The store keeps
  **no** long-lived snapshot of the whitelist.
* **Scope isolation.** ``dm``/``group``/``room`` map to disjoint config lists
  (``whitelist.users`` / ``whitelist.groups`` / ``whitelist.rooms``). An id
  present in one scope grants nothing in another.
* **Defensive defaults.** Missing config keys degrade to empty structures,
  never raise. Only explicit contract violations (admin no-delete, bad scope)
  raise :class:`WhitelistError`.

Config schema lives under ``platforms.line`` — see the subsystem spec.
"""

from __future__ import annotations

import time
from typing import Any, Callable, Dict, Optional

__all__ = ["WhitelistError", "WhitelistStore"]


# scope -> whitelist list key
_SCOPE_TO_LISTKEY: Dict[str, str] = {
    "dm": "users",
    "group": "groups",
    "room": "rooms",
}

# source_type (used by is_allowed) -> whitelist list key
_SOURCETYPE_TO_LISTKEY: Dict[str, str] = {
    "dm": "users",
    "group": "groups",
    "room": "rooms",
}

# scope -> meta bucket key
_SCOPE_TO_METAKEY: Dict[str, str] = {
    "dm": "users",
    "group": "groups",
    "room": "rooms",
}

# LINE id prefix -> store scope. Fallback for pending entries that predate the
# ``source_type`` field (e.g. a legacy ``unauthorized_seen`` row written before
# the pending-queue feature): U… = user/DM, C… = group, R… = room.
_ID_PREFIX_TO_SCOPE: Dict[str, str] = {"U": "dm", "C": "group", "R": "room"}


def _infer_scope_from_id(source_id: str) -> str:
    """Best-effort scope from a LINE id prefix; '' if unrecognised."""
    return _ID_PREFIX_TO_SCOPE.get((source_id or "")[:1].upper(), "")

_DEFAULT_RETENTION_DAYS = 3
_DEFAULT_REQUIRES_MENTION = True


class WhitelistError(Exception):
    """Raised on contract violations (bad scope, admin no-delete, etc.)."""


class WhitelistStore:
    """Access-control store for the LINE platform.

    Parameters
    ----------
    config_path:
        Optional path hint. Retained for API symmetry / logging; the actual
        read path is the injected ``loader`` (or the default global loader,
        which resolves the path itself). Not used to bypass the loader.
    loader:
        Zero-arg callable returning the full config dict. Defaults to
        :func:`hermes_cli.config.load_config`. Injected in tests to avoid
        touching the real config and to defeat mtime caching.
    writer:
        Optional ``(field_key, value) -> None`` callable used to persist a
        single ``platforms.line.<field_key>``. Defaults to a thin wrapper over
        :func:`hermes_cli.config.write_platform_config_field`. Injected in
        tests so mutations land in the same fake config the loader reads.
        (Keyword-only extension to the spec's ``__init__``; all query/mutation
        signatures are unchanged.)
    """

    def __init__(
        self,
        config_path: Optional[str] = None,
        *,
        loader: Optional[Callable[[], Dict[str, Any]]] = None,
        writer: Optional[Callable[[str, Any], None]] = None,
    ) -> None:
        self._config_path = config_path
        self._loader = loader
        self._writer = writer

    # ------------------------------------------------------------------
    # config access
    # ------------------------------------------------------------------
    def _load(self) -> Dict[str, Any]:
        loader = self._loader
        if loader is None:
            from hermes_cli.config import load_config

            loader = load_config
        try:
            cfg = loader() or {}
        except Exception:
            return {}
        return cfg if isinstance(cfg, dict) else {}

    def _line_config(self) -> Dict[str, Any]:
        cfg = self._load()
        platforms = cfg.get("platforms")
        if not isinstance(platforms, dict):
            return {}
        line = platforms.get("line")
        return line if isinstance(line, dict) else {}

    def _write_field(self, field_key: str, value: Any) -> None:
        writer = self._writer
        if writer is None:
            from hermes_cli.config import write_platform_config_field

            def writer(fk: str, v: Any) -> None:  # noqa: E306
                write_platform_config_field("line", fk, v)

        writer(field_key, value)

    # ------------------------------------------------------------------
    # helpers
    # ------------------------------------------------------------------
    @staticmethod
    def _as_dict(value: Any) -> Dict[str, Any]:
        return value if isinstance(value, dict) else {}

    @staticmethod
    def _as_list(value: Any) -> list:
        return list(value) if isinstance(value, (list, tuple)) else []

    def _whitelist(self) -> Dict[str, Any]:
        return self._as_dict(self._line_config().get("whitelist"))

    def _meta(self) -> Dict[str, Any]:
        return self._as_dict(self._line_config().get("meta"))

    # ------------------------------------------------------------------
    # queries
    # ------------------------------------------------------------------
    def is_allowed(self, source_type: str, source_id: str) -> bool:
        """Return True if ``source_id`` is authorized for ``source_type``.

        ``source_type`` in {"dm","group","room"}. ``allow_all_users`` short
        circuits for *dm* sources only (it is a user-scope flag). Scope is
        isolated: a group id in ``whitelist.groups`` does not authorize a dm.
        """
        line = self._line_config()
        if source_type == "dm" and bool(line.get("allow_all_users", False)):
            return True
        list_key = _SOURCETYPE_TO_LISTKEY.get(source_type)
        if list_key is None:
            return False
        return source_id in self._as_list(self._whitelist().get(list_key))

    def requires_mention(self, group_id: str) -> bool:
        """Whether a group requires an @mention to engage the agent.

        Global default is ``platforms.line.requires_mention`` (falling back to
        True), overridable per-group via ``meta.groups[gid].requires_mention``.
        """
        line = self._line_config()
        default = bool(line.get("requires_mention", _DEFAULT_REQUIRES_MENTION))
        groups_meta = self._as_dict(self._meta().get("groups"))
        entry = self._as_dict(groups_meta.get(group_id))
        if "requires_mention" in entry:
            return bool(entry["requires_mention"])
        return default

    def is_admin(self, user_id: str) -> bool:
        return user_id in self._as_list(self._line_config().get("admins"))

    def is_notify_target(self, platform: str, caller_id: str) -> bool:
        """True if ``caller_id`` is the configured ``unauthorized_notify``
        recipient on ``platform``.

        Interactive decision cards are delivered to (and tapped from) the
        *notify* platform — e.g. Telegram — so a button tap arrives with the
        Telegram user id, which will never match the LINE ``admins`` list.
        The person who RECEIVES the card is authorized to act on it, so the
        card callback treats the notify recipient as an admin for that platform.
        Target format: ``"telegram:521703862"`` (``"<platform>:<chat_id>"``).
        """
        if not caller_id:
            return False
        target = str(self._line_config().get("unauthorized_notify") or "")
        if ":" in target:
            p, cid = target.split(":", 1)
            return p == platform and cid == str(caller_id)
        return False

    def card_admins(self) -> Dict[str, list]:
        """Per-platform admin id lists for interactive-card authorization.

        Shape: ``{"line": ["U…"], "telegram": ["521703862"], "discord": [...]}``
        under ``platforms.line.card_admins``. Managed from the Dashboard Settings
        panel. Empty/absent platforms fall back to the legacy checks.
        """
        raw = self._as_dict(self._line_config().get("card_admins"))
        return {k: self._as_list(v) for k, v in raw.items()}

    def set_card_admins(self, platform: str, ids: list) -> None:
        """Replace the admin id list for ``platform`` and persist."""
        table = self.card_admins()
        table[str(platform)] = [str(i).strip() for i in (ids or []) if str(i).strip()]
        self._write_field("card_admins", table)

    def is_card_admin(self, platform: str, caller_id: str) -> bool:
        """Admin check for interactive-card callbacks.

        Authorized if ``caller_id`` is in the per-platform ``card_admins`` table
        (the managed mechanism), OR a LINE whitelist admin, OR the configured
        notify recipient on the tapping ``platform`` (legacy fallbacks).
        """
        if not caller_id:
            return False
        if str(caller_id) in self.card_admins().get(str(platform), []):
            return True
        return self.is_admin(caller_id) or self.is_notify_target(platform, caller_id)

    # ------------------------------------------------------------------
    # managed settings (Dashboard Settings panel)
    # ------------------------------------------------------------------
    # Config keys the Settings panel may write (config-backed, hot-reload).
    _EDITABLE_SETTINGS = (
        "requires_mention", "unauthorized_notify", "retention_days",
        "observe_unmentioned", "allow_all_users", "media",
    )

    def get_settings(self) -> Dict[str, Any]:
        """Current values of the config-backed, Dashboard-editable settings."""
        line = self._line_config()
        return {
            "requires_mention": bool(line.get("requires_mention", _DEFAULT_REQUIRES_MENTION)),
            "unauthorized_notify": line.get("unauthorized_notify"),
            "retention_days": int(line.get("retention_days", _DEFAULT_RETENTION_DAYS) or _DEFAULT_RETENTION_DAYS),
            "observe_unmentioned": bool(line.get("observe_unmentioned", True)),
            "allow_all_users": bool(line.get("allow_all_users", False)),
            "media": self._as_dict(line.get("media")) or {
                "keep_types": ["image", "file"], "drop_types": ["video", "audio"]
            },
        }

    def set_setting(self, key: str, value: Any) -> None:
        """Write a single editable ``platforms.line.<key>`` and persist.

        Raises :class:`WhitelistError` for a key not in the editable allowlist
        (so the endpoint can't be used to write arbitrary config paths).
        """
        if key not in self._EDITABLE_SETTINGS:
            raise WhitelistError(f"setting not editable: {key!r}")
        self._write_field(key, value)

    def list(self, scope: Optional[str] = None) -> dict:
        """Return the whitelist plus meta.

        With ``scope`` (dm|group|room) returns only that scope's list under its
        canonical key. Without, returns all three keys. Meta is always attached
        under ``"meta"``.
        """
        wl = self._whitelist()
        result: Dict[str, Any] = {}
        if scope is None:
            for list_key in ("users", "groups", "rooms"):
                result[list_key] = self._as_list(wl.get(list_key))
        else:
            list_key = _SCOPE_TO_LISTKEY.get(scope)
            if list_key is None:
                raise WhitelistError(f"unknown scope: {scope!r}")
            result[list_key] = self._as_list(wl.get(list_key))
        result["meta"] = self._meta()
        return result

    # ------------------------------------------------------------------
    # mutations
    # ------------------------------------------------------------------
    def add(
        self,
        scope: str,
        source_id: str,
        *,
        added_by: str,
        note: str = "",
        name: str = "",
    ) -> None:
        """Add ``source_id`` to ``scope`` (dm->users). Idempotent.

        Records a meta entry and clears any pending unauthorized dedup state
        for this id (a freshly authorized source starts clean).
        """
        list_key = _SCOPE_TO_LISTKEY.get(scope)
        if list_key is None:
            raise WhitelistError(f"unknown scope: {scope!r}")

        wl = self._whitelist()
        current = self._as_list(wl.get(list_key))
        if source_id not in current:
            current.append(source_id)
            wl[list_key] = current
            self._write_field("whitelist", wl)

        # meta entry
        meta = self._meta()
        meta_key = _SCOPE_TO_METAKEY[scope]
        bucket = self._as_dict(meta.get(meta_key))
        entry = self._as_dict(bucket.get(source_id))
        entry.setdefault("added_by", added_by)
        entry.setdefault("added_at", time.time())
        if name:
            entry["name"] = name
        if note:
            entry["note"] = note
        bucket[source_id] = entry
        meta[meta_key] = bucket
        self._write_field("meta", meta)

        # a newly authorized source should not carry stale reject state
        self.clear_unauthorized(source_id)

    def remove(self, scope: str, source_id: str) -> bool:
        """Remove ``source_id`` from ``scope``.

        Idempotent: returns ``True`` if the id was present (and has now been
        removed + persisted), ``False`` if it was already absent (a no-op — the
        desired end state already holds). Raises :class:`WhitelistError` if the
        scope is unknown or the id is a protected admin (admin no-delete).
        """
        list_key = _SCOPE_TO_LISTKEY.get(scope)
        if list_key is None:
            raise WhitelistError(f"unknown scope: {scope!r}")

        if source_id in self._as_list(self._line_config().get("admins")):
            raise WhitelistError(
                f"cannot remove {source_id!r}: protected admin (no-delete)"
            )

        wl = self._whitelist()
        current = self._as_list(wl.get(list_key))
        if source_id not in current:
            return False
        wl[list_key] = [x for x in current if x != source_id]
        self._write_field("whitelist", wl)
        return True

    # ------------------------------------------------------------------
    # unauthorized dedup / throttle (§2.6)
    # ------------------------------------------------------------------
    def _seen(self) -> Dict[str, Any]:
        return self._as_dict(self._line_config().get("unauthorized_seen"))

    def _seen_entry(self, source_id: str) -> Dict[str, Any]:
        return self._as_dict(self._seen().get(source_id))

    def should_notify_unauthorized(
        self, source_id: str, window_sec: Optional[int] = None
    ) -> bool:
        """Whether to notify admins about this unauthorized source.

        Default (``window_sec is None``): notify **once only** — subsequent
        calls return False until :meth:`clear_unauthorized`. With a window,
        re-notify once the window since ``last_notified`` has elapsed.

        An ``ignored`` source is never re-notified regardless of window.
        """
        entry = self._seen_entry(source_id)
        if entry.get("status") == "ignored":
            return False
        last = entry.get("last_notified")
        if last is None:
            return True
        if window_sec is None:
            return False
        try:
            return (time.time() - float(last)) >= float(window_sec)
        except (TypeError, ValueError):
            return True

    def should_reply_unauthorized(
        self, source_id: str, window_sec: int = 86400
    ) -> bool:
        """Whether to send a one-off reply to an unauthorized source.

        Per-source once / ``window_sec`` (default 24h) since ``last_replied``.
        """
        entry = self._seen_entry(source_id)
        last = entry.get("last_replied")
        if last is None:
            return True
        try:
            return (time.time() - float(last)) >= float(window_sec)
        except (TypeError, ValueError):
            return True

    def _mutate_seen(self, source_id: str, updates: Dict[str, Any]) -> None:
        seen = self._seen()
        entry = self._as_dict(seen.get(source_id))
        entry.update(updates)
        seen[source_id] = entry
        self._write_field("unauthorized_seen", seen)

    def mark_unauthorized_notified(self, source_id: str) -> None:
        now = time.time()
        entry = self._seen_entry(source_id)
        first_seen = entry.get("first_seen", now)
        count = int(entry.get("count", 0) or 0) + 1
        self._mutate_seen(
            source_id,
            {"first_seen": first_seen, "last_notified": now, "count": count},
        )

    def mark_unauthorized_replied(self, source_id: str) -> None:
        now = time.time()
        entry = self._seen_entry(source_id)
        first_seen = entry.get("first_seen", now)
        self._mutate_seen(
            source_id, {"first_seen": first_seen, "last_replied": now}
        )

    def record_attempt(
        self,
        source_id: str,
        *,
        platform: str = "line",
        source_type: str,
        name: str = "",
    ) -> None:
        """Upsert a pending-queue entry for an unauthorized ``source_id``.

        New id -> ``first_seen`` = now, ``status`` = "pending", ``count`` 0->1.
        Every call -> ``last_seen`` = now, ``count`` += 1, refresh
        ``platform``/``source_type``. ``name`` overwrites only when non-empty
        (a resolved name is never blanked). ``last_notified``/``last_replied``
        are left untouched — notify/reply throttling owns those.
        """
        now = time.time()
        entry = self._seen_entry(source_id)
        first_seen = entry.get("first_seen", now)
        status = entry.get("status", "pending")
        count = int(entry.get("count", 0) or 0) + 1
        updates: Dict[str, Any] = {
            "first_seen": first_seen,
            "last_seen": now,
            "count": count,
            "platform": platform,
            "source_type": source_type,
            "status": status,
        }
        if name:
            updates["name"] = name
        self._mutate_seen(source_id, updates)

    def record_unauthorized_attempt(self, source_id: str, meta: dict) -> None:
        """Log a DM-stranger attempt (thin delegate to :meth:`record_attempt`).

        Kept for existing callers with the old ``(source_id, meta)`` signature;
        the DM ``name`` (if any) is lifted out of ``meta`` and threaded through
        the single ``record_attempt`` code path. Extra ``meta`` keys are stored
        on the entry so nothing is lost.
        """
        meta = meta if isinstance(meta, dict) else {}
        self.record_attempt(
            source_id, source_type="dm", name=meta.get("name", "")
        )
        # preserve any extra meta keys (note/text/user_id/...) on the entry,
        # without clobbering the fields record_attempt just set.
        extra = {
            k: v
            for k, v in meta.items()
            if k not in {"name", "source_type", "platform"}
        }
        if extra:
            self._mutate_seen(source_id, extra)

    def list_pending(self) -> list:
        """Pending unauthorized sources, newest first.

        Excludes entries marked ``ignored`` and any id that is *currently*
        whitelisted for its scope. Sorted by ``last_seen`` descending. Each
        item is a flat dict with defaulted fields, never raising on gaps.
        """
        seen = self._seen()
        result: list = []
        for source_id, raw in seen.items():
            entry = self._as_dict(raw)
            if entry.get("status") == "ignored":
                continue
            # Infer source_type from the id prefix for legacy entries so the UI
            # shows the right type and the "already whitelisted" filter works.
            source_type = entry.get("source_type", "") or _infer_scope_from_id(source_id)
            if source_type and self.is_allowed(source_type, source_id):
                continue
            last_seen = entry.get("last_seen", entry.get("first_seen", 0.0))
            result.append(
                {
                    "platform": entry.get("platform", "line"),
                    "source_type": source_type,
                    "id": source_id,
                    "name": entry.get("name", ""),
                    "first_seen": entry.get("first_seen", last_seen),
                    "last_seen": last_seen,
                    "count": int(entry.get("count", 0) or 0),
                    "last_notified": entry.get("last_notified"),
                    "last_replied": entry.get("last_replied"),
                    "status": entry.get("status", "pending"),
                }
            )
        result.sort(key=lambda e: e.get("last_seen") or 0.0, reverse=True)
        return result

    def approve_pending(self, source_id: str, *, added_by: str = "") -> dict:
        """Whitelist a pending source, then drop it from the pending queue.

        Resolves ``scope`` from the entry's ``source_type`` (dm/group/room,
        already the store's scope vocabulary), calls :meth:`add` with the
        stored ``name``, then :meth:`clear_unauthorized`. Returns
        ``{"approved": True, "scope", "id"}`` on success, or
        ``{"approved": False, "reason": "not found", "id"}`` if there is no
        entry (or it lacks a usable source_type).
        """
        seen = self._seen()
        if source_id not in seen:
            return {"approved": False, "reason": "not found", "id": source_id}
        entry = self._as_dict(seen.get(source_id))
        scope = entry.get("source_type", "")
        if scope not in _SCOPE_TO_LISTKEY:
            # Legacy entry with no source_type (or a bad one): fall back to the
            # LINE id prefix so it can still be approved (U→dm/C→group/R→room).
            scope = _infer_scope_from_id(source_id)
        if scope not in _SCOPE_TO_LISTKEY:
            return {"approved": False, "reason": "unresolved scope", "id": source_id}
        self.add(
            scope, source_id, added_by=added_by, name=entry.get("name", "")
        )
        # add() already clears unauthorized state, but call it explicitly per
        # contract in case add()'s cleanup ever changes.
        self.clear_unauthorized(source_id)
        return {"approved": True, "scope": scope, "id": source_id}

    def ignore_pending(self, source_id: str) -> bool:
        """Mark a source ``ignored`` so it is suppressed from notify/list.

        Creates a minimal entry if none exists so the decision sticks even for
        a source not yet recorded. Always returns ``True``.
        """
        entry = self._seen_entry(source_id)
        updates: Dict[str, Any] = {"status": "ignored"}
        if "first_seen" not in entry:
            updates["first_seen"] = time.time()
        self._mutate_seen(source_id, updates)
        return True

    def set_name(self, source_id: str, name: str) -> None:
        """Backfill a resolved display ``name`` onto a pending entry.

        No-op for a blank name (never blanks an already-resolved name). Creates
        the entry if absent so a late-resolved name is not lost.
        """
        if not name:
            return
        self._mutate_seen(source_id, {"name": name})

    def clear_unauthorized(self, source_id: str) -> None:
        seen = self._seen()
        if source_id in seen:
            seen = {k: v for k, v in seen.items() if k != source_id}
            self._write_field("unauthorized_seen", seen)

    # ------------------------------------------------------------------
    # media retention (§7)
    # ------------------------------------------------------------------
    def retention_days(self, source_id: Optional[str] = None) -> int:
        """Media retention window in days.

        Global default is ``platforms.line.retention_days`` (falling back to
        3). A per-source override may live under ``meta.groups[id]`` or
        ``meta.users[id]`` as ``retention_days``.
        """
        line = self._line_config()
        try:
            default = int(line.get("retention_days", _DEFAULT_RETENTION_DAYS))
        except (TypeError, ValueError):
            default = _DEFAULT_RETENTION_DAYS

        if source_id is None:
            return default

        meta = self._meta()
        for bucket_key in ("groups", "users", "rooms"):
            bucket = self._as_dict(meta.get(bucket_key))
            entry = self._as_dict(bucket.get(source_id))
            if "retention_days" in entry:
                try:
                    return int(entry["retention_days"])
                except (TypeError, ValueError):
                    return default
        return default
