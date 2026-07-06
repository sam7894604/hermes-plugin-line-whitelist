#!/usr/bin/env python3
"""Agent-mediated LINE whitelist approval tool (Phase 3).

Background
----------
The LINE adapter (``plugins/platforms/line/adapter.py``) only answers chats
(DMs, groups, rooms) whose id appears in a whitelist persisted in
``config.yaml``. Phase 1 introduced :class:`WhitelistStore`, the single owner
of that config-backed list, and Phase 2 wired the adapter to hot-reload it.

This module is the Phase 3 *agent* surface: a compressed, action-oriented tool
that lets an **admin** approve / remove / list whitelist entries from inside a
normal chat turn, without hand-editing ``config.yaml``. On success the store
writes ``config.yaml`` and the adapter hot-reloads, so an approval takes effect
immediately for the next inbound message.

Permission model
----------------
The whitelist is a security boundary — a non-admin must not be able to add
their own chat to it. Every mutating action (``approve`` / ``remove``) is gated
by ``WhitelistStore.is_admin(<caller user id>)``. The caller's user id is read
from the session context (``HERMES_SESSION_USER_ID``), the same channel
``send_message`` uses to resolve the acting participant. ``list`` is admin-only
too: the whitelist is an access-control list and its contents (who can reach
the agent) should not leak to arbitrary group members.

Non-admin proposals
-------------------
A non-admin who *asks* to be added is not silently dropped: their request is
staged to a ``line``-scoped pending store (mirroring
``tools/write_approval.py``'s stage/gate pattern, but self-contained here so we
never touch the shared ``_SUBSYSTEMS`` tuple). An admin later confirms with
``action="approve_pending"`` / ``action="list_pending"`` / ``action="reject"``.
This is optional sugar on top of the MVP direct-approve path.

Store interface (Phase 1, may not exist yet in this worktree)
-------------------------------------------------------------
    from plugins.platforms.line.whitelist_store import (
        WhitelistStore, WhitelistError,
    )
    store.list(scope=None)                     -> list[dict]
    store.add(scope, id, added_by=, note=)     -> dict | None
    store.remove(scope, id)                    -> bool  (raises WhitelistError
                                                  when id is an admin)
    store.is_admin(user_id)                    -> bool

The import is deferred (inside ``_get_store``) so this module imports cleanly
even before Phase 1 lands, and so tests can monkeypatch the store factory.
"""

from __future__ import annotations

import json
import logging
import os
import time
import uuid
from pathlib import Path
from typing import Any, Dict, List, Optional

logger = logging.getLogger(__name__)

# Canonical scopes a LINE chat id can belong to.
_VALID_SCOPES = ("dm", "group", "room")

# Actions the tool understands.
_VALID_ACTIONS = (
    "list",
    "approve",
    "remove",
    # Optional non-admin proposal flow:
    "propose",
    "list_pending",
    "approve_pending",
    "reject",
)


# ---------------------------------------------------------------------------
# Store access (deferred import so this module loads before Phase 1 lands)
# ---------------------------------------------------------------------------

def _get_store():
    """Return a live :class:`WhitelistStore` instance.

    Deferred import + thin factory so (a) this module imports even when the
    Phase-1 store isn't present yet, and (b) tests can monkeypatch
    ``tools.line_whitelist_tool._get_store`` with a mock. The real store owns
    config.yaml read/write; we never touch config here directly.
    """
    from plugins.platforms.line.whitelist_store import WhitelistStore
    return WhitelistStore()


def _whitelist_error_cls():
    """Return the ``WhitelistError`` class (deferred import).

    Falls back to ``Exception`` if Phase 1 isn't importable, so error handling
    degrades gracefully instead of raising ``ImportError`` at except-clause
    evaluation time.
    """
    try:
        from plugins.platforms.line.whitelist_store import WhitelistError
        return WhitelistError
    except Exception:  # pragma: no cover - only when Phase 1 absent
        return Exception


# ---------------------------------------------------------------------------
# Caller identity
# ---------------------------------------------------------------------------

def _caller_user_id() -> str:
    """Return the acting participant's user id from the session context.

    Reuses ``HERMES_SESSION_USER_ID`` — the same channel ``send_message`` reads
    to resolve the acting participant (see ``tools/send_message_tool.py``).
    Empty string when unknown (CLI / no session bound), which
    ``WhitelistStore.is_admin`` will treat as non-admin.
    """
    try:
        from gateway.session_context import get_session_env
        return get_session_env("HERMES_SESSION_USER_ID", "") or ""
    except Exception:
        # CLI / test process with no gateway session context.
        return os.getenv("HERMES_SESSION_USER_ID", "") or ""


# ---------------------------------------------------------------------------
# line-scoped pending store (self-contained; does NOT touch write_approval._SUBSYSTEMS)
# ---------------------------------------------------------------------------

def _pending_dir() -> Path:
    from hermes_constants import get_hermes_home
    return get_hermes_home() / "pending" / "line_whitelist"


def _stage_proposal(scope: str, chat_id: str, *, note: str,
                    proposed_by: str) -> Dict[str, Any]:
    """Persist a non-admin whitelist proposal and return its record.

    Best-effort disk write (same safe-failure posture as
    ``write_approval.stage_write``): a disk error logs and still returns the
    record; the proposal is simply not durably staged, which is the safe
    failure for an approval gate (nothing is silently whitelisted).
    """
    pid = uuid.uuid4().hex[:8]
    record = {
        "id": pid,
        "subsystem": "line_whitelist",
        "scope": scope,
        "chat_id": chat_id,
        "note": (note or "").strip(),
        "proposed_by": proposed_by,
        "created_at": time.time(),
    }
    try:
        d = _pending_dir()
        d.mkdir(parents=True, exist_ok=True)
        path = d / f"{pid}.json"
        tmp = path.with_suffix(".json.tmp")
        tmp.write_text(json.dumps(record, ensure_ascii=False, indent=2), encoding="utf-8")
        os.replace(tmp, path)
    except Exception as e:  # pragma: no cover - disk failure path
        logger.error("Failed to stage LINE whitelist proposal: %s", e, exc_info=True)
    return record


def _list_proposals() -> List[Dict[str, Any]]:
    d = _pending_dir()
    if not d.exists():
        return []
    out: List[Dict[str, Any]] = []
    for p in d.glob("*.json"):
        try:
            out.append(json.loads(p.read_text(encoding="utf-8")))
        except Exception:
            logger.warning("Skipping unreadable LINE whitelist proposal: %s", p)
    out.sort(key=lambda r: r.get("created_at", 0))
    return out


def _get_proposal(pid: str) -> Optional[Dict[str, Any]]:
    path = _pending_dir() / f"{pid}.json"
    if not path.exists():
        return None
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        return None


def _discard_proposal(pid: str) -> bool:
    path = _pending_dir() / f"{pid}.json"
    try:
        if path.exists():
            path.unlink()
            return True
    except Exception as e:  # pragma: no cover
        logger.error("Failed to discard LINE whitelist proposal %s: %s", pid, e)
    return False


# ---------------------------------------------------------------------------
# Core tool
# ---------------------------------------------------------------------------

def line_whitelist(
    action: str,
    scope: Optional[str] = None,
    id: Optional[str] = None,
    note: Optional[str] = None,
    pending_id: Optional[str] = None,
    task_id: str = None,
) -> str:
    """Manage the LINE chat whitelist (admin-gated).

    See module docstring for the permission model. Returns a JSON string.
    """
    del task_id  # kept for handler-signature compatibility

    normalized = (action or "").strip().lower()
    if normalized not in _VALID_ACTIONS:
        return tool_error(
            f"Unknown action '{action}'. Valid actions: {', '.join(_VALID_ACTIONS)}.",
            success=False,
        )

    caller = _caller_user_id()

    try:
        store = _get_store()
    except Exception as e:
        return tool_error(
            f"LINE whitelist store unavailable: {e}. Is the LINE platform configured?",
            success=False,
        )

    try:
        is_admin = bool(store.is_admin(caller))
    except Exception as e:
        return tool_error(f"Failed to resolve admin status: {e}", success=False)

    # ---- list (admin-only: the whitelist is an access-control list) --------
    if normalized == "list":
        if not is_admin:
            return _refuse("list the LINE whitelist")
        entries = store.list(scope=_norm_scope(scope)) if scope else store.list()
        return tool_result(success=True, action="list", count=len(entries), entries=entries)

    # ---- approve (admin-only) ---------------------------------------------
    if normalized == "approve":
        if not is_admin:
            return _refuse("approve LINE whitelist entries")
        scope_err = _validate_scope(scope)
        if scope_err:
            return scope_err
        if not (id or "").strip():
            return tool_error("`id` is required for action='approve'.", success=False)
        try:
            result = store.add(
                _norm_scope(scope), id.strip(), added_by=caller, note=note,
            )
        except _whitelist_error_cls() as e:
            return tool_error(f"Could not add to whitelist: {e}", success=False)
        return tool_result(
            success=True,
            action="approve",
            scope=_norm_scope(scope),
            id=id.strip(),
            added_by=caller,
            entry=result,
            message=(
                f"Added {_norm_scope(scope)} '{id.strip()}' to the LINE whitelist. "
                "It takes effect immediately."
            ),
        )

    # ---- remove (admin-only) ----------------------------------------------
    if normalized == "remove":
        if not is_admin:
            return _refuse("remove LINE whitelist entries")
        scope_err = _validate_scope(scope)
        if scope_err:
            return scope_err
        if not (id or "").strip():
            return tool_error("`id` is required for action='remove'.", success=False)
        try:
            removed = store.remove(_norm_scope(scope), id.strip())
        except _whitelist_error_cls() as e:
            # Store raises WhitelistError when the id is an admin (can't
            # deadman-remove yourself). Surface it as a clear tool error.
            return tool_error(f"Could not remove from whitelist: {e}", success=False)
        if not removed:
            return tool_error(
                f"No {_norm_scope(scope)} entry '{id.strip()}' in the whitelist.",
                success=False,
            )
        return tool_result(
            success=True,
            action="remove",
            scope=_norm_scope(scope),
            id=id.strip(),
            message=(
                f"Removed {_norm_scope(scope)} '{id.strip()}' from the LINE "
                "whitelist. It takes effect immediately."
            ),
        )

    # ---- propose (non-admin proposal → staged for admin) -------------------
    if normalized == "propose":
        scope_err = _validate_scope(scope)
        if scope_err:
            return scope_err
        if not (id or "").strip():
            return tool_error("`id` is required for action='propose'.", success=False)
        rec = _stage_proposal(
            _norm_scope(scope), id.strip(), note=note or "", proposed_by=caller,
        )
        return tool_result(
            success=True,
            action="propose",
            staged=True,
            pending_id=rec["id"],
            message=(
                "Proposal staged for admin approval — NOT yet whitelisted. "
                "An admin can review with line_whitelist(action='list_pending') "
                "and confirm with action='approve_pending'."
            ),
        )

    # ---- list_pending (admin-only) ----------------------------------------
    if normalized == "list_pending":
        if not is_admin:
            return _refuse("list pending LINE whitelist proposals")
        proposals = _list_proposals()
        return tool_result(
            success=True, action="list_pending",
            count=len(proposals), pending=proposals,
        )

    # ---- approve_pending (admin-only) -------------------------------------
    if normalized == "approve_pending":
        if not is_admin:
            return _refuse("approve pending LINE whitelist proposals")
        if not (pending_id or "").strip():
            return tool_error(
                "`pending_id` is required for action='approve_pending'.", success=False,
            )
        rec = _get_proposal(pending_id.strip())
        if not rec:
            return tool_error(
                f"No pending proposal '{pending_id.strip()}'. "
                "Use action='list_pending' to inspect proposals.",
                success=False,
            )
        try:
            result = store.add(
                rec["scope"], rec["chat_id"], added_by=caller, note=rec.get("note"),
            )
        except _whitelist_error_cls() as e:
            return tool_error(f"Could not add proposed entry: {e}", success=False)
        _discard_proposal(pending_id.strip())
        return tool_result(
            success=True,
            action="approve_pending",
            scope=rec["scope"],
            id=rec["chat_id"],
            added_by=caller,
            entry=result,
            message=(
                f"Approved proposal {pending_id.strip()}: added {rec['scope']} "
                f"'{rec['chat_id']}' to the LINE whitelist (effective immediately)."
            ),
        )

    # ---- reject (admin-only) ----------------------------------------------
    if normalized == "reject":
        if not is_admin:
            return _refuse("reject pending LINE whitelist proposals")
        if not (pending_id or "").strip():
            return tool_error(
                "`pending_id` is required for action='reject'.", success=False,
            )
        if not _discard_proposal(pending_id.strip()):
            return tool_error(
                f"No pending proposal '{pending_id.strip()}' to reject.", success=False,
            )
        return tool_result(
            success=True, action="reject", pending_id=pending_id.strip(),
            message=f"Rejected and discarded proposal {pending_id.strip()}.",
        )

    # Unreachable (action validated above), but keep a clear fallback.
    return tool_error(f"Unhandled action '{action}'.", success=False)  # pragma: no cover


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _norm_scope(scope: Optional[str]) -> str:
    return (scope or "").strip().lower()


def _validate_scope(scope: Optional[str]) -> Optional[str]:
    """Return a tool_error JSON string if scope is invalid, else None."""
    s = _norm_scope(scope)
    if not s:
        return tool_error(
            f"`scope` is required and must be one of {_VALID_SCOPES}.", success=False,
        )
    if s not in _VALID_SCOPES:
        return tool_error(
            f"Invalid scope '{scope}'. Must be one of {_VALID_SCOPES}.", success=False,
        )
    return None


def _refuse(what: str) -> str:
    return tool_error(
        f"Permission denied: only a LINE admin may {what}. "
        "This action requires an admin session.",
        success=False,
        permission_denied=True,
    )


# ---------------------------------------------------------------------------
# Availability check
# ---------------------------------------------------------------------------

def check_line_whitelist_requirements() -> bool:
    """Tool is available when the LINE platform plugin is importable.

    Uses a cheap import probe on the Phase-1 store module; if LINE isn't
    installed/configured, the tool is hidden rather than erroring at call time.
    """
    try:
        import importlib.util
        return (
            importlib.util.find_spec("plugins.platforms.line.whitelist_store")
            is not None
        )
    except Exception:
        return False


# ---------------------------------------------------------------------------
# Schema
# ---------------------------------------------------------------------------

LINE_WHITELIST_SCHEMA = {
    "name": "line_whitelist",
    "description": (
        "Manage the LINE chat whitelist (which DMs/groups/rooms the agent will "
        "answer). ADMIN-ONLY for list/approve/remove — a non-admin session is "
        "refused. Changes take effect immediately (the adapter hot-reloads "
        "config.yaml).\n\n"
        "Actions:\n"
        "  list    — show current whitelist (optionally filtered by scope).\n"
        "  approve — add scope+id to the whitelist.\n"
        "  remove  — remove scope+id from the whitelist.\n"
        "  propose — (non-admin) stage a request to be whitelisted for an admin "
        "to confirm later; nothing is whitelisted until approved.\n"
        "  list_pending / approve_pending / reject — admin review of proposals.\n\n"
        "scope is one of: dm, group, room. Always list first before removing so "
        "you use the exact id."
    ),
    "parameters": {
        "type": "object",
        "properties": {
            "action": {
                "type": "string",
                "enum": list(_VALID_ACTIONS),
                "description": (
                    "One of: list, approve, remove, propose, list_pending, "
                    "approve_pending, reject."
                ),
            },
            "scope": {
                "type": "string",
                "enum": list(_VALID_SCOPES),
                "description": (
                    "Chat scope: 'dm' (1:1), 'group', or 'room'. Required for "
                    "approve/remove/propose."
                ),
            },
            "id": {
                "type": "string",
                "description": (
                    "The LINE chat/user id to approve or remove. Required for "
                    "approve/remove/propose."
                ),
            },
            "note": {
                "type": "string",
                "description": "Optional free-text note recorded with the entry.",
            },
            "pending_id": {
                "type": "string",
                "description": (
                    "The proposal id (from list_pending). Required for "
                    "approve_pending/reject."
                ),
            },
        },
        "required": ["action"],
    },
}


# ---------------------------------------------------------------------------
# Registry
# ---------------------------------------------------------------------------

from tools.registry import registry, tool_error, tool_result

registry.register(
    name="line_whitelist",
    toolset="line_whitelist",
    schema=LINE_WHITELIST_SCHEMA,
    handler=lambda args, **kw: line_whitelist(
        action=args.get("action", ""),
        scope=args.get("scope"),
        id=args.get("id"),
        note=args.get("note"),
        pending_id=args.get("pending_id"),
        task_id=kw.get("task_id"),
    ),
    check_fn=check_line_whitelist_requirements,
    emoji="✅",
)
