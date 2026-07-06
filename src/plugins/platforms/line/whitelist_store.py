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
        """
        entry = self._seen_entry(source_id)
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

    def record_unauthorized_attempt(self, source_id: str, meta: dict) -> None:
        """Log a DM-stranger attempt: bumps count, stores first_seen + meta."""
        now = time.time()
        entry = self._seen_entry(source_id)
        first_seen = entry.get("first_seen", now)
        count = int(entry.get("count", 0) or 0) + 1
        updates: Dict[str, Any] = {"first_seen": first_seen, "count": count}
        if isinstance(meta, dict):
            updates.update(meta)
        self._mutate_seen(source_id, updates)

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
