/**
 * Hermes LINE Whitelist — Dashboard Plugin
 *
 * Admin surface for the LINE bot allowlist backed by the Phase-1
 * WhitelistStore. Lets an operator CRUD the users / groups / rooms
 * allowlist, browse LINE communication records, and resolve LINE ids to
 * display names via the plugin's /resolve endpoint.
 *
 * Calls the plugin's backend at /api/plugins/line-whitelist/.
 *
 * Plain IIFE, no build step (mirrors plugins/kanban/dashboard/dist/index.js).
 * Uses window.__HERMES_PLUGIN_SDK__ for React + shadcn primitives and
 * SDK.fetchJSON for authenticated calls; registers via
 * window.__HERMES_PLUGINS__.register.
 */
(function () {
  "use strict";

  const SDK = window.__HERMES_PLUGIN_SDK__;
  if (!SDK) return;

  const { React } = SDK;
  const h = React.createElement;
  const {
    Card, CardContent,
    Badge, Button, Input, Label, Select, SelectOption,
  } = SDK.components;
  const { useState, useEffect, useCallback } = SDK.hooks;
  const cn = (SDK.utils && SDK.utils.cn) || function () {
    return Array.prototype.filter.call(arguments, Boolean).join(" ");
  };
  const timeAgo = (SDK.utils && SDK.utils.timeAgo) || function (ts) {
    if (!ts) return "";
    try { return new Date(ts * 1000).toLocaleString(); } catch (e) { return String(ts); }
  };

  const API = "/api/plugins/line-whitelist";

  // scope -> {bucket key in the /whitelist response, human label, chip class}
  const SCOPES = [
    { scope: "user",  bucket: "users",  label: "Users",  prefix: "U", resolvable: "user" },
    { scope: "group", bucket: "groups", label: "Groups", prefix: "C", resolvable: "group" },
    { scope: "room",  bucket: "rooms",  label: "Rooms",  prefix: "R", resolvable: null },
  ];

  // -------------------------------------------------------------------------
  // Whitelist section — chip add/remove per scope (models ChannelsPage.tsx)
  // -------------------------------------------------------------------------

  function WhitelistSection(props) {
    const { data, onAdd, onRemove, busy, error, resolveName } = props;

    // Per-scope draft input state for the "add" row.
    const [draftScope, setDraftScope] = useState("user");
    const [draftId, setDraftId] = useState("");
    const [draftNote, setDraftNote] = useState("");

    const submit = useCallback(function () {
      const id = (draftId || "").trim();
      if (!id) return;
      onAdd({ scope: draftScope, id: id, note: (draftNote || "").trim() || undefined });
      setDraftId("");
      setDraftNote("");
    }, [draftScope, draftId, draftNote, onAdd]);

    return h(Card, { className: "hermes-line-card" },
      h(CardContent, { className: "hermes-line-card-body" },
        h("div", { className: "hermes-line-section-head" }, "Allowlist"),

        // --- add row -------------------------------------------------------
        h("div", { className: "hermes-line-addrow" },
          h(Select, {
            value: draftScope,
            onValueChange: setDraftScope,
            className: "hermes-line-scope-select",
          }, SCOPES.map(function (s) {
            return h(SelectOption, { key: s.scope, value: s.scope }, s.label);
          })),
          h(Input, {
            value: draftId,
            placeholder: "LINE id (e.g. U1234… / C1234… / R1234…)",
            onChange: function (e) { setDraftId(e.target.value); },
            onKeyDown: function (e) { if (e.key === "Enter") submit(); },
            className: "hermes-line-id-input",
          }),
          h(Input, {
            value: draftNote,
            placeholder: "note (optional)",
            onChange: function (e) { setDraftNote(e.target.value); },
            onKeyDown: function (e) { if (e.key === "Enter") submit(); },
            className: "hermes-line-note-input",
          }),
          h(Button, {
            size: "sm",
            disabled: !!busy || !draftId.trim(),
            onClick: submit,
          }, "Add"),
        ),
        error ? h("div", { className: "hermes-line-msg-err" }, error) : null,

        // --- per-scope chip lists -----------------------------------------
        SCOPES.map(function (s) {
          const entries = (data && data[s.bucket]) || [];
          return h("div", { key: s.scope, className: "hermes-line-scope-block" },
            h("div", { className: "hermes-line-scope-title" },
              s.label,
              h(Badge, { className: "hermes-line-count" }, String(entries.length)),
            ),
            entries.length === 0
              ? h("div", { className: "hermes-line-empty" }, "none")
              : h("div", { className: "hermes-line-chips" },
                  entries.map(function (entry) {
                    return h(WhitelistChip, {
                      key: (entry.id || entry) + ":" + s.scope,
                      entry: entry,
                      scope: s.scope,
                      resolvable: s.resolvable,
                      resolveName: resolveName,
                      busy: busy,
                      onRemove: onRemove,
                    });
                  })
                ),
          );
        }),
      ),
    );
  }

  // A single allowlist entry rendered as a removable chip. Shows the resolved
  // display name (looked up lazily via /resolve) alongside the raw id.
  function WhitelistChip(props) {
    const { entry, scope, resolvable, resolveName, onRemove, busy } = props;
    const id = typeof entry === "string" ? entry : entry.id;
    const note = typeof entry === "string" ? "" : (entry.note || "");
    const [name, setName] = useState(null);

    useEffect(function () {
      let alive = true;
      if (resolvable) {
        resolveName(resolvable, id).then(function (n) {
          if (alive && n && n !== id) setName(n);
        });
      }
      return function () { alive = false; };
    }, [resolvable, id, resolveName]);

    const title = note
      ? id + " — " + note
      : id;

    return h("span", { className: "hermes-line-chip", title: title },
      h("span", { className: "hermes-line-chip-label" }, name || id),
      note ? h("span", { className: "hermes-line-chip-note" }, note) : null,
      h("button", {
        className: "hermes-line-chip-x",
        disabled: !!busy,
        title: "Remove",
        onClick: function () { onRemove(scope, id); },
      }, "×"),
    );
  }

  // -------------------------------------------------------------------------
  // Authorized section — the full authorized picture (store ∪ env overlay)
  // -------------------------------------------------------------------------
  //
  // Distinct from the Pending queue and from the editable Allowlist chips:
  // this lists every id the LINE gate would authorize — STORE entries AND the
  // env-overlay ids (LINE_ALLOWED_USERS/GROUPS/ROOMS) that are otherwise
  // invisible. Locked rows (admin or env-managed) show a 🔒 and no delete.

  function AuthorizedRow(props) {
    const { entry, scope, resolvable, resolveName, onRemove, busy } = props;
    const id = entry.id;
    const locked = !!entry.locked;
    const [name, setName] = useState(entry.name || null);

    useEffect(function () {
      let alive = true;
      if (!name && resolvable) {
        resolveName(resolvable, id).then(function (n) {
          if (alive && n && n !== id) setName(n);
        });
      }
      return function () { alive = false; };
    }, [resolvable, id, resolveName]);  // eslint-disable-line

    const badgeCls = entry.source === "env"
      ? "hermes-line-src hermes-line-src--env"
      : "hermes-line-src hermes-line-src--store";

    return h("div", { className: "hermes-line-auth-row", title: entry.note || "" },
      h("div", { className: "hermes-line-auth-main" },
        h("span", { className: "hermes-line-auth-name" }, name || id),
        entry.admin ? h(Badge, { className: "hermes-line-src hermes-line-src--admin" }, "admin") : null,
        h(Badge, { className: badgeCls }, entry.source || "store"),
        entry.also_in_env ? h(Badge, { className: "hermes-line-src hermes-line-src--env" }, "+env") : null,
        locked ? h("span", { className: "hermes-line-lock", title: entry.admin ? "admin (protected)" : "managed via env" }, "🔒") : null,
      ),
      h("div", { className: "hermes-line-auth-meta" },
        h("span", { className: "hermes-line-auth-id" }, id),
        h("span", null, entry.added_at ? timeAgo(entry.added_at) : ""),
        !locked
          ? h("button", {
              className: "hermes-line-chip-x hermes-line-auth-x",
              disabled: !!busy,
              title: "Remove",
              onClick: function () { onRemove(scope, id); },
            }, "×")
          : null,
      ),
    );
  }

  function AuthorizedSection(props) {
    const { data, loading, busy, onRemove, resolveName } = props;
    const total = SCOPES.reduce(function (n, s) {
      return n + (((data && data[s.bucket]) || []).length);
    }, 0);

    return h(Card, { className: "hermes-line-card" },
      h(CardContent, { className: "hermes-line-card-body" },
        h("div", { className: "hermes-line-section-head" },
          "Authorized / 已授權清單",
          h(Badge, { className: "hermes-line-count" }, String(total)),
        ),
        loading
          ? h("div", { className: "hermes-line-empty" }, "loading…")
          : SCOPES.map(function (s) {
              const entries = (data && data[s.bucket]) || [];
              return h("div", { key: s.scope, className: "hermes-line-scope-block" },
                h("div", { className: "hermes-line-scope-title" },
                  s.label,
                  h(Badge, { className: "hermes-line-count" }, String(entries.length)),
                ),
                entries.length === 0
                  ? h("div", { className: "hermes-line-empty" }, "none")
                  : h("div", { className: "hermes-line-auth-list" },
                      entries.map(function (entry) {
                        return h(AuthorizedRow, {
                          key: entry.id + ":" + s.scope,
                          entry: entry,
                          scope: s.scope,
                          resolvable: s.resolvable,
                          resolveName: resolveName,
                          busy: busy,
                          onRemove: onRemove,
                        });
                      })
                    ),
              );
            }),
      ),
    );
  }

  // -------------------------------------------------------------------------
  // Pending section — attempts awaiting an approve / ignore decision
  // -------------------------------------------------------------------------

  function PendingSection(props) {
    const { pending, loading, busy, onApprove, onIgnore } = props;

    return h(Card, { className: "hermes-line-card" },
      h(CardContent, { className: "hermes-line-card-body" },
        h("div", { className: "hermes-line-section-head" },
          "Pending / 待審清單",
          h(Badge, { className: "hermes-line-count" }, String((pending || []).length)),
        ),
        loading
          ? h("div", { className: "hermes-line-empty" }, "loading…")
          : ((pending || []).length === 0
              ? h("div", { className: "hermes-line-empty" }, "No pending attempts.")
              : h("div", { className: "hermes-line-pending" },
                  (pending || []).map(function (p) {
                    const label = p.name || p.id;
                    return h("div", { key: p.id, className: "hermes-line-pending-row" },
                      h("div", { className: "hermes-line-pending-main" },
                        h("span", { className: "hermes-line-pending-name" }, label),
                        h(Badge, { className: "hermes-line-count" },
                          String(p.count == null ? "?" : p.count)),
                      ),
                      h("div", { className: "hermes-line-pending-meta" },
                        h("span", null, (p.platform || "line") + " · " + (p.source_type || "?")),
                        h("span", null, timeAgo(p.last_seen || p.first_seen)),
                      ),
                      h("div", { className: "hermes-line-pending-actions" },
                        h(Button, {
                          size: "sm",
                          disabled: !!busy,
                          onClick: function () { onApprove(p.id); },
                        }, "加入白名單 / Approve"),
                        h(Button, {
                          size: "sm",
                          variant: "outline",
                          disabled: !!busy,
                          onClick: function () { onIgnore(p.id); },
                        }, "忽略 / Ignore"),
                      ),
                    );
                  })
                )
            ),
      ),
    );
  }

  // -------------------------------------------------------------------------
  // Records section — LINE communication records (reuses session data)
  // -------------------------------------------------------------------------

  function RecordsSection(props) {
    const { records, loading, onOpen, selected, messages, msgLoading } = props;

    return h(Card, { className: "hermes-line-card" },
      h(CardContent, { className: "hermes-line-card-body" },
        h("div", { className: "hermes-line-section-head" }, "Communication records"),
        loading
          ? h("div", { className: "hermes-line-empty" }, "loading…")
          : (records.length === 0
              ? h("div", { className: "hermes-line-empty" }, "No LINE sessions yet.")
              : h("div", { className: "hermes-line-records" },
                  records.map(function (r) {
                    const isSel = selected === r.session_id;
                    return h("div", {
                      key: r.session_id,
                      className: cn(
                        "hermes-line-record",
                        isSel && "hermes-line-record--sel",
                      ),
                      onClick: function () { onOpen(r.session_id); },
                    },
                      h("div", { className: "hermes-line-record-main" },
                        h("span", { className: "hermes-line-record-title" },
                          r.title || r.session_key || r.session_id),
                        h(Badge, { className: "hermes-line-count" },
                          String(r.message_count == null ? "?" : r.message_count)),
                      ),
                      h("div", { className: "hermes-line-record-meta" },
                        h("span", { className: "hermes-line-record-key" }, r.session_key || ""),
                        h("span", null, timeAgo(r.last_active || r.started_at)),
                      ),
                    );
                  })
                )
            ),

        // Selected session's messages.
        selected ? h("div", { className: "hermes-line-messages" },
          h("div", { className: "hermes-line-section-subhead" }, "Messages"),
          msgLoading
            ? h("div", { className: "hermes-line-empty" }, "loading…")
            : ((messages || []).length === 0
                ? h("div", { className: "hermes-line-empty" }, "no messages")
                : (messages || []).map(function (m, i) {
                    return h("div", {
                      key: i,
                      className: cn("hermes-line-msg", "hermes-line-msg--" + (m.role || "user")),
                    },
                      h("span", { className: "hermes-line-msg-role" }, m.role || "user"),
                      h("span", { className: "hermes-line-msg-body" },
                        typeof m.content === "string"
                          ? m.content
                          : JSON.stringify(m.content)),
                    );
                  })
              )
        ) : null,
      ),
    );
  }

  // -------------------------------------------------------------------------
  // Top-level page
  // -------------------------------------------------------------------------

  function LineWhitelistPage() {
    const [whitelist, setWhitelist] = useState({ users: [], groups: [], rooms: [] });
    const [authorized, setAuthorized] = useState({ users: [], groups: [], rooms: [] });
    const [authLoading, setAuthLoading] = useState(true);
    const [wlError, setWlError] = useState(null);
    const [busy, setBusy] = useState(false);

    const [pending, setPending] = useState([]);
    const [pendLoading, setPendLoading] = useState(true);

    const [records, setRecords] = useState([]);
    const [recLoading, setRecLoading] = useState(true);
    const [selected, setSelected] = useState(null);
    const [messages, setMessages] = useState([]);
    const [msgLoading, setMsgLoading] = useState(false);

    // ---- name resolution cache (client-side, backs /resolve TTL cache) ----
    const resolveName = useCallback(function (type, id) {
      return SDK.fetchJSON(
        `${API}/resolve?type=${encodeURIComponent(type)}&id=${encodeURIComponent(id)}`
      ).then(function (r) {
        return (r && r.name) || id;
      }).catch(function () { return id; });
    }, []);

    // ---- whitelist load ---------------------------------------------------
    const loadWhitelist = useCallback(function () {
      return SDK.fetchJSON(`${API}/whitelist`)
        .then(function (data) {
          setWhitelist({
            users: (data && data.users) || [],
            groups: (data && data.groups) || [],
            rooms: (data && data.rooms) || [],
          });
          setWlError(null);
        })
        .catch(function (e) { setWlError(String(e && e.message || e)); });
    }, []);

    // ---- authorized load (store ∪ env overlay) ----------------------------
    const loadAuthorized = useCallback(function () {
      setAuthLoading(true);
      return SDK.fetchJSON(`${API}/authorized`)
        .then(function (data) {
          setAuthorized({
            users: (data && data.users) || [],
            groups: (data && data.groups) || [],
            rooms: (data && data.rooms) || [],
          });
        })
        .catch(function () { setAuthorized({ users: [], groups: [], rooms: [] }); })
        .finally(function () { setAuthLoading(false); });
    }, []);

    // ---- records load -----------------------------------------------------
    const loadRecords = useCallback(function () {
      setRecLoading(true);
      return SDK.fetchJSON(`${API}/records?limit=50`)
        .then(function (data) { setRecords((data && data.records) || []); })
        .catch(function () { setRecords([]); })
        .finally(function () { setRecLoading(false); });
    }, []);

    // ---- pending load -----------------------------------------------------
    const loadPending = useCallback(function () {
      setPendLoading(true);
      return SDK.fetchJSON(`${API}/pending`)
        .then(function (data) { setPending((data && data.pending) || []); })
        .catch(function () { setPending([]); })
        .finally(function () { setPendLoading(false); });
    }, []);

    useEffect(function () {
      loadWhitelist(); loadAuthorized(); loadPending(); loadRecords();
    }, [loadWhitelist, loadAuthorized, loadPending, loadRecords]);

    // ---- add / remove -----------------------------------------------------
    const onAdd = useCallback(function (body) {
      setBusy(true);
      SDK.fetchJSON(`${API}/whitelist`, {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify(body),
      })
        .then(function () { setWlError(null); return Promise.all([loadWhitelist(), loadAuthorized()]); })
        .catch(function (e) { setWlError(String(e && e.message || e)); })
        .finally(function () { setBusy(false); });
    }, [loadWhitelist, loadAuthorized]);

    const onRemove = useCallback(function (scope, id) {
      setBusy(true);
      SDK.fetchJSON(
        `${API}/whitelist/${encodeURIComponent(scope)}/${encodeURIComponent(id)}`,
        { method: "DELETE" }
      )
        .then(function (res) {
          if (res && res.env_managed) {
            setWlError(id + " is managed via env (LINE_ALLOWED_*) — edit the env var, not the dashboard.");
          } else {
            setWlError(null);
          }
          return Promise.all([loadWhitelist(), loadAuthorized()]);
        })
        .catch(function (e) { setWlError(String(e && e.message || e)); })
        .finally(function () { setBusy(false); });
    }, [loadWhitelist, loadAuthorized]);

    // ---- approve / ignore a pending attempt -------------------------------
    const onApprove = useCallback(function (id) {
      setBusy(true);
      SDK.fetchJSON(`${API}/pending/${encodeURIComponent(id)}/approve`, {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({}),
      })
        .then(function () { return Promise.all([loadPending(), loadWhitelist(), loadAuthorized()]); })
        .catch(function (e) { setWlError(String(e && e.message || e)); })
        .finally(function () { setBusy(false); });
    }, [loadPending, loadWhitelist, loadAuthorized]);

    const onIgnore = useCallback(function (id) {
      setBusy(true);
      SDK.fetchJSON(`${API}/pending/${encodeURIComponent(id)}/ignore`, {
        method: "POST",
      })
        .then(function () { return loadPending(); })
        .catch(function (e) { setWlError(String(e && e.message || e)); })
        .finally(function () { setBusy(false); });
    }, [loadPending]);

    // ---- open a record's messages ----------------------------------------
    const onOpen = useCallback(function (sessionId) {
      if (selected === sessionId) { setSelected(null); setMessages([]); return; }
      setSelected(sessionId);
      setMsgLoading(true);
      SDK.fetchJSON(`${API}/records?session_id=${encodeURIComponent(sessionId)}`)
        .then(function (data) { setMessages((data && data.messages) || []); })
        .catch(function () { setMessages([]); })
        .finally(function () { setMsgLoading(false); });
    }, [selected]);

    return h("div", { className: "hermes-line-page" },
      h("div", { className: "hermes-line-header" },
        h("h2", { className: "hermes-line-title" }, "LINE Whitelist"),
        h(Button, { size: "sm", variant: "outline", onClick: function () {
          loadWhitelist(); loadAuthorized(); loadPending(); loadRecords();
        } }, "Refresh"),
      ),
      h(PendingSection, {
        pending: pending,
        loading: pendLoading,
        busy: busy,
        onApprove: onApprove,
        onIgnore: onIgnore,
      }),
      h(AuthorizedSection, {
        data: authorized,
        loading: authLoading,
        busy: busy,
        onRemove: onRemove,
        resolveName: resolveName,
      }),
      h(WhitelistSection, {
        data: whitelist,
        onAdd: onAdd,
        onRemove: onRemove,
        busy: busy,
        error: wlError,
        resolveName: resolveName,
      }),
      h(RecordsSection, {
        records: records,
        loading: recLoading,
        onOpen: onOpen,
        selected: selected,
        messages: messages,
        msgLoading: msgLoading,
      }),
    );
  }

  // -------------------------------------------------------------------------
  // Register
  // -------------------------------------------------------------------------

  if (window.__HERMES_PLUGINS__ && typeof window.__HERMES_PLUGINS__.register === "function") {
    window.__HERMES_PLUGINS__.register("line-whitelist", LineWhitelistPage);
  }
})();
