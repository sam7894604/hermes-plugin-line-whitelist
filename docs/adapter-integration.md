# Adapter integration (the non-standalone part)

Most of this subsystem ships as **standalone files** you drop into a Hermes tree
(`whitelist_store.py`, `whitelist_notify.py`, the `line_whitelist` tool, the
Dashboard plugin). But the **runtime behavior** — the actual gate, `@mention`
gating, unauthorized reply, observed passive-context recording, media filtering,
quote-reply lookup, and display-name resolution — lives *inside* the LINE
adapter's inbound path. That cannot be a separate file: it edits
`plugins/platforms/line/adapter.py`.

So that part is shipped honestly as a **patch**, not a plugin file:

- `patches/line-adapter.diff` — the LINE adapter changes (gate / @mention /
  reject / observed / media / quote / name resolution **+ pending-queue
  `record_attempt` recording**). Reference: **fork PR #6 + #7** on
  `sam7894604/hermes-agent`.
- `patches/hermes-core-wiring.diff` — tiny additive edits to shared Hermes
  files that register the `line_whitelist` toolset and let the Dashboard plugin
  be discovered (see below).
- `patches/telegram-discord-cards.diff` — **optional** additive edits to the
  Telegram + Discord adapters that add interactive ✅Approve/⛔Ignore/➖Skip
  **decision cards** for the unauthorized-source notification (button taps call
  `approve_pending`/`ignore_pending`). Without this patch the notification
  degrades cleanly to plain text (the `whitelist_notify` bridge falls back when
  no `send_whitelist_decision` method is present). Reference: **fork PR #7**.
  ⚠️ These edit the live Telegram/Discord adapters — apply additively and
  restart the gateway. The standalone LINE-plugin files never need them.

## What the adapter patch does

Applied to `plugins/platforms/line/adapter.py`, it:

1. **Hot-reload gate.** `_source_authorized(source)` = the existing static env
   allow-lists (`LINE_ALLOWED_*`, kept as a backward-compatible overlay) **OR**
   `WhitelistStore.is_allowed(...)` read live from `config.yaml` on every message
   — so whitelist edits take effect on the next message, no restart.
2. **`@mention` gating.** Parses `message.mention.mentionees[]`; a whitelisted
   group with `requires_mention` (default true) only triggers the agent when the
   bot is `@`-ed. Non-`@` messages route to passive recording.
3. **Unauthorized policy.** A stranger DM, or an `@`-ed message in a
   non-whitelisted group, gets a throttled English "not authorized" reply +
   a deduped admin notification (via `whitelist_notify`). Non-`@` messages in a
   non-whitelisted group stay silent and are **not** recorded.
4. **Observed passive context.** In a whitelisted group, non-`@` messages are
   recorded (`observed=True`) into a shared, chat-scoped session with
   `[name|userId]` attribution — reusing Hermes' Telegram-style observed-group
   mechanism — so a later `@`-mention turn sees the whole group's context.
5. **Media policy.** Video/audio are dropped from passive recording; image/file
   are recorded as a lightweight placeholder (subject to `retention_days`).
6. **Quote reply.** A message carrying `quotedMessageId` reverse-looks-up the
   quoted original in the transcript and injects it as context.
7. **Name resolution.** `_LineClient.get_profile` / `get_group_summary` /
   `get_member_name` (TTL-cached, best-effort) fill display names, falling back
   to raw IDs.

## How to apply

```bash
cd <your-hermes-tree>
git apply /path/to/hermes-plugin-line-whitelist/patches/line-adapter.diff
git apply /path/to/hermes-plugin-line-whitelist/patches/hermes-core-wiring.diff
```

If your adapter has diverged and `git apply` refuses, apply the changes by hand
using the diff as a guide, or cherry-pick from fork PR #6. The adapter changes
are additive and self-contained (new helper methods + a restructured
`_dispatch_event` / `_handle_message_event`); they do not remove existing
behavior.

## The three core-wiring edits (`hermes-core-wiring.diff`)

These are additive and anchored on the existing `cronjob` toolset entries:

- `toolsets.py` — add a static `line_whitelist` toolset (`tools: ["line_whitelist"]`).
- `hermes_cli/tools_config.py` — expose it in the configurator + mark it
  default-**off** (opt-in, admin only).
- `tools/delegate_tool.py` — add `line_whitelist` to `DELEGATE_BLOCKED_TOOLS`
  so subagents can't self-approve access-control changes.
- `hermes_cli/web_server.py` — make `_discover_dashboard_plugins` scan
  `plugins/platforms/<name>/dashboard/` so the LINE Dashboard plugin mounts.

(The web_server edit is included in `hermes-core-wiring.diff` too.)

## Verifying after install

```bash
# from a neutral cwd, in the Hermes venv
python -c "import plugins.platforms.line.adapter as a; print('adapter OK', hasattr(a,'WhitelistStore'))"
python -c "import toolsets; print('toolset', toolsets.get_toolset('line_whitelist')['tools'])"
python -c "from hermes_cli.web_server import _discover_dashboard_plugins as d; print([p['name'] for p in d()])"
```

See the main [README](../README.md) for the full configuration + go-live steps.
