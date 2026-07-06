<!-- Language: [English](./README.md) · **繁體中文** -->

# hermes-plugin-line-whitelist

> [English](./README.md) · **繁體中文 (Traditional Chinese)**

給 [Hermes](https://github.com/NousResearch/hermes-agent) **LINE adapter** 的**白名單／授權子系統**。把 LINE bot 從「誰找到都能用」變成受管、管理員核准的白名單——含 **scope 隔離**（DM／群組／房間各自獨立）、`@mention` gating、對未授權來源的英文婉拒、去重的管理員通知、被動群組脈絡記錄、媒體保留控制、引用回覆感知。附一個 **Dashboard CRUD 插件** 與一個 **agent 中介核准工具**。

驅動情境：一個旅遊群，大家整天把消費 po 進來，bot 平常不出聲、只**被動記錄**；晚上管理員 `@` 它一次「幫大家結算」，它就有整天的完整脈絡——包含那些沒被個別授權的旅伴的訊息。

---

## ⚠️ 先看這個：總開關

白名單只有在 adapter 的 **allow-all 逃生門關閉**時才會 gate：

```
LINE_ALLOW_ALL_USERS=false      # 在你的 Hermes .env / 環境變數
```

若 `LINE_ALLOW_ALL_USERS=true`（開發用的方便開關），**所有來源都被當成已授權**，白名單形同虛設——永遠不會拒絕、不會通知管理員。設成 `false` 才會**啟用 gating**。你原有的 `LINE_ALLOWED_USERS` / `LINE_ALLOWED_GROUPS` env 值會保留為**向後相容 overlay**，故已用 env 允許的對象仍保有存取。

---

## 哪些是獨立元件、哪些是 adapter patch（誠實切分）

這個子系統**一部分**是獨立 bundle、**一部分**是對 LINE adapter 的原地修改，誠實標明：

| 部分 | 形式 | 放到哪 |
|------|------|--------|
| `WhitelistStore`（`whitelist_store.py`）| **獨立檔** | `plugins/platforms/line/` |
| 未授權通知 helper（`whitelist_notify.py`）| **獨立檔** | `plugins/platforms/line/` |
| Dashboard 插件（`dashboard/`）| **獨立插件** | `plugins/platforms/line/dashboard/` |
| agent 核准工具（`line_whitelist_tool.py`）| **獨立檔** | `tools/` |
| adapter 執行期行為（gate／`@mention`／婉拒／observed／媒體／quote／名稱解析）| **patch**——無法當獨立檔 | `patches/line-adapter.diff` → `plugins/platforms/line/adapter.py` |
| toolset + Dashboard 探索接線 | **patch** | `patches/hermes-core-wiring.diff` |

獨立檔放在 [`src/`](./src) 下、鏡射它們在 Hermes 內的路徑。adapter 那部分是 diff，因為它改的是 adapter 的 inbound 流程——見 [`docs/adapter-integration.md`](./docs/adapter-integration.md)。參考實作：`sam7894604/hermes-agent` 的 **fork PR #6**（`feat/line-whitelist-management`）。

---

## 功能

- 🟢 **熱加載白名單、免重啟。** store 以 `config.yaml`（`platforms.line.*`）為後端，每則訊息即時經 Hermes `load_config()` 讀取（改檔自動失效）。在 Dashboard 加一個群 → 下一則訊息就受它 gate。
- 🟢 **scope 隔離。** DM／群組／房間是三個獨立白名單。在群組被允許**不代表**能私訊，反之亦然。
- 🟢 **admin 禁刪保護。** 管理員 userId 無法被工具或 Dashboard 從白名單移除（會拋 `WhitelistError`）——不會不小心把自己鎖在外面。
- 🟢 **`@mention` gating。** 白名單群預設 `requires_mention: true`——只有被 `@` 才回應；其餘訊息轉為被動脈絡。
- 🟢 **婉拒 + 去重通知。** `@` 到 bot（或私訊）的未授權來源，會收到一則**限流**的英文「尚未授權，已通知管理員」，且管理員**每來源只被通知一次**（去重），發到你選的管道。
- 🟢 **被動 observed 脈絡。** 白名單群內非 `@` 的訊息以 `observed` 記錄（不觸發 agent），帶 `[name|userId]` 歸屬，讓之後的 `@` turn 有完整脈絡。
- 🟢 **媒體政策 + 保留。** 影片／語音在被動記錄時濾除；照片／檔案記為 placeholder，保留 `retention_days`（預設 3、可 per-source 覆寫）。
- 🟢 **引用回覆感知。** 引用某則舊訊息（`quotedMessageId`）的回覆，會把被引用原文注入當前 turn 當脈絡。
- 🟢 **Dashboard + 核准工具。** 從網頁 Dashboard 管理白名單（CRUD + 通訊紀錄 + 名稱解析），或讓管理員在聊天中用 `line_whitelist` 工具核准。

---

## 待審佇列與一鍵核准（v0.2）

每個未授權來源（@bot 的群、陌生 DM、bot 入群/被加）都會記進 store 的**待審佇列**
——`platforms.line.unauthorized_seen[id]` 擴充 `platform`、`source_type`、解析
`name`、`first_seen`/`last_seen`、`count`、`status`。管理員**免手抄 ID**：

- **Dashboard「待審清單」面板**——列出每個待審來源（name/id · type · count ·
  last seen）+ 一鍵 **加入白名單** / **忽略** 按鈕。REST：`GET /pending`、
  `POST /pending/{id}/approve`、`POST /pending/{id}/ignore`（冪等 200）。忽略後
  不再通知。
- **互動決策卡**（選配，`patches/telegram-discord-cards.diff`）——當未授權通知的
  target 是 **Telegram 或 Discord**，通知改為互動 **✅同意 / ⛔拒絕 / ➖略過** 卡；
  按鈕（admin gated）直接核准/忽略該來源。LINE 及未套 patch 的平台維持純文字
  （自動 fallback）。見 [`docs/adapter-integration.md`](./docs/adapter-integration.md)。
- **名稱解析**——群名走 `getGroupSummary`、user 名走 `getProfile`（TTL 快取、
  fallback raw id），存進待審條目並在 Dashboard 顯示。

## 設定參考（`config.yaml` → `platforms.line`）

```yaml
platforms:
  line:
    whitelist:                       # 白名單本體（scope 隔離）
      users:  ["U..."]               # DM scope
      groups: ["C..."]               # 群組 scope
      rooms:  ["R..."]               # 房間 scope
    admins: ["U..."]                 # 管理員 userId——禁刪保護；
                                     #   也是唯一可核准／移除的人
    requires_mention: true           # 群組預設：只有被 @ 才回
    unauthorized_notify: "telegram:521703862"
                                     # 未授權來源要通知到哪。
                                     #   格式 "<platform>:<chat_id>"，例如
                                     #   "telegram:123"、"discord:456"，或
                                     #   純 LINE id。null => LINE home channel。
    retention_days: 3                # 媒體保留（全域預設）
    allow_all_users: false           # LINE_ALLOW_ALL_USERS 的 config 端對應
    meta:                            # 選配 per-source metadata + 覆寫
      groups:
        "C...": { added_by: "U...", note: "峇里島旅伴", requires_mention: true, retention_days: 7 }
      users:
        "U...": { added_by: "U...", note: "..." }
    media:                           # 媒體型別政策
      keep_types: ["image", "file"]  # 記錄（受 retention_days）
      drop_types: ["video", "audio"] # 直接濾除、根本不下載
    observe_unmentioned: true        # 被動 observed 脈絡記錄 開/關
    unauthorized_seen: {}            # 執行期去重／限流狀態（自動寫入）
```

**啟用 admin 核准工具**（自成 toolset、預設關）——把它加進 LINE 平台的 toolset 清單：

```yaml
platform_toolsets:
  line:
    - ...            # 你現有的 LINE toolsets
    - line_whitelist # <-- 加這個；需重啟 gateway 才生效
```

**環境變數（總開關 + 向後相容 overlay）：**

```
LINE_ALLOW_ALL_USERS=false          # gating 必要（見上方警告）
LINE_ALLOWED_USERS=U...,U...        # 選配 env overlay（保留可用）
LINE_ALLOWED_GROUPS=C...            # 選配 env overlay
```

---

## 安裝

1. **複製獨立檔**到你的 Hermes 樹（**不要**覆蓋 adapter 自己的 `__init__.py`）：

   ```bash
   H=<你的-hermes-樹>
   cp src/plugins/platforms/line/whitelist_store.py   "$H/plugins/platforms/line/"
   cp src/plugins/platforms/line/whitelist_notify.py  "$H/plugins/platforms/line/"
   cp -r src/plugins/platforms/line/dashboard         "$H/plugins/platforms/line/"
   cp src/tools/line_whitelist_tool.py                "$H/tools/"
   ```

2. **套用 adapter + 接線 patch**（見 [`docs/adapter-integration.md`](./docs/adapter-integration.md)）：

   ```bash
   cd "$H"
   git apply /path/to/patches/line-adapter.diff
   git apply /path/to/patches/hermes-core-wiring.diff
   ```

   若你的安裝是 *pip/site-packages* build（非 editable），把檔案複製進 venv 的 `site-packages` 並在那裡套 adapter 改動，或重建套件。對 `toolsets.py`／`tools_config.py`／`web_server.py`／`delegate_tool.py` 的加法改動都錨定在既有的 `cronjob` 條目上。

3. **設定** `config.yaml` + env（見上方參考）。設 `LINE_ALLOW_ALL_USERS=false`、你的 `admins`、`unauthorized_notify`、以及 `whitelist.groups` / `whitelist.users`。把 `line_whitelist` 加進 `platform_toolsets.line`。

4. **生效。** 白名單 gate、admin、通知、`requires_mention` 皆**熱加載**（免重啟）。`line_whitelist` toolset 與任何 env 變更（`LINE_ALLOW_ALL_USERS`）需**重啟 gateway**。

---

## 用法

### Dashboard
Dashboard 插件加一個 **LINE Whitelist** 分頁（由 `plugins/platforms/line/dashboard/manifest.json` 自動探索）。你可以：
- 列出／新增／移除白名單條目（分 scope；admin 禁刪以 4xx 強制）。
- 檢視通訊紀錄（複用既有 session store；篩到 `line:*` sessions）。
- 解析顯示名稱（LINE profile／group-summary API、TTL 快取、拿不到 fallback raw ID）。

### admin 核准工具（聊天中）
當 `line_whitelist` 在 `platform_toolsets.line` 且 gateway 已重啟後，**管理員**（`admins` 內的 userId）可在與 bot 的 LINE 聊天中：
- `line_whitelist(action="approve", scope="group", id="C...")` — 加一個群。
- `line_whitelist(action="list")` — 列出目前條目。
- `line_whitelist(action="remove", scope="group", id="C...")` — 移除（admin id 會被擋）。

非管理員會被拒絕；另提供非管理員 `propose` → 管理員 `approve_pending` 的暫存流程。此工具**被排除於子代理**（不能自我核准）。

### 各來源會看到什麼（gating 開啟時）
- **白名單群：** bot 平常安靜、被動記錄大家發言；只有你 `@` 它才回應；影片濾除、照片／檔案保留 `retention_days`；引用某則再 `@`，agent 會看到被引用原文。
- **非白名單群：** 只有被 `@` 時才回一則英文「尚未授權」+ 通知管理員（每來源一次）；否則靜默、不記錄。
- **admin DM／白名單 DM：** 正常 1:1 對話，不需 `@`。
- **陌生 DM：** 英文「尚未授權」+ 通知管理員（一次）、記錄該次嘗試供 Dashboard 檢視。

---

## Rollback（可還原）

每項變更都可逆：

- **設定／白名單：** 還原你的 `config.yaml` 備份（或移除 `platforms.line.whitelist` / `admins` / `line_whitelist` toolset 條目）。
- **總開關：** 把 `.env` 的 `LINE_ALLOW_ALL_USERS` 改回原值。
- **adapter patch：** `git apply -R patches/line-adapter.diff`（以及 `-R patches/hermes-core-wiring.diff`），或從備份還原 adapter 檔。
- **獨立檔：** 刪掉 `whitelist_store.py`、`whitelist_notify.py`、`dashboard/`、`tools/line_whitelist_tool.py`。

env／toolset／adapter 變更需重啟 gateway 才生效。

---

## 測試

```bash
python -m pytest tests/ -q      # 或：python run_tests.py（逐檔隔離）
```

測試 harness（[`tests/conftest.py`](./tests/conftest.py)）會自動 stub Hermes host 模組，故在乾淨 checkout 就能跑（不需裝 Hermes——只要 `pytest`、`fastapi`、`httpx`）。在真實 Hermes 樹內，真模組優先，故這套測試同時也是整合 smoke check。

**59 個測試**：WhitelistStore（scope 隔離、admin 禁刪、去重／限流、保留、熱加載）· 通知路由（含跨平台 `telegram:` target）· 核准工具（admin gating、暫存）· Dashboard 插件（CRUD、紀錄、名稱解析）。

---

## 限制與備註

- **不是即插即用的單一插件。** 執行期行為是 adapter patch；請把本 repo 當「bundle + 安裝指南」，非 `hermes plugins install` 一行搞定。
- **observed 群脈絡**依賴群組共享 session——請以 `group_sessions_per_user=false` 跑 LINE 平台（如同 Telegram observed 機制的假設），白名單群的被動脈絡才會載入被 addressed 的那個 turn。
- **媒體保留**目前把照片／檔案記為 metadata placeholder；二進位保留 + 清理 cron 為後續。
- **quote 反查**若被引用原文已過保留期消失，會優雅降級。

---

## 授權

[MIT](./LICENSE) © 2026 sam7894604
