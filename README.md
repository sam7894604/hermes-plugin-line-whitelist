<!-- Language: **English** · [繁體中文](./README_zh-TW.md) -->

# hermes-plugin-line-whitelist

> **English** · [繁體中文 (Traditional Chinese)](./README_zh-TW.md)

A **whitelist / authorization subsystem** for the [Hermes](https://github.com/NousResearch/hermes-agent) **LINE adapter**. It turns the LINE bot from "anyone who finds it can talk to it" into a managed, admin-approved allow-list — with per-scope (DM / group / room) isolation, `@mention` gating, a polite English rejection for unauthorized sources, deduped admin notifications, passive group-context recording, media retention control, and quote-reply awareness. It ships with a **Dashboard CRUD plugin** and an **agent-mediated approval tool**.

Driving use-case: a travel group where everyone posts expenses all day, the bot stays quiet and just *observes*, and at night the admin `@`s it once — "settle up" — and it has the whole day's context, including messages from people who aren't individually authorized.

---

## ⚠️ Read this first: the master switch

The whitelist only gates if the adapter's **allow-all escape hatch is OFF**:

```
LINE_ALLOW_ALL_USERS=false      # in your Hermes .env / environment
```

If `LINE_ALLOW_ALL_USERS=true` (a dev-only convenience), **every source is treated as authorized** and the whitelist does nothing — no rejection, no admin notification, ever. Setting it to `false` is what activates gating. Your existing `LINE_ALLOWED_USERS` / `LINE_ALLOWED_GROUPS` env values are kept as a backward-compatible overlay, so anyone already allow-listed via env keeps access.

---

## What's standalone vs. what's an adapter patch (honest split)

This subsystem is **partly** a standalone bundle and **partly** an in-place edit to the LINE adapter. The split is honest:

| Part | Form | Where it goes |
|------|------|---------------|
| `WhitelistStore` (`whitelist_store.py`) | **standalone file** | `plugins/platforms/line/` |
| Unauthorized-notify helper (`whitelist_notify.py`) | **standalone file** | `plugins/platforms/line/` |
| Dashboard plugin (`dashboard/`) | **standalone plugin** | `plugins/platforms/line/dashboard/` |
| Agent approval tool (`line_whitelist_tool.py`) | **standalone file** | `tools/` |
| Adapter runtime behavior (gate / `@mention` / reject / observed / media / quote / name resolution) | **patch** — cannot be a separate file | `patches/line-adapter.diff` → `plugins/platforms/line/adapter.py` |
| Toolset + Dashboard-discovery wiring | **patch** | `patches/hermes-core-wiring.diff` |

The standalone files live under [`src/`](./src) mirroring their in-Hermes paths. The adapter part is a diff because it edits the adapter's inbound path — see [`docs/adapter-integration.md`](./docs/adapter-integration.md). Reference implementation: **fork PR #6** on `sam7894604/hermes-agent` (`feat/line-whitelist-management`).

---

## Features

- 🟢 **Hot-reload allow-list, no restart.** The store is backed by `config.yaml` (`platforms.line.*`) and read live on every message via Hermes' `load_config()` (which auto-invalidates on file change). Add a group in the Dashboard → the next message is gated by it.
- 🟢 **Scope isolation.** DM / group / room are three independent allow-lists. Being allowed in a group does **not** authorize a private DM, and vice-versa.
- 🟢 **Admin no-delete protection.** Admin userIds cannot be removed from the whitelist by the tool or the Dashboard (a `WhitelistError` is raised) — you can't accidentally lock yourself out.
- 🟢 **`@mention` gating.** Whitelisted groups default to `requires_mention: true` — the bot only responds when `@`-ed; other messages become passive context.
- 🟢 **Polite rejection + deduped notify.** An unauthorized source that `@`s the bot (or DMs it) gets one throttled English "not authorized, an admin has been notified" reply, and the admin is notified **once per source** (dedup), on the channel you choose.
- 🟢 **Passive observed context.** In a whitelisted group, non-`@` messages are recorded as `observed` context (never triggering the agent) with `[name|userId]` attribution, so a later `@` turn has the full picture.
- 🟢 **Media policy + retention.** Video/audio are dropped from passive recording; image/file are recorded as placeholders, retained `retention_days` (default 3, per-source override).
- 🟢 **Quote-reply awareness.** A reply that quotes an earlier message (`quotedMessageId`) injects the quoted original into the turn as context.
- 🟢 **Dashboard + approval tool.** Manage the whitelist from the web dashboard (CRUD + communication records + display-name resolution), or let an admin approve entries in-chat via the `line_whitelist` tool.

---

## Configuration reference (`config.yaml` → `platforms.line`)

```yaml
platforms:
  line:
    whitelist:                       # the allow-list (scope-isolated)
      users:  ["U..."]               # DM scope
      groups: ["C..."]               # group scope
      rooms:  ["R..."]               # room scope
    admins: ["U..."]                 # admin userIds — no-delete protected;
                                     #   the only ones who may approve/remove
    requires_mention: true           # group default: only respond when @-ed
    unauthorized_notify: "telegram:521703862"
                                     # where to notify on an unauthorized source.
                                     #   Format: "<platform>:<chat_id>" — e.g.
                                     #   "telegram:123", "discord:456", or a bare
                                     #   LINE id. null => the LINE home channel.
    retention_days: 3                # media retention (global default)
    allow_all_users: false           # config-side mirror of LINE_ALLOW_ALL_USERS
    meta:                            # optional per-source metadata + overrides
      groups:
        "C...": { added_by: "U...", note: "Bali trip", requires_mention: true, retention_days: 7 }
      users:
        "U...": { added_by: "U...", note: "..." }
    media:                           # media type policy
      keep_types: ["image", "file"]  # recorded (subject to retention_days)
      drop_types: ["video", "audio"] # dropped entirely, never fetched
    observe_unmentioned: true        # passive observed-context recording on/off
    unauthorized_seen: {}            # runtime dedup/throttle state (auto-written)
```

**Enable the admin approval tool** (its own toolset, default-off) by adding it to the LINE platform's toolset list:

```yaml
platform_toolsets:
  line:
    - ...            # your existing LINE toolsets
    - line_whitelist # <-- add this; requires a gateway restart to take effect
```

**Environment (master switch + backward-compatible overlay):**

```
LINE_ALLOW_ALL_USERS=false          # REQUIRED for gating (see the warning above)
LINE_ALLOWED_USERS=U...,U...        # optional env overlay (kept working)
LINE_ALLOWED_GROUPS=C...            # optional env overlay
```

---

## Install

1. **Copy the standalone files** into your Hermes tree (do **not** overwrite the adapter's own `__init__.py`):

   ```bash
   H=<your-hermes-tree>
   cp src/plugins/platforms/line/whitelist_store.py   "$H/plugins/platforms/line/"
   cp src/plugins/platforms/line/whitelist_notify.py  "$H/plugins/platforms/line/"
   cp -r src/plugins/platforms/line/dashboard         "$H/plugins/platforms/line/"
   cp src/tools/line_whitelist_tool.py                "$H/tools/"
   ```

2. **Apply the adapter + wiring patches** (see [`docs/adapter-integration.md`](./docs/adapter-integration.md)):

   ```bash
   cd "$H"
   git apply /path/to/patches/line-adapter.diff
   git apply /path/to/patches/hermes-core-wiring.diff
   ```

   If your install is a *pip/site-packages* build (not editable), copy the files into the venv's `site-packages` and apply the adapter edits there, or rebuild your package. Additive edits to `toolsets.py` / `tools_config.py` / `web_server.py` / `delegate_tool.py` are anchored on the existing `cronjob` entries.

3. **Configure** `config.yaml` + env (see the reference above). Set `LINE_ALLOW_ALL_USERS=false`, your `admins`, `unauthorized_notify`, and your `whitelist.groups` / `whitelist.users`. Add `line_whitelist` to `platform_toolsets.line`.

4. **Apply.** The whitelist gate, admin, notify, and `requires_mention` **hot-reload** (no restart). The `line_whitelist` toolset and any env change (`LINE_ALLOW_ALL_USERS`) require a **gateway restart**.

---

## Usage

### Dashboard
The Dashboard plugin adds a **LINE Whitelist** tab (auto-discovered from `plugins/platforms/line/dashboard/manifest.json`). From it you can:
- List / add / remove whitelist entries (per scope; admin no-delete enforced with a 4xx).
- View communication records (reuses the existing session store; filtered to `line:*` sessions).
- Resolve display names (LINE profile / group-summary API, TTL-cached, falls back to raw IDs).

### Admin approval tool (in-chat)
Once `line_whitelist` is in `platform_toolsets.line` and the gateway is restarted, an **admin** (a userId in `admins`) can, in a LINE chat with the bot:
- `line_whitelist(action="approve", scope="group", id="C...")` — add a group.
- `line_whitelist(action="list")` — list current entries.
- `line_whitelist(action="remove", scope="group", id="C...")` — remove (blocked for admin ids).

Non-admins are refused; a non-admin `propose` → admin `approve_pending` staging flow is also provided. The tool is **excluded from subagents** (can't self-approve).

### What each source sees (with gating on)
- **Whitelisted group:** bot is quiet, passively records everyone's messages; responds only when you `@` it; video dropped, image/file kept `retention_days`; quoting a message + `@` lets the agent see the quoted original.
- **Non-whitelisted group:** only replies (English "not authorized" + notifies the admin, once per source) when `@`-ed; otherwise silent and not recorded.
- **Admin DM / allow-listed DM:** normal 1:1 conversation, no `@` needed.
- **Stranger DM:** English "not authorized" reply + admin notified (once), attempt recorded for the Dashboard.

---

## Rollback

Every change is reversible:

- **Config / whitelist:** restore your `config.yaml` backup (or remove the `platforms.line.whitelist` / `admins` / `line_whitelist` toolset entries).
- **Master switch:** set `LINE_ALLOW_ALL_USERS` back to its previous value in `.env`.
- **Adapter patch:** `git apply -R patches/line-adapter.diff` (and `-R patches/hermes-core-wiring.diff`), or restore the adapter file from backup.
- **Standalone files:** delete `whitelist_store.py`, `whitelist_notify.py`, `dashboard/`, `tools/line_whitelist_tool.py`.

Restart the gateway for env / toolset / adapter changes to take effect.

---

## Tests

```bash
python -m pytest tests/ -q      # or: python run_tests.py  (per-file isolation)
```

The harness ([`tests/conftest.py`](./tests/conftest.py)) stubs the Hermes host modules automatically, so the tests run in a plain checkout (no Hermes install needed — only `pytest`, `fastapi`, `httpx`). Inside a real Hermes tree the genuine modules win, so the suite doubles as an integration smoke check.

**59 tests**: WhitelistStore (scope isolation, admin no-delete, dedup/throttle, retention, hot-reload) · notify routing (incl. cross-platform `telegram:` targets) · the approval tool (admin gating, staging) · the Dashboard plugin (CRUD, records, name resolution).

---

## Limitations & notes

- **Not a drop-in single plugin.** The runtime behavior is an adapter patch; treat this repo as a bundle + install guide, not a `hermes plugins install` one-liner.
- **Observed group context** relies on shared group sessions — run the LINE platform with `group_sessions_per_user=false` (as the Telegram observed mechanism assumes) so a whitelisted group's passive context loads into the addressed turn.
- **Media retention** currently records image/file as metadata placeholders; binary retention + a cleanup cron are a documented follow-up.
- **Quote reconstruction** degrades gracefully if the quoted original has aged out of the transcript.

---

## License

[MIT](./LICENSE) © 2026 sam7894604
