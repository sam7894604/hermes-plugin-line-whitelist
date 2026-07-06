"""LINE whitelist dashboard plugin — backend API routes.

Mounted at ``/api/plugins/line-whitelist/`` by the dashboard plugin system
(see ``hermes_cli/web_server._mount_plugin_api_routes``).

This layer is intentionally thin, mirroring ``plugins/kanban/dashboard/
plugin_api.py``: every handler is a small wrapper around either the Phase-1
``WhitelistStore`` (allowlist CRUD), the shared ``hermes_state.SessionDB``
data layer (communication records — the same store the core ``/api/sessions``
endpoints read), or a direct call to the LINE Messaging API (name resolution).

Auth
----
Like the kanban plugin and the core ``/api/webhooks`` handlers, these routes
carry **no per-route FastAPI ``Depends(...)`` auth**. They inherit the
dashboard's process-wide auth gate (``hermes_cli.web_server`` middleware),
which requires the session bearer token / cookie on every ``/api/plugins/...``
request. There is nothing extra to wire up here.

Records reuse
-------------
``GET /records`` does NOT re-implement session storage. It opens the same
``hermes_state.SessionDB`` the core session endpoints use and filters rows to
those whose ``session_key`` begins with ``line:`` — no HTTP self-loop.

Name resolution
---------------
``GET /resolve`` runs inside the web_server process (NOT the LINE adapter), so
it reads the channel access token from env / config itself and calls the LINE
profile/group-summary endpoints directly, with a small TTL cache. On any
failure it falls back to echoing the raw id so the UI always has *something*
to show.
"""

from __future__ import annotations

import logging
import os
import time
from typing import Any, Optional

from fastapi import APIRouter, HTTPException, Query
from pydantic import BaseModel

log = logging.getLogger(__name__)

router = APIRouter()

# Valid allowlist scopes. Kept in sync with the WhitelistStore contract
# (users / groups / rooms). ``add``/``remove`` accept the singular scope names
# the store uses; ``list`` returns the plural buckets.
VALID_SCOPES = ("user", "group", "room")


# ---------------------------------------------------------------------------
# WhitelistStore access (Phase 1)
# ---------------------------------------------------------------------------
#
# The store may not exist yet in this worktree (P1 lands separately). Import it
# lazily inside a helper so this module still imports cleanly — the dashboard
# plugin loader execs the file at startup, and a hard ImportError at module
# top-level would drop ALL of these routes. When the store is missing we
# surface a clean 503 per-request instead.


def _get_store():
    """Return a :class:`WhitelistStore` instance, or raise 503 if P1 absent.

    Imported lazily (not at module top-level) so the plugin still mounts on a
    tree where ``whitelist_store`` hasn't landed yet; the endpoints then return
    a clean 503 rather than the whole router failing to import.
    """
    try:
        from plugins.platforms.line.whitelist_store import WhitelistStore
    except Exception as exc:  # pragma: no cover - depends on P1 presence
        raise HTTPException(
            status_code=503,
            detail=(
                "LINE whitelist store unavailable "
                f"(Phase 1 whitelist_store not importable: {exc})"
            ),
        )
    return WhitelistStore()


def _whitelist_error_cls():
    """Return the ``WhitelistError`` type if importable, else a sentinel.

    Used so ``remove`` can map the store's admin-no-delete guard to a 4xx
    without a hard dependency on P1 at import time.
    """
    try:
        from plugins.platforms.line.whitelist_store import WhitelistError
        return WhitelistError
    except Exception:  # pragma: no cover
        return None


# ---------------------------------------------------------------------------
# GET /whitelist  — list users / groups / rooms
# ---------------------------------------------------------------------------

@router.get("/whitelist")
def list_whitelist(
    scope: Optional[str] = Query(
        None, description="Restrict to a single scope: user|group|room",
    ),
):
    """Return the current allowlist grouped by scope.

    Shape: ``{"users": [...], "groups": [...], "rooms": [...]}`` where each
    entry is whatever the store records (typically ``{id, note, added_by,
    added_at}``). When ``scope`` is passed, only that bucket is populated.
    """
    if scope is not None and scope not in VALID_SCOPES:
        raise HTTPException(
            status_code=400,
            detail=f"scope must be one of {VALID_SCOPES}",
        )
    store = _get_store()
    try:
        data = store.list(scope=scope)
    except Exception as exc:
        log.warning("whitelist list failed: %s", exc)
        raise HTTPException(status_code=500, detail=f"list failed: {exc}")
    # Normalise to the documented buckets so the UI can rely on the shape even
    # if the store omits an empty bucket.
    return {
        "users": data.get("users", []),
        "groups": data.get("groups", []),
        "rooms": data.get("rooms", []),
    }


# ---------------------------------------------------------------------------
# POST /whitelist  — add {scope, id, note?}
# ---------------------------------------------------------------------------

class AddWhitelistBody(BaseModel):
    scope: str
    id: str
    note: Optional[str] = None


@router.post("/whitelist")
def add_whitelist(payload: AddWhitelistBody):
    """Add an id to the allowlist under ``scope``.

    Returns ``{"ok": True, "entry": {...}}`` on success. Rejects unknown
    scopes and blank ids with a 400.
    """
    if payload.scope not in VALID_SCOPES:
        raise HTTPException(
            status_code=400,
            detail=f"scope must be one of {VALID_SCOPES}",
        )
    entry_id = (payload.id or "").strip()
    if not entry_id:
        raise HTTPException(status_code=400, detail="id is required")

    store = _get_store()
    WhitelistError = _whitelist_error_cls()
    try:
        entry = store.add(
            payload.scope,
            entry_id,
            added_by="dashboard",
            note=payload.note,
        )
    except Exception as exc:
        # Surface store-level validation (e.g. malformed id / duplicate) as 4xx.
        if WhitelistError is not None and isinstance(exc, WhitelistError):
            raise HTTPException(status_code=400, detail=str(exc))
        log.warning("whitelist add failed: %s", exc)
        raise HTTPException(status_code=400, detail=str(exc))
    return {"ok": True, "entry": entry}


# ---------------------------------------------------------------------------
# DELETE /whitelist/{scope}/{id}  — remove
# ---------------------------------------------------------------------------

@router.delete("/whitelist/{scope}/{id}")
def remove_whitelist(scope: str, id: str):
    """Remove an id from the allowlist.

    DELETE is **idempotent**: whether the id was present (removed now) or
    already absent (no-op), the desired end state — "id is not in the
    whitelist" — holds, so we return 200. The response carries ``removed`` /
    ``already_absent`` so the UI can tell the two apart. The store enforces the
    "admin entry cannot be deleted" rule and raises ``WhitelistError``, which we
    surface as a 409 so the UI can show why the delete was refused.

    (Previously this returned 404 when ``store.remove`` was falsy — but the
    store returns ``None``/``False`` for a *successful* no-present-op removal,
    so a genuine delete was mis-reported to the UI as 404 while the config was
    in fact written. The store now returns a proper bool and this handler treats
    the operation as idempotent.)
    """
    if scope not in VALID_SCOPES:
        raise HTTPException(
            status_code=400,
            detail=f"scope must be one of {VALID_SCOPES}",
        )
    store = _get_store()
    WhitelistError = _whitelist_error_cls()
    try:
        removed = bool(store.remove(scope, id))
    except Exception as exc:
        if WhitelistError is not None and isinstance(exc, WhitelistError):
            # Admin-protected / policy refusal → 409 Conflict.
            raise HTTPException(status_code=409, detail=str(exc))
        log.warning("whitelist remove failed: %s", exc)
        raise HTTPException(status_code=400, detail=str(exc))
    return {
        "ok": True,
        "scope": scope,
        "id": id,
        "removed": removed,
        "already_absent": not removed,
    }


# ---------------------------------------------------------------------------
# GET /records  — LINE communication records (reuses SessionDB)
# ---------------------------------------------------------------------------

_LINE_KEY_PREFIX = "line:"


@router.get("/records")
def list_records(
    limit: int = Query(50, ge=1, le=500),
    offset: int = Query(0, ge=0),
    session_id: Optional[str] = Query(
        None, description="If set, return this session's messages instead of the list",
    ),
):
    """List LINE communication records, or one session's messages.

    Reuses the SAME data layer the core ``/api/sessions`` endpoints use
    (``hermes_state.SessionDB``) rather than looping back over HTTP. Rows are
    filtered to ``session_key`` values beginning with ``line:``.

    * Without ``session_id``: returns ``{"records": [...], "count": N}`` — a
      list of LINE sessions (id, key, title, timestamps, message_count).
    * With ``session_id``: returns ``{"session_id": sid, "messages": [...]}``
      mirroring ``GET /api/sessions/{id}/messages``.
    """
    try:
        from hermes_state import SessionDB
    except Exception as exc:
        raise HTTPException(
            status_code=503, detail=f"session store unavailable: {exc}",
        )

    db = SessionDB()
    try:
        # Per-session messages view.
        if session_id:
            sid = db.resolve_session_id(session_id)
            if not sid:
                raise HTTPException(status_code=404, detail="session not found")
            try:
                sid = db.resolve_resume_session_id(sid)
            except Exception:
                pass
            messages = db.get_messages(sid)
            return {"session_id": sid, "messages": messages}

        # Pull a generous page of recent sessions, then keep only LINE ones.
        # We over-fetch (limit+offset from the LINE subset) by scanning a
        # wider window because list_sessions_rich can't filter on
        # session_key directly; LINE traffic is a small slice of all
        # sessions so a bounded scan is fine for a dashboard.
        rows = db.list_sessions_rich(
            limit=max(limit + offset, limit) * 4,
            offset=0,
            order_by_last_active=True,
        )
        line_rows = [
            r for r in rows
            if str(r.get("session_key") or "").startswith(_LINE_KEY_PREFIX)
        ]
        page = line_rows[offset: offset + limit]
        records = [
            {
                "session_id": r.get("session_id") or r.get("id"),
                "session_key": r.get("session_key"),
                "title": r.get("title"),
                "started_at": r.get("started_at"),
                "last_active": r.get("last_active"),
                "ended_at": r.get("ended_at"),
                "message_count": r.get("message_count"),
                "source": r.get("source"),
            }
            for r in page
        ]
        return {
            "records": records,
            "count": len(records),
            "total_line_sessions": len(line_rows),
            "limit": limit,
            "offset": offset,
        }
    except HTTPException:
        raise
    except Exception as exc:
        log.warning("records query failed: %s", exc)
        raise HTTPException(status_code=500, detail=f"records query failed: {exc}")
    finally:
        try:
            db.close()
        except Exception:
            pass


# ---------------------------------------------------------------------------
# GET /resolve  — LINE display-name resolution (independent of the adapter)
# ---------------------------------------------------------------------------
#
# Runs in the web_server process, so we read the channel access token here and
# call the LINE Messaging API directly. Small in-memory TTL cache keeps the
# UI snappy and avoids hammering LINE when a records table renders many ids.

_RESOLVE_TTL_SECONDS = 600  # 10 minutes
# key: (type, id) -> (expires_at_epoch, name)
_resolve_cache: dict[tuple[str, str], tuple[float, str]] = {}


def _line_channel_access_token() -> Optional[str]:
    """Resolve the LINE channel access token, mirroring the adapter's order.

    Env var (``LINE_CHANNEL_ACCESS_TOKEN``) wins over the config value at
    ``platforms.line.channel_access_token`` — exactly what
    ``plugins/platforms/line/adapter.py`` does. Returns ``None`` when neither
    is set.
    """
    tok = os.getenv("LINE_CHANNEL_ACCESS_TOKEN")
    if tok:
        return tok
    try:
        from hermes_cli.config import load_config
        cfg = load_config() or {}
        return (
            (cfg.get("platforms", {}) or {})
            .get("line", {})
            .get("channel_access_token")
        ) or None
    except Exception:
        return None


@router.get("/resolve")
async def resolve_name(
    type: str = Query(..., description="user or group"),
    id: str = Query(..., description="LINE user id (U...) or group id (C...)"),
):
    """Resolve a LINE user/group id to its display name.

    ``type=user``  → GET https://api.line.me/v2/bot/profile/{id}   (displayName)
    ``type=group`` → GET https://api.line.me/v2/bot/group/{id}/summary (groupName)

    Cached for ``_RESOLVE_TTL_SECONDS``. On any failure (no token, network
    error, non-200) the raw id is returned as ``name`` with ``resolved=False``
    so the UI always has a label. ``cached`` marks a cache hit.
    """
    if type not in ("user", "group"):
        raise HTTPException(status_code=400, detail="type must be 'user' or 'group'")
    ident = (id or "").strip()
    if not ident:
        raise HTTPException(status_code=400, detail="id is required")

    now = time.time()
    cache_key = (type, ident)
    hit = _resolve_cache.get(cache_key)
    if hit and hit[0] > now:
        return {"type": type, "id": ident, "name": hit[1], "resolved": True, "cached": True}

    token = _line_channel_access_token()
    if not token:
        # No token configured — fall back to the raw id.
        return {"type": type, "id": ident, "name": ident, "resolved": False, "cached": False}

    if type == "user":
        url = f"https://api.line.me/v2/bot/profile/{ident}"
        name_field = "displayName"
    else:
        url = f"https://api.line.me/v2/bot/group/{ident}/summary"
        name_field = "groupName"

    try:
        import httpx

        async with httpx.AsyncClient(timeout=10.0) as client:
            resp = await client.get(
                url, headers={"Authorization": f"Bearer {token}"},
            )
        if resp.status_code == 200:
            name = resp.json().get(name_field) or ident
            _resolve_cache[cache_key] = (now + _RESOLVE_TTL_SECONDS, name)
            return {"type": type, "id": ident, "name": name, "resolved": True, "cached": False}
        log.debug("LINE resolve %s %s -> HTTP %s", type, ident, resp.status_code)
    except Exception as exc:
        log.debug("LINE resolve %s %s failed: %s", type, ident, exc)

    # Fallback: echo the id.
    return {"type": type, "id": ident, "name": ident, "resolved": False, "cached": False}
