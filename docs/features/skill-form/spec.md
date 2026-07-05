---
summary: 把 graft 從「daemon 工具」重構為「Python 庫 + Claude Code Plugin」，刪除 daemon 進程模型、保留 helper 作者契約。
read_when:
  - 動 daemon.py / loader.py / cli.py 之前
  - 評估是否要在 daemon 周邊加新功能時（默認答案：不要）
  - 規劃 Plugin 分發或 PyPI 發版前
status: draft
---

# Skill-Form：去 daemon 化 + Claude Code Plugin 化

## 問題

graft v0.2 的核心架構是一個本地 HTTP daemon（`graft serve`），helper 代碼透過 `httpx` 呼叫 `127.0.0.1:<port>` 再由 daemon 代發真正的 HTTPS。這個設計在 2025 年 MVP 階段是合理的——daemon 集中管理 auth、retry、circuit、stats，helper 文件保持薄。

但 2026 年的生態變了：

1. **Claude Code Plugin 系統成熟**：`.claude-plugin/plugin.json` + `/plugin install <git-url>` 提供一條命令的安裝體驗，用戶端自動載入 SKILL.md、scripts、hooks。要走這條路，graft 必須是 **import-and-go** 的庫——沒有空間讓用戶在 session 開始前先去跑 `graft serve`。
2. **daemon 是反向摩擦**：當前安裝流程是 `uv tool install graft` → `graft init` → `graft serve` → 開另一個視窗用 Claude Code。三步裡有兩步是 daemon 帶來的。
3. **daemon 三件事都不需要進程隔離**：auth 注入是讀環境變量+toml；retry 是 `httpx.HTTPTransport(retries=2)` 一行；stats 是 filelock 寫 JSONL。沒有任何一件需要常駐進程。
4. **跨進程狀態的代價超過收益**：daemon 唯一真實提供的「跨進程共享」價值是熔斷計數，但實踐中 graft 是單人 per-project 工具，並發 agent session 共享熔斷的場景幾乎不存在。

決策已記錄於 [memory/graft_direction_dedaemonize_skill.md](~/.claude/projects/-Users-maine-dev-graft/memory/graft_direction_dedaemonize_skill.md)。OpenClaw skill 路線已調研並否決，本 spec 是 Claude Code Plugin 方向的具體執行計劃。

## User Story

- **作為**用 Claude Code 的開發者，**我想要**用 `/plugin install github:mymaine/graft` 一條命令把 graft 加到 session，**這樣**不需要記 `graft serve`、不佔一個終端視窗。
- **作為** helper 作者（人或 agent），**我想要**寫的 `from graft.context import request` 直接 in-process 執行真正的 HTTP，**這樣**沒有 daemon 死掉、port 衝突、liveness 檢查這些跟業務無關的故障模式。
- **作為** graft 維護者，**我想要**砍掉 250+ 行 daemon 相關代碼，**這樣**更貼合「LOC 是 feature」的紀律，模組數從 11 降到 8 左右。

## Acceptance Criteria

- [ ] **AC-1（庫直接可用）**：在乾淨環境執行 `pip install -e .`（或 `uv pip install -e .`），新開 Python REPL 跑 `from graft.context import request; r = request("github", "GET", "https://api.github.com/zen")`，**無需啟動任何後台進程**，能拿到 200 + body。`GRAFT_GITHUB_TOKEN` 不存在時返回未注入 auth 的真實響應；存在時 header 含 `Authorization: Bearer ...`。
- [ ] **AC-2（helper 契約零變更）**：Phase 1 之前寫的 `helpers/<service>.py` 文件（依賴 `from graft.context import request` 與 `graft.context.auth`），不修改任何一行，在新版本下功能等價。`tests/` 中所有現存使用 helper API 的測試保持綠色。
- [ ] **AC-3（Plugin 一鍵安裝）**：repo 根目錄存在 `.claude-plugin/plugin.json`，描述 graft 為 Claude Code plugin。在已配置 marketplace 或直接 `/plugin install <git-url>` 後，新開的 Claude Code session 能自動讀到 graft 的 SKILL.md、agent 不需要任何額外配置就能寫 helpers 並調用 `from graft.context import request`。
- [ ] **AC-4（daemon 全部清除）**：`src/graft/daemon.py` 已刪除；`graft serve` CLI 子命令已移除；`.graft/daemon.port` 文件不再被任何代碼讀寫；`DaemonNotRunning` exception class 已刪除；`grep -r "daemon" src/` 只留歷史 ADR 中的字串引用，不留可執行代碼路徑。
- [ ] **AC-5（stats 跨進程安全）**：兩個並行 Python 進程同時 import 同一個 helper 並各跑 100 次，`.graft/stats.jsonl` 行數恰好 200、無交錯破損行（透過 `fcntl.flock` 或等價機制實現 append 原子性）。
- [ ] **AC-6（熔斷退化已記錄）**：circuit 狀態從「daemon 進程記憶體共享」退化為「per-process 記憶體 + 不持久化」，行為差異寫入 ADR；`spec.md` AC-8 中對應條款更新。
- [ ] **AC-7（LOC 降低）**：`scc src/graft/ --by-file --no-cocomo` 報告 Code 列數 ≤ **700**（從當前 905 降至少 200 行）；CI 上限從 950 收緊到 700。
- [ ] **AC-8（cold-start E2E 不依賴 daemon）**：`tests/e2e/cold_start.sh` 移除 `graft serve` 啟動步驟，端到端流程仍通過原本 5 個 acceptance gate（helper 寫入 / stats 記錄 / git auto-commit / mypy strict / 退出碼 0）。
- [ ] **AC-9（Plugin install 可用）**：本機執行 `claude plugin install <local-path>` 或從 git 倉庫 install，新 session 中執行 `/skills` 能看到 graft skill；agent 在新 session 內被指示「呼叫 GitHub API 列 issue」時能找到並使用 graft skill 完成任務。

## 需求範圍

- 把 daemon 的三大職責（auth / retry / stats / circuit）改為 in-process 函數
- 改寫 `loader.py`：`request()` 與 `_track()` 不再走 `httpx.Client(base_url=...)` 到 localhost
- 刪除 `daemon.py`、`graft serve` 子命令、port 文件機制、`DaemonNotRunning`
- 新增 `.claude-plugin/plugin.json` 與配套 SKILL.md 位置調整
- stats 寫入加 file lock 保護跨進程 append
- 更新 README、`docs/spec.md`、AGENTS.md 中所有「daemon」「serve」相關描述
- 寫一份 ADR：`docs/decisions/YYYY-MM-DD-remove-daemon.md`

## 需求邊界外

- **不改 helper 契約**：`graft.context.request` / `graft.context.auth` 公開簽名不動
- **不改 helper validator**：Generalization docstring、forbidden imports 規則保留
- **不改 git_memory 自動 commit**：邏輯保留，可能調用點從 daemon 搬到 loader
- **不改 stats 文件格式**：`stats.jsonl` 一行一 JSON、1024 byte 上限、字段不變
- **不引入新依賴**：`httpx` 留下，但放進 graft 進程而非 daemon；不引入 `filelock` 套件，用 stdlib `fcntl`
- **不做跨進程熔斷恢復**：接受 per-process 退化，未來如有需求再評估
- **不做 plugin marketplace 提交**：本 spec 範圍止於 plugin.json 可被本地或私有 git URL 安裝；提交到 Anthropic 官方 marketplace 是後續決策
- **不做 venv 自動 bootstrap**：第一版要求用戶 `pip install graft`；自動建 venv 進 `${CLAUDE_PLUGIN_DATA}/venv` 是 Phase 5+ 的可選增強，不阻塞主流程

## 設計

### 模塊結構（重構後）

```
src/graft/
├── __init__.py
├── cli.py             # init / sync / stats / hot / inspect / prune / add / reset（無 serve）
├── context.py         # 公開 namespace：request / auth / Response（不變）
├── loader.py          # in-process request、validator、stats 包裝、circuit
├── circuit.py         # per-process 熔斷狀態
├── stats.py           # filelock-protected append + aggregate
├── git_memory.py      # 自動 commit（不變）
├── validator.py       # Generalization 約束（不變）
├── registry.py        # graft add（不變）
├── skill.py           # SKILL.md template 處理（不變）
└── templates/
    └── SKILL.md
```

### 新版 `loader.request()`（核心改動）

```python
def request(service, method, url, *, params=None, headers=None, json=None, timeout=None):
    headers = dict(headers or {})
    if (token := auth(service)) and not any(k.lower() == "authorization" for k in headers):
        headers["Authorization"] = f"Bearer {token}"
    with httpx.Client(transport=httpx.HTTPTransport(retries=2),
                      timeout=timeout or 30.0) as c:
        r = c.request(method, url, headers=headers, params=params, json=json)
    return Response(r.status_code, dict(r.headers), r.content)
```

### `loader.load()` 中的 circuit + stats 路徑

- circuit check：直接調 `Circuit` 物件方法（已存在於 `circuit.py`），不再 POST 到 daemon
- stats append：直接調 `stats.append()`，內部加 `fcntl.flock` 保證跨進程 append 不交錯

### `.claude-plugin/plugin.json` 形態

```json
{
  "name": "graft",
  "version": "0.3.0",
  "description": "Self-editing HTTP API harness for AI agents.",
  "author": { "name": "mymaine" },
  "homepage": "https://github.com/mymaine/graft"
}
```

SKILL.md 從 `templates/SKILL.md` 拷貝到 plugin 標準位置（具體位置待 Phase 4 確認 Claude Code plugin spec 細節）。

### file lock 細節

`stats.append()` 在打開 jsonl 文件後先 `fcntl.flock(fd, LOCK_EX)`，寫完 `LOCK_UN`。Linux 與 macOS 都支援；Windows 不支援但 graft 本來就只跑 Unix（pyproject 已聲明）。

## 測試策略

- **單元測試（多）**：
  - `loader.request()` in-process 路徑 — auth 注入正確、未配置 token 不加 header、retry 觸發、Response 解碼
  - `stats.append()` 跨進程 — 用 `multiprocessing` 起兩個 worker 並發寫，驗證行數準確、無破損
  - `Circuit` 物件 — 連續 fail/ok 計數、reset 行為、第 3/5 次邊界
  - `validator` — Generalization 缺失、forbidden import 命中、合法 helper 通過
- **集成測試（中）**：
  - `loader.load()` 端到端 — 寫一個臨時 helper 文件、加載、調用、驗證 stats 行被寫入、git commit 被觸發
  - 用 `httpx.MockTransport` 替換真實網路，驗證 5xx retry、auth header
  - SKILL.md 由 plugin manifest 暴露後，能被現存 `tests/` 的 SKILL discovery 流程識別
- **E2E 測試（少）**：
  - `tests/e2e/cold_start.sh` 改寫版 — 不啟動 daemon，跑完整 Claude Code 寫 helper → 調用 → stats → commit 流程
  - `claude plugin install <local-path>` 後在新 session 內觸發 graft 使用（手動驗證寫入 `docs/internal/demo.md`）
- **不測的部分**：
  - Claude Code plugin marketplace 的服務端行為（Anthropic 維護，graft 不可控）
  - `httpx` 內部 retry 算法（第三方 SDK 內部）
  - macOS / Linux 之外的平台（範圍外）

## 文件改動矩陣

| 文件 | Phase | 動作 |
| ---- | ----- | ---- |
| `src/graft/loader.py` | 1 | 加 in-process `request()` 路徑 + `_inprocess` flag |
| `src/graft/circuit.py` | 1 | 暴露為 module-level 單例（取代 daemon 內 attr） |
| `src/graft/stats.py` | 1 | `append()` 加 `fcntl.flock` |
| `tests/unit/test_loader_inprocess.py` | 1 | 新增（in-process 路徑覆蓋） |
| `tests/unit/test_stats_concurrent.py` | 1 | 新增（filelock 覆蓋） |
| `src/graft/loader.py` | 2 | 默認切到 in-process；daemon path 變回退 |
| `src/graft/daemon.py` | 3 | **刪除** |
| `src/graft/cli.py` | 3 | 移除 `serve` 子命令、port file 處理 |
| `src/graft/loader.py` | 3 | 移除 `_connect()`、`DaemonNotRunning`、port_file 參數 |
| `tests/e2e/cold_start.sh` | 3 | 移除 `graft serve` 啟動行 |
| `.claude-plugin/plugin.json` | 4 | 新增 |
| `templates/SKILL.md` | 4 | 內容調整為 plugin 形態說明（不再提 `graft serve`） |
| `README.md` | 5 | 改寫 quickstart、刪 daemon 相關段落 |
| `docs/spec.md` | 5 | AC-3/AC-5/AC-8 文字更新；hard constraints LOC 上限 905 → 700 |
| `AGENTS.md` | 5 | LOC budget 行更新 |
| `docs/decisions/<date>-remove-daemon.md` | 5 | 新增 ADR |

## 禁止改動的文件

- `src/graft/validator.py` — 規則不變
- `src/graft/git_memory.py` — 自動 commit 邏輯不變（呼叫點可能搬，函數本身不動）
- `src/graft/skill.py` — SKILL.md 處理不變
- `src/graft/registry.py` — `graft add` 不變
- `helpers/`（任何用戶倉庫的）— 公開契約不變
- 舊 ADR — 不刪除、不修改，新 ADR 補充上下文

## 清理計劃（Expand-and-Contract）

| 舊實現 | 替換為 | Contract 時機 | 負責 Phase |
| ------ | ------ | ------------- | ---------- |
| `daemon.py` 整檔 + `graft serve` | `loader._inprocess_request()` 函數 | 默認路徑切換後、所有測試綠 | Phase 3 |
| `loader._connect()` + `DaemonNotRunning` | 直接函數調用 | daemon.py 刪除同 commit | Phase 3 |
| daemon 路由 `/circuit/check` `/stats` `/request` `/health` `/reload` | 直接 method 調用 + filelock | daemon.py 刪除同 commit | Phase 3 |
| `.graft/daemon.port` 文件 | 不存在 | Phase 3 結束時，運行 graft 不再生成 |
| 舊 `templates/SKILL.md`（含「啟動 daemon」段） | 新 SKILL.md（plugin 形態） | Phase 4 | Phase 4 |

每次 contract 是獨立 commit，message 格式：`refactor: contract <舊實現名> after <條件>`。

## 遷移

無需數據遷移。

`stats.jsonl` 格式不變、`helpers/` 不變、`auth.toml` 格式不變（讀取路徑從 daemon 搬進 loader）。用戶側唯一感知變化：

- 舊：`graft init && graft serve`（兩步、長駐）
- 新：`pip install graft`（一步、無長駐進程）

`graft init` 仍存在，繼續建立 `helpers/`、`.graft/`、SKILL.md，但 `.graft/daemon.port` 不再生成。

## 風險

| 風險 | 等級 | 緩解 |
| ---- | ---- | ---- |
| stats 跨進程寫破損 | 中 | `fcntl.flock` + 並發測試覆蓋 |
| Plugin manifest 規範細節變動 | 中 | Phase 4 開始前再確認一次 Claude Code plugin docs；spec 設計時對 plugin.json 結構保持最少欄位 |
| 用戶習慣 `graft serve`，升級時驚訝 | 低 | README 改寫 + CHANGELOG 顯著標註；`graft serve` 第一版可保留為 stub 印出「已棄用，本版無需啟動」訊息（Phase 3 移除實質邏輯但保留命令一個小版本）|
| 跨進程熔斷退化導致 flaky API 過度重試 | 低 | 接受退化，AC-6 將其文檔化；如實際出問題再加 file-based circuit |
| LOC 從 905 降到 700 不夠激進，反而暗示 daemon 模塊小 | 低 | 700 是保守目標，實際刪完估計 ≤ 650；CI 跟著實際結果再緊一檔 |

---

## Roadmap

### Phase 1 — Expand：in-process 路徑與 daemon 並存

> 目標：新 in-process 實現就位、有測試覆蓋；daemon 路徑保持默認、零回歸

| # | Task | 狀態 | 備註 |
|---|------|------|------|
| 1 | `loader.py` 新增 `_inprocess_request()` + `GRAFT_INPROCESS` 環境變量切換 | [ ] | 不動默認路徑 |
| 2 | `circuit.py` 改為 module-level 單例可被 loader 直接呼叫 | [ ] | daemon 仍可使用同一物件 |
| 3 | `stats.append()` 加 `fcntl.flock` | [ ] | macOS + Linux 雙跑 |
| 4 | 新增 `tests/unit/test_loader_inprocess.py` 覆蓋 in-process 路徑 | [ ] | auth 注入、retry、Response 解碼 |
| 5 | 新增 `tests/unit/test_stats_concurrent.py` 用 multiprocessing 驗證 filelock | [ ] | 200 並發行驗收 |
| 6 | 跑現有完整測試套件，確保 daemon 路徑零回歸 | [ ] |  |

### Phase 2 — 切換默認路徑

> 目標：默認走 in-process；daemon 變成可選回退（為 Phase 3 刪除做準備）

| # | Task | 狀態 | 備註 |
|---|------|------|------|
| 1 | `loader.request()` 默認 in-process；`GRAFT_DAEMON=1` 才走 daemon | [ ] | 預設翻轉 |
| 2 | 跑完整測試 + e2e cold_start，確保默認路徑全綠 | [ ] |  |
| 3 | CHANGELOG 標註「v0.3 起無需 graft serve」 | [ ] |  |

### Phase 3 — Contract：刪除 daemon

> 目標：daemon.py 與所有相關邏輯/CLI/文件全清除

| # | Task | 狀態 | 備註 |
|---|------|------|------|
| 1 | 刪除 `src/graft/daemon.py` | [ ] | 獨立 commit |
| 2 | 從 `cli.py` 移除 `serve` 子命令；保留命令名印「已棄用」訊息一個小版本 | [ ] |  |
| 3 | 從 `loader.py` 移除 `_connect()` / `DaemonNotRunning` / `PORT_FILE` / `port_file` 參數 | [ ] |  |
| 4 | 刪除 `.graft/daemon.port` 寫入點與所有讀取點 | [ ] |  |
| 5 | `tests/e2e/cold_start.sh` 移除 `graft serve` 啟動步驟 | [ ] | 同步驗證 5 個 gate 仍 PASS |
| 6 | `scc src/graft/` 確認 ≤ 700；更新 CI LOC 上限 | [ ] |  |

### Phase 4 — Plugin 化

> 目標：`/plugin install <git-url>` 一鍵安裝可用

| # | Task | 狀態 | 備註 |
|---|------|------|------|
| 1 | 新增 `.claude-plugin/plugin.json` minimum 欄位 | [ ] |  |
| 2 | 確認 SKILL.md 在 plugin 中正確位置；必要時調整 `graft init` 行為 | [ ] | 參考 Claude Code plugin 文檔 |
| 3 | 本地 `claude plugin install <path>` 驗證能在新 session 載入 | [ ] | 手動 + 寫入 `docs/internal/demo.md` |
| 4 | （可選）研究 `${CLAUDE_PLUGIN_DATA}/venv` bootstrap，若 < 50 行可實現則做，否則延後 | [ ] | 不阻塞 Phase 5 |

### Phase 5 — 文檔與發版

> 目標：對外資訊一致、PyPI v0.3 發布

| # | Task | 狀態 | 備註 |
|---|------|------|------|
| 1 | README 改寫 quickstart、刪 daemon 段落、加 plugin 安裝段 | [ ] |  |
| 2 | `docs/spec.md` AC-3/AC-5/AC-8 文字 + LOC 上限更新 | [ ] | spec.md 仍 < 500 行 |
| 3 | `AGENTS.md` LOC budget 行 905 → 700 | [ ] |  |
| 4 | 新 ADR `docs/decisions/<date>-remove-daemon.md` | [ ] |  |
| 5 | bump version 0.3.0；發 PyPI；發 GitHub release | [ ] | 等用戶批准 |
