---
name: project-to-agent-plus
description: |
  Transform any existing software project (Python script, CLI tool, automation, web app, library, etc.) into a self-evolving conversational agent driven entirely through Telegram. Wraps existing functions as multi-provider LLM tools (Gemini / Groq / OpenAI / Anthropic / Ollama), PLUS two zero-API-key subscription brains — Claude Code CLI (Pro/Max) and OpenAI codex CLI (ChatGPT plan) driven headlessly with the project's tools exposed over MCP, sets up a Telegram bot front-end (no local UI, no REPL — phone-first), requests folder permissions, optionally adds a shell-bash tool with a host-vs-sandbox choice (sandbox = WSL2 + Docker Engine installed via custom .bat, NOT Docker Desktop, to avoid commercial licensing), Tavily web search, and a self-evolution loop where the agent drafts new tools and the user approves on TG. Make sure to use this skill whenever the user mentions: turning a project into an agent, "agentify", building a Telegram bot for an existing tool, exposing a Python script as a chat-driven assistant, self-improving / self-evolving agent, adding LLM control to an existing app, packaging functions as tools for an LLM, or making any tool remote-controllable through chat. Trigger even when the user only says "make my X chat-controllable" or "turn this into a bot" — those are calls for this skill.
---

# project-to-agent

把現有的專案包成一個**只透過 Telegram** 操作的對話式 agent — 可以呼叫專案原本的函數、寫 code、搜尋網路、隨對話自我擴增新工具。

## 這個 skill 跑完使用者拿到什麼

- **Agent core**(orchestrator + tool registry)— 跟 LLM 對話、呼叫工具、處理 tool result
- **既有專案函數包成 LLM tools** — 不從零做、wrap 既有的
- **多 LLM provider 支援** — Gemini(預設、免費)/ Groq / OpenAI / Anthropic / Ollama,settings 切換不改 code
- **訂閱制 CLI 大腦(Phase 3b,opt-in)** — Claude Code(Pro/Max)/ OpenAI codex(ChatGPT 訂閱)當大腦,**免 API Key**;工具經 MCP server 曝露給 CLI,session 精準續聊、陪跑等待+樹殺、交付契約雙保險全套實戰方法。**沒選就完全走原有 API-key 邏輯,零影響**
- **Two-step write 協議** — 任何「寫」操作預覽 → 確認 → 寫,防 LLM 幻覺
- **Server-side hallucination 偵測** — LLM 宣稱「已套用」但實際沒寫會自動偵測 + 警告
- **Telegram bot adapter** — 唯一前端,phone-first;per-chat session 隔離、4000 字 chunking、polling lock 防多實例、tool progress callback 推送 AI 在做什麼
- **Channel-specific system prompt** — 通道專屬規範(底稿用 marker 區隔、未來擴展用)+ 動態注入(今日日期、工具清單)
- **Folder permission boundaries** — agent 只動使用者明確授權的資料夾(Phase 7 強制 AskUserQuestion 拿實際路徑、不可跳)
- **Shell tool(opt-in)** + **host vs sandbox 二選一**
  - **Host 模式**:agent 直接在使用者 OS 跑 shell、設定快但隔離弱
  - **Sandbox 模式**:WSL2 + Docker Engine(透過附帶的 `setup_sandbox.bat` 自動裝、**不用 Docker Desktop** — 避開商業授權)、寫操作鎖在容器
- **基礎工具集(Phase 10b)** — 沙盒就緒後 AskUserQuestion 問是否加入 read_file / write_file / edit_file / glob_paths / grep_files / view_image / ask_user / done / run_python。預設全給(都是 agent fundamentals)
- **Multi-modal vision** — Gemma 4 / GPT-4o / Claude 3+ 都自動支援。TG 用戶傳圖 → adapter 下載 → orchestrator 注入到下次 chat 的 user content
- **Tavily web search(opt-in)** + 1h TTL cache + per-user rate limit
- **Self-evolution loop** — LLM 缺工具時自動草稿、TG inline button 確認、merge 後 hot-reload(不重啟)
- **Context scaling watch(Phase 13)** — 跑完 skill 評估一次工具數、給對應對策建議(trim / caching / grouping / embedding routing)、不到門檻就設「未來提醒」、agent 之後工具長大會自動 ping 使用者
- **Memory system(Phase 14,opt-in)** — 4 層記憶架構(working / semantic / episodic / procedural),sqlite + numpy,跨 session 記住使用者偏好 + 對話摘要可語意檢索。SENSITIVE_PATTERNS deny-list 自動擋 API key / 密碼 / 私鑰

## 核心理念

1. **既有專案是種子。** 第一天就讓 agent 能驅動現有功能、不從零造輪子。
2. **權限永遠 explicit。** 寫入資料夾、shell access、self-modify 三個都是 opt-in。
3. **Self-evolution = 加 tool。** 新能力 = 新工具 = 一個小函數。LLM 草稿、使用者在 TG approve 才生效。
4. **Sandbox 不等於 Docker Desktop。** Skill 附自家 `.bat` + `.sh` 用 curl 從 docker.com 直接裝 Docker Engine,不踩 Docker Desktop 商業授權地雷。
5. **TG 是唯一 UI、沒有 REPL。** 預設假設使用者在外面、手機上跟 agent 對話、桌面沒有額外介面。
6. **鑰匙在使用者手上。** API key、bot token、Tavily key 全部進 `.env`、gitignored。

## 何時觸發

- 使用者有專案(任何語言、任何大小)說「make this an agent」/「我想跟它聊天」/「轉成 Telegram bot」
- 想遠端遙控跑專案的機器
- 想「自動化」既有工具
- 問怎麼給 LLM 控制現有 app

## ⛔ Phase 0 — 開場儀式(每次跑 skill 必跑、不可省)

這個 skill 帶使用者跑 15 個 phase。**第一回合進入 skill 時**,執行 skill 的 agent 必須做下列 4 件事、依序、不可跳:

### Step 0.1 — 用 TaskCreate 建 15 個 phase 的 TODO 清單

不是隨口列、是**用 TaskCreate 工具實際建任務**,讓使用者在側欄看得到。範例:

```python
TaskCreate(subject="Phase 1 — 分析專案", activeForm="分析專案")
TaskCreate(subject="Phase 2 — 挑工具候選", activeForm="挑工具候選")
TaskCreate(subject="Phase 3 — LLM setup(multi-provider)", activeForm="設定 LLM provider")
...(全 15 個都建)
```

### Step 0.2 — `AskUserQuestion` 對齊 scope **+ channel**

**必須**問 2 個問題、不是 1 個:

```python
AskUserQuestion(questions=[
    {
        "question": "你想做到哪個範圍?",
        "header": "Scope",
        "multiSelect": False,
        "options": [
            {"label": "MVP(Phase 1-8 + 一個 channel)", "description": "分析 → 工具 → LLM → core → two-step → hallucination → permissions → channel(TG 或 Web)。最快可用版"},
            {"label": "MVP + Channel prompt(1-9)", "description": "多加 Phase 9 動態系統提示,30 分鐘"},
            {"label": "Full(15 phase 全跑)", "description": "含沙盒 / 記憶 / self-evolution / context scaling,每個都是 opt-in"},
            {"label": "先 MVP、之後再決定", "description": "channel 跑完先停、看實際效果再決定"},
        ],
    },
    {
        "question": "Agent 跑哪個介面?",
        "header": "Channel",
        "multiSelect": True,
        "options": [
            {"label": "Telegram bot(Phase 8)", "description": "手機隨身、需 @BotFather 拿 token、對話經 TG server"},
            {"label": "Streamlit 本機 web(Phase 8b)", "description": "本機 localhost:8501、0 第三方、資安顧慮場景首選"},
        ],
    },
])
```

依使用者答案調整 TODO:
- **只選 TG** → mark Phase 8b 為 `status=deleted`
- **只選 Web** → mark Phase 8 為 `status=deleted`
- **兩個都選** → 兩個 phase 都跑、TG 跑完接著跑 Web(兩個 process 共用同一個 orchestrator factory)
- Scope = MVP → 把 Phase 9-15 全 mark deleted

### Step 0.3 — 把現況 Brief 給使用者

用一段話總結:
- 「你的專案在 `<path>`、語言 `<x>`、預期跑 `<N>` 個 phase」
- 「每個 phase 結束我會問你 OK 不 OK,沒問完不會自己往下跑」
- 「你隨時可以說『跳過』『改設定』『回上一步』」

### Step 0.4 — `TaskUpdate(Phase 1, status='in_progress')` 開始第一個 phase

不是「我來分析專案了」就開始,**先用 TaskUpdate 把 Phase 1 標 in_progress**,使用者看得到進度。

---

## ⛔ Hard Rules — 每個 phase 都得遵守

| Rule | 違反就是 bug |
|---|---|
| **每進一個 phase**:`TaskUpdate(..., status="in_progress")` | 沒做 → 使用者不知道你在哪 phase |
| **每結束一個 phase**:`TaskUpdate(..., status="completed")` | 沒做 → 任務清單失準、之後重啟對話沒法續 |
| **下一 phase 前**:`AskUserQuestion` 確認結果 OK 或調整 | 沒做 → 「隨口問問」、使用者沒明確 yes 不能往下 |
| **同回合不准連跳 3 個 phase 以上** | 沒做 → 變成「一次全包」、違背逐 phase 設計 |
| **Phase 7 / 10b / 13 / 14 hard ask** | 沒做 → 隱私 / 資安 / cost 失控 |
| **Phase 8 vs 8b 是 channel 選擇,不是 2 個 phase 順著跑** | 沒在 Phase 7.5 確認 channel = 直接跳 Phase 8 寫 TG bot 整合,使用者根本不想要 TG。先 AskUserQuestion 問 channel,Phase 8 跟 8b 至少二選一(或都選) |
| **Per-user / per-chat context 用 ContextVar、不要 inject kwargs 進 tool args** | registry 盲目 `args["_user_id"] = x` 會炸所有沒接該 kwarg 的 tool(`TypeError: unexpected keyword`)。正解見 phase14-memory.md「Per-user 隔離」section。 |
| **每個 tool function signature 必接 `**kwargs`** | 即使選 ContextVar 路線,留 `**kwargs` 當安全網。framework 一旦在某個 phase 改用 inject、所有沒接 kwargs 的 tool 集體 `TypeError`。詳見 phase2-tools.md §kwargs-rule。 |
| **Tool error 必雙寫:short dict 給前端 + full stack trace 給 host stderr** | 使用者在 TG/Web 看到 `{"error": "..."}` 一句話、回 host terminal 什麼都沒有 → 無法 debug。`orchestrator.step()` 內 tool exception 必 `log.exception()`,sandbox shell 失敗必印 stdout/stderr 到 stderr。詳見 phase4-core.md「Host-terminal logging」。 |
| **每輪 LLM call 前必跑 Sequence Sanitizer**(`_strip_orphan_tool_calls`,multi-pass) | Gemini API 對 `user/assistant/tool` 順序極度嚴苛;歷史殘留的 orphan `tool_calls` / 連續 user / consecutive assistant 都會 400。**單 pass 不夠**,刪一筆會產生新 orphan,要 loop 到 stable。詳見 phase4-core.md「Sequence Sanitizer」。 |
| **顯式設 `AGENT_SANDBOX_MODE=sandbox` 但 preflight 失敗 → `raise RuntimeError`,絕不偷偷退回 host** | silent fallback 等於擦除使用者安全意圖,LLM 變成可寫 host filesystem。詳見 phase10-shell.md「Anti-pattern: silent fallback to host」。 |
| **Phase 12 hot-reload 後必呼 `importlib.invalidate_caches()`** | sys.modules cache 不清、新檔案進不來、`reload_tools` 一直拿舊版,使用者誤判「self-evolution 壞了」。詳見 phase12-evolve.md。 |
| **`orchestrator.step()` 每輪重抓 `registry.schemas()`,不准 cache 進 `__init__`** | cache 後 Phase 12 approve 新工具、當前 session 看不到、agent 會回「請重啟才能用」 — self-evolution 直接廢。詳見 phase4-core.md L114 範例。 |
| **`ToolRegistry.run()` 必 wrap 非 dict 回傳成 `{"result": ...}`(Defense 4)** | Google GenAI / OpenAI SDK 底層用 Pydantic 校驗、回 str/int/float 直接 400、且是「下一輪 request 才爆」極難 debug。詳見 phase12-evolve.md Defense 4。 |
| **`propose_tool` 必檢查 `def register(registry)` 缺失就 AST 自動補上(Defense 5)** | LLM 40% 漏寫 register 函數、merge 完工具「隱形」、agent 跟使用者說「做好了」實際沒掛。詳見 phase12-evolve.md Defense 5。 |
| **Tool package 共用 helper(`_check_perm` 等)集中放 `agent/tools/__init__.py` + `__all__` 顯式列出** | 動態工具用 `from agent.tools import _check_perm`、helper 沒匯出就 `ImportError`、tool 跑不起來。詳見 phase12-evolve.md「__init__.py 匯出」。 |
| **Sanitizer 還要處理:leading 非 user 丟掉 + trailing dangling assistant 丟掉** | Gemini 要求 sequence 從 user 開頭、最後一條不能是 assistant 空殼,否則 400 INVALID_ARGUMENT。詳見 phase4-core.md 規則 (d)(e)。 |
| **Phase 10 sandbox:不要重寫 `setup_sandbox.bat`,直接 copy asset** | SKILL 附的版本已修好 3 個坑:`chcp 65001` UTF-8、`wslpath` 路徑翻譯、`sed` 自癒 CRLF。重寫一份簡化版會踩亂碼 / `setup.sh: No such file` / `\r: command not found` 全套。詳見 phase10-shell.md「不要重寫 setup_sandbox.bat」。 |
| **Phase 12 `reload_all()` 必呼 `tools_pkg.register_all(self)`、不能只迭代 submodule** | 只迭代 `iter_modules(tools.__path__)` 會 wipe `register_all()` 內定義的核心工具(read_file / write_file / run_shell / remember_fact / ask_user / done),下一輪 LLM 呼叫變 `unknown tool`。詳見 phase12-evolve.md「Hot reload」範例。 |
| **Phase 8 / 8b 必提供 `/stop` 或 Stop button 中斷機制** | LLM 卡 typing / 工具 hang / 無限 tool loop,使用者只能 host kill process。Per-chat `threading.Event`,`step_stream` for-loop 開頭檢查 `is_set()`、throw `StopRequestedException`、用完 clear。詳見 phase8 §9。 |
| **Phase 11b 排程器必在 `post_init` 內 start、且 `AsyncIOScheduler(timezone=...)` 鎖時區** | (1) 在 `run_polling()` 前 start → 綁到死掉的舊 event loop、reminder 永不觸發。(2) 不鎖時區 → naive datetime 被當 UTC、提醒早/晚數小時。詳見 phase11b-scheduler.md 坑點一二。 |
| **Phase 11b 週期任務的結束條件必走 `end_time`(Trigger.end_date + 重啟標 expired),不准「設提醒叫自己取消」** | 排程器 fire 只發訊息、不喚醒 AI reasoning loop。AI 設個「12 點提醒自己取消」的提醒、12 點只會跳一行字、週期任務照樣無限跑。結束條件要在設定當下用 `end_time_str` 表達完整。詳見 phase11b-scheduler.md 坑點三。 |
| **使用者明確說「跳這個 phase」** | OK,跳;但 TaskUpdate 設 status="deleted" 並 log 原因到下個 phase 的 description |

---

## 15-Phase 工作流

skill 帶使用者跑 15 個 phase。**不要一次全部倒給使用者** — 一 phase 一 phase 來、每個 phase 結束 check 一次。

| # | Phase | Reference |
|---|---|---|
| 1 | **分析專案** — 了解它在做什麼 | `references/phase1-analyze.md` |
| 2 | **挑工具候選** — 找值得 expose 的函數 | `references/phase2-tools.md` |
| 3 | **LLM setup**(multi-provider:Gemini 預設、其他備選)| `references/phase3-llm.md` |
| **3b** | **訂閱制 CLI 大腦(opt-in)** — Claude Code / codex 當大腦、免 API Key;Phase 3 選了訂閱選項才跑,否則跳過 | `references/phase3b-subscription-cli.md` |
| 4 | **Agent core**(planner loop + streaming + per-chat history + token budget)| `references/phase4-core.md` |
| 5 | **Two-step write 協議**(預覽 → 確認 → 寫,防 LLM 幻覺寫入)| `references/phase5-two-step-write.md` |
| 6 | **Server-side hallucination 偵測**(LLM 宣稱已套用但沒寫的自動警告)| `references/phase6-hallucination-detection.md` |
| 7 | **Permission boundaries**(folder ACL)**⛔ 必須 AskUserQuestion 問實際路徑、不可跳** | `references/phase7-permissions.md` |
| **7.5** | **⛔ Channel gate** — 沒在 Phase 0 問過 channel 就在這裡問。Phase 8 跟 8b **互斥或並列**、不是兩個都要跑。**若使用者之後想要 Phase 11b 主動推送提醒、這裡要提醒:只有 TG 推得到、Web-only 只能下次互動補檢查** | (在 SKILL.md Hard Rules 內) |
| 8 | **Telegram bot adapter**(per-chat session、chunking、polling lock、progress callback)| `references/phase8-telegram.md` |
| 8b | **Streamlit web adapter**(本機中控台、無第三方;TG 替代或並列)| `references/phase8b-web-adapter.md` |
| 9 | **Channel-specific system prompt**(通道專屬規範 + 動態注入)| `references/phase9-channel-prompts.md` |
| 10 | **Shell tool(opt)+ Host vs Sandbox 選擇**(sandbox 走 `setup_sandbox.bat`、不用 Docker Desktop)| `references/phase10-shell.md` |
| 10b | **基礎工具擴充**(沙盒就緒後)— read_file / write_file / edit_file / glob_paths / grep_files / view_image / ask_user / done / run_python | `references/phase10b-expand-tools.md` |
| 11 | **Tavily web search(opt)** + cache + rate limit | `references/phase11-tavily.md` |
| 11b | **排程提醒 / 主動推送(opt)** — APScheduler + sqlite 持久化、`set/list/cancel_reminder` 三工具。**只有 push-capable channel(TG)能真推**、Web 退化成「下次互動補檢查」| `references/phase11b-scheduler.md` |
| 12 | **Self-evolution loop**(LLM 草稿新工具、TG approve、evals harness 驗收後 merge)| `references/phase12-evolve.md` |
| 13 | **Context scaling — evaluate-and-suggest**(依當前工具數動態建議:trim / caching / grouping / embedding routing / multi-agent split。**沒到門檻就退場、設 reminder**)| `references/phase13-context-scaling.md` |
| 14 | **Memory system(opt-in)** — 4 層記憶架構(working / semantic / episodic / procedural),sqlite + numpy,7 個工具,跟 Phase 5 / 7 / 13 對齊 | `references/phase14-memory.md` |

## Phase 摘要

### Phase 1 — 分析專案
讀 codebase 了解:語言、entry points、現有 CLI / 函數 / API endpoints、依賴、資料流。產出一段話的摘要、使用者確認後才繼續。**沒對齊認知不要往下走**。

### Phase 2 — 挑工具候選
從分析結果挑 5-15 個函數能變工具。每個工具要:做一件事、明確 I/O、可重複呼叫安全。跳過純 helper、無參數靠 global state、無確認就會破壞東西的。給使用者編輯。

### Phase 3 — LLM setup(multi-provider)
**預設 Gemini**(Google AI Studio 免費 tier、key 從 https://aistudio.google.com/apikey 拿)。但 skill 同時備好 4 個 fallback provider 的 client adapter:Groq(快、免費 tier 大方)、OpenAI、Anthropic、Ollama(本地、無 key)。所有 provider 走同一個 `llm_client.py` factory、用 settings 切換。實作含 streaming + 300s timeout + transient error retry + friendly error 翻譯。

**AskUserQuestion 選 provider 時,選單必須多列兩個訂閱制選項**(使用者已付訂閱、不想再花 API 費的場景):
- **Claude Code CLI(訂閱)** — 使用者已裝 `claude` 並登入 Pro/Max → 免 API Key
- **OpenAI codex CLI(訂閱)** — 使用者已裝 `codex` 並 `codex login`(ChatGPT 方案)→ 免 API Key

選了任一個 → 跑 **Phase 3b**(`references/phase3b-subscription-cli.md`:MCP server 曝露工具 + `cli_brain.py` 驅動 + session 管理 + 全部參數地雷)。**沒選 → Phase 3b 標 deleted,一切照原有 API-key 流程,零改動**。兩條路可並存(settings 切換)。注意:訂閱路線的大腦是 CLI 自帶的 agentic loop,Phase 4 的 orchestrator 只服務 API-key 路線;Phase 6 的 hallucination 偵測在訂閱路線由「交付契約+系統偵測網」替代(Phase 3b 文件 §7)。

### Phase 3b — 訂閱制 CLI 大腦(opt-in)
Claude Code / codex 官方 CLI headless 驅動,工具經 `agent/mcp_server.py`(FastMCP)跨行程共用同一個 ToolRegistry。核心件:明確 session id(claude `--session-id/--resume`、codex `--json` 拿 thread_id 後 `exec resume`)、陪跑等待+樹殺(不設硬逾時、孤兒行程教訓)、codex 提示結構(首輪完整/續聊精簡三行+使用者訊息分隔,防「回應守則本身」)、交付契約+系統偵測網。資產:`assets/cli_brain.py`(drop-in 驅動器)+ `assets/mcp_server.py`(工具曝露範本)。**不含 Gemini 訂閱(Antigravity)——headless 不可行,已驗證,別浪費時間**。

### Phase 4 — Agent core
建 `agent/` 含:`tool_registry.py`、`llm_client.py`、`orchestrator.py`、`tools.py`。Planner loop(max 5-10 iters)+ **per-chat history**(`dict[chat_id, list]`、不是全域單 list)+ token budget(history cap + single tool result 16KB cap)+ retry / timeout。**不做 REPL、不做桌面 chat — TG 是唯一前端**。

### Phase 5 — Two-step write 協議 **(NEW)**
任何會「寫到外部世界」的工具(改檔案、發訊息、呼叫遠端 API 改狀態、執行任務)必須支援 `confirm: bool` 參數。LLM 流程:`tool(confirm=False)` 拿到 preview + 副作用摘要 → 文字告訴使用者「我要做 X、確認?」→ 收到 `yes` → `tool(confirm=True)` 真正寫。System prompt 必寫死規範。**不補一定會踩雷**:LLM 直接 confirm=True 寫錯沒 undo。

### Phase 6 — Hallucination 偵測 **(NEW)**
LLM 會口頭說「✅ 已套用 / 已建立 / 已執行」但實際沒呼任何 confirm=True tool call。Orchestrator 在每輪 reply 前 scan content:
1. 含「已套用 / 已寫入 / 已建立 / 已執行」等 claim 詞
2. 且 turn 內所有 tool_calls 沒一個是 confirm=True

→ 在 reply 前綴警告告訴使用者「LLM 口頭說好但實際沒做、請重試」。

### Phase 7 — Permissions **⛔ 不可跳 user ask**
**HARD RULE**:進這個 phase **必須** `AskUserQuestion` 確認實際資料夾路徑。寫 `agent/permissions.json.example` 然後就走是常見的 #1 失敗 — example 永遠不會被 cp 成 real 檔、agent 最終跑起來碰任何檔都 deny。

最少要問三題:
- 哪些資料夾可以 **read**?
- 哪些可以 **write**?
- 哪些可以 **delete**?(預設保守 — 不開)

拿到答案 → 寫**實際的** `agent/permissions.json`(absolute path、gitignored)→ 拿給使用者看一眼 sanity-check。

每個碰 FS 的工具必呼 `permissions.check(path, op)`。Deny-list(`.env` / `*.key` / `*secret*`)在 `permissions.py` 寫死、永遠擋掉、不論 allowlist。詳見 reference。

### Phase 7.5 — Channel gate **(NEW)**
**⛔ 進 Phase 8 之前必跑、即使 Phase 0 已問過也要再 confirm**。

理由:
- Phase 0 問 channel 時、使用者可能還沒走過 Phase 1-7、不知道實際 agent 長什麼樣 → 7 跑完真實感受後可能改主意
- TG 跟 Web 不是兩個都要跑、是**至少選一個**(或兩個都選)
- Phase 0 答 TG、後來想加 Web,Phase 7.5 是合法的「擴充」入口
- 跑錯 channel 等於白做 30 分鐘

腳本:
```python
AskUserQuestion(questions=[{
    "question": "Phase 1-7 都到位了!現在要做哪個對話介面?",
    "header": "Channel",
    "multiSelect": True,
    "options": [
        {"label": "Telegram bot", "description": "Phase 8 — 手機隨身、@BotFather 拿 token、對話經 TG server"},
        {"label": "Streamlit 本機 web", "description": "Phase 8b — localhost、0 第三方、立即可用"},
        {"label": "兩個都做", "description": "兩個 process 跑、共用 agent core、各有 use case"},
        {"label": "暫時不做、stop here", "description": "Phase 7 結束 ship — agent core 可用、但沒前端"},
    ],
}])
```

依答案 TaskUpdate Phase 8 / 8b:
- TG → Phase 8 in_progress、Phase 8b deleted
- Web → Phase 8 deleted、Phase 8b in_progress
- 兩個 → 兩個依序跑(TG 先、Web 後 — 或反之、由使用者決定)
- 都不做 → 兩個都 deleted、直接停在 Phase 7

### Phase 8 — Telegram bot
@BotFather 拿 token、`TELEGRAM_BOT_TOKEN` + `TELEGRAM_AUTHORIZED_USERS`(numeric user ID 白名單)。Adapter 強化:
- **per-chat session 隔離**:`_chat_history: dict[chat_id, list]`(全域單條 → 多 user 互相污染)
- **訊息 chunking**:4000 字切分 + markdown→HTML 自轉(TG 4096 字硬上限)
- **polling lock**:同 token 多實例同時跑會 409、加 lock file + 啟動 delete_webhook 搶占
- **tool progress callback**:每個 tool call 之前送一行繁中進度給 TG(讓使用者知道 AI 在做什麼、別以為當機)
- **檔案 / 圖片自動偵測**:tool result 含 `output_file/path/saved_path` 之類 key 自動 send_document、`.png/.jpg` 自動 send_photo

### Phase 9 — Channel-specific system prompt **(NEW)**
Skill 雖然只做 TG、但 system prompt 用 `<!--TG_ONLY_BEGIN--> ... <!--TG_ONLY_END-->` marker 區隔通道特定段落、為未來擴展 web UI 留路。**動態注入**:今日日期(防 LLM 搜陳舊年份)+ 已註冊的工具清單 + in-flight 子任務摘要(如有)。

### Phase 10 — Shell + Host vs Sandbox 選擇
**Shell tool 是 opt-in、選了才問 host vs sandbox。** 純 read-only agent 不需要、跳過整個 phase。

選了 shell 後問:
- **🏠 Host 模式**:agent shell 直接在使用者 OS 跑、設定快、零依賴、適合 Mac / Linux / 不想裝 docker 的 Windows 使用者
- **🐳 Sandbox 模式**:WSL2 + Docker Engine、寫操作鎖在容器、安全很多

選 sandbox 必做 pre-flight check(WSL 安裝、`docker info` 免 sudo 可用),不過 → **強制退回 host 模式**、不准半套。`setup_sandbox.bat` 自動裝 WSL/Docker(用 curl get.docker.com、**不用 Docker Desktop**)。

Skill 帶來的沙盒 assets:
- `sandbox/setup_sandbox.bat` — Windows 入口
- `sandbox/setup.sh` — WSL 內安裝邏輯
- `sandbox/Dockerfile` — 最小 Python 容器

### Phase 10b — 基礎工具擴充(沙盒就緒後)**(NEW)**
沙盒 build 完 + preflight 通過 → **問使用者要不要給基礎工具集**。Phase 2 wrap 的 10 個專案工具不夠 LLM 用、缺乏 read_file / write_file / glob / grep / view_image / ask_user / done / run_python 這類 agent-fundamentals。

預設推薦「都給」。沙盒已就緒、寫操作有 two-step + permissions 守、run_python 進 container 隔離 — 風險都鎖住了、給 LLM 用反而比每次自己拼 run_shell command 安全。

涉及三層改動(都在 reference 內詳述):
- `agent/file_tools.py` 新檔 — read_file / write_file / edit_file / glob_paths / grep_files / view_image
- `agent/orchestrator.py` — 加 `_pending_attachments` buffer(view_image 用)+ 認 `done` / `ask_user` 提前結束 loop
- `agent/llm_client.py` — 每個 provider 的 `chat(attachments=...)` 都要接 multi-modal image
- `agent/telegram_adapter.py` — 加 PHOTO handler 自動 download + attach
- `agent/shell_tool.py` — sandbox 模式拿掉 allowlist(container 是 boundary)+ 加 `run_python`

### Phase 11 — Tavily web search
Tavily key 從 https://tavily.com,1000/月免費。包成 `web_search` tool。加 1h TTL 結果 cache(query → result)+ per-user rate limit(預設 20 次/24h)。Brave / Serper / DuckDuckGo 列為備選 provider、但 code 沒 wire 進去。

### Phase 11b — 排程提醒 / 主動推送(opt-in)
唯一「agent 不等使用者開口就動作」的能力。APScheduler(記憶體執行)+ sqlite(持久化、重啟可恢復)+ 3 個工具(`set/list/cancel_reminder`)。支援相對(`10m`)/ 絕對 / cron 三種時間。**Channel 依賴**:只有 TG 推得到、Streamlit 關分頁就推不到(退化成下次互動補檢查)。三個必避地雷:(1) 排程器要在 `post_init` 內 start(否則綁死掉的 event loop、永不觸發);(2) `AsyncIOScheduler` 要鎖 `timezone`(否則 naive datetime 時區偏移);(3) 週期任務的結束條件要走 `end_time`(Trigger.end_date + 重啟標 expired)— **不能叫 AI「設提醒提醒自己取消」**,因為排程 fire 只發訊息、不喚醒 reasoning loop。

### Phase 12 — Self-evolution
Shell + permissions 就緒後啟用:LLM 遇到使用者請求但無對應工具時 → 草稿 `agent/tools_proposed/<name>.py` → TG 顯示 diff + 確認 button → approve 後 `mv` 到 `agent/tools/` → `reload_tools()`(`importlib.reload`)hot-reload、不重啟。

新工具產出時跑 `evals/evals.json` 內定義的 smoke test、通過才 merge,fail 退回 proposed 區。

**Hard rule**:`tool_registry.py` / `orchestrator.py` / `permissions.py` 永遠不能自動 merge(只能進 `tools/`)。

### Phase 14 — Memory System(opt-in)**(NEW)**
**不裝記憶的 agent 等於每次見面都自我介紹一遍** — 對快查 / stateless 場景沒差,對「個人助理 / 長期合作」場景就是 crippling gap。

4 層架構:

| 層 | 持久性 | Storage | MVP? |
|---|---|---|:---:|
| **Working memory** | 短期 in-memory | Orchestrator.messages(已有,Phase 4)| 既有 |
| **Semantic memory** | 跨 session | sqlite facts table | ✅ |
| **Episodic memory** | 跨 session + 可語意檢索 | sqlite episodes + numpy embedding | ✅ |
| **Procedural memory** | macro 化 | (跟 Phase 12 self-evolution 重疊、不另做)| ❌ |

7 個工具:`remember_fact` / `recall_fact` / `list_facts` / `forget_fact`(對齊 Phase 5 two-step)+ `recall_episode`(向量檢索)+ `wipe_memory`(destructive、要 double confirm)+ `memory_state`。

Episode 自動產:TG `/reset` 或 idle 30min,background LLM 摘要當下 session 1-3 句 + embed → 存。LLM 不主動呼,使用者無感、但下次能 recall。

**Privacy 守備**(這 phase 是 attack surface):
- SENSITIVE_PATTERNS deny-list(API key / 密碼 / id_rsa)— LLM 想記也擋
- memory.db 進 `.gitignore`、permissions.json write 允許清單
- `memory_state()` snapshot 進**動態 user prefix**(不污染 Phase 13 static cache)

**最重要**:**永遠 AskUserQuestion 才開**。使用者可能不想被「監聽」對話,要尊重。詳見 `references/phase14-memory.md`。

### Phase 13 — Context scaling(evaluate-and-suggest)**(NEW)**
**這個 phase 不是必做** — 是 skill 結尾跑一次評估、依 **provider × 工具數** 雙因素給對應建議:

| Provider | < 25 | 25-50 | 50-100 | 100+ |
|---|---|---|---|---|
| Gemini Free(Gemma / Flash free) | 退場 + reminder | **trim only**(caching ❌)| trim + grouping | + embedding |
| Gemini Paid / Anthropic | 退場 + reminder | trim + caching | + grouping | + embedding |
| OpenAI | 退場 + reminder | **trim only**(caching 自動)| + grouping | + embedding |
| Groq / Ollama | 退場 + reminder | trim(無 caching)| + grouping | + embedding |

不論做或不做、**system prompt 加 provider-aware `_scaling_reminder()` block**、工具增加到下個門檻時 agent 自己會主動提醒使用者。

**最重要**:
- **永遠 AskUserQuestion、不要不問就自動做**
- **不能對 Gemini free / Ollama / Groq / Gemma 推 caching**(API 不支援、會撞牆)
- **OpenAI 不必實作 cache code**(prefix > 1024 token 自動 50% off)

詳見 `references/phase13-context-scaling.md`。

## Order of operations

不必一次跑完 14 phase。**MVP = Phase 1-8**(分析 → 工具 → LLM → core → two-step → hallucination → permissions → TG)。Phase 9-12 是擴展、每個都是清楚的 opt-in moment。

**Phase 13 是 evaluate-and-suggest**:跑完前 12 個就跑一次、依當前工具數動態建議或退場。即使整個 skill 沒跑滿、做完 Phase 1-8 也該跑一下 Phase 13 設好「未來提醒」 — 之後 agent 工具增加會自動提醒使用者回來升級。

每個 phase 完成 **git commit**,rollback 方便。

## What to NOT do

- **⛔⛔⛔ 進 skill 第一回合不開 Phase 0 儀式**。第一動作 = `TaskCreate` 列 15 個 phase。沒有 TODO 清單使用者看不到全貌、會以為你「一次全做」。
- **⛔⛔⛔ 任何 phase 跳過 AskUserQuestion 直接動手**。「我想你應該要 X、所以我做了」= 違規。除非 hard rules 表內列為「使用者明確說跳」,否則一律先問。
- **⛔⛔⛔ 為了 per-user / per-chat context 改 registry inject kwargs(`args["_chat_id"] = x`)**。會炸**所有既有 tool**(`TypeError: unexpected keyword`)、不是少數工具。正解:用 `contextvars.ContextVar`(`agent/user_context.py`)、tool signature 完全不必改。詳見 `phase14-memory.md`「Per-user 隔離」section。實戰回報過的高頻坑、不要重踩。
- **⛔ 不要跳 Phase 7 的 user ask**。寫 `permissions.json.example` 不算數、必須 AskUserQuestion 拿到實際路徑寫進 real `permissions.json`。這是 skill 最常踩的坑。
- **⛔ 不要 Phase 10 沙盒裝完就停在原始 `shell` 工具**。沙盒就緒後跑 Phase 10b、用 AskUserQuestion 問使用者要哪些基礎工具(read_file / write_file / glob / grep / view_image / ask_user / done / run_python)。給 10 個專案 wrap 工具 + 一個 `shell` 等於 agent 大半時間在拼 shell command。
- **⛔ Phase 13 不要不問就自動做 trim/caching/embedding**。即使工具數到了門檻、必須 AskUserQuestion。使用者可能不在乎 cost(自用 / free tier 內、$1/月)、強推浪費他時間。Phase 13 是「**evaluate-and-suggest**」、不是「auto-optimize」。
- **⛔ Phase 14 不要不問就 enable memory**。記憶代表「agent 監聽你的對話、永久存」— 隱私敏感,使用者可能不想。AskUserQuestion + 提供「只記 facts、不記對話摘要」精簡選項。
- **⛔ Phase 14 不要把 memory snapshot 塞 static system prompt**。每次記憶變動都破 Phase 13 cache、白做。塞 user message 動態 prefix。
- **⛔ Phase 14 LLM 想記 API key / 密碼 / 私鑰 → 必擋**。SENSITIVE_PATTERNS deny-list 不可以「以為使用者不會犯這種錯」就省掉。
- 工具不要寫無參數 + 靠 global state 的 — LLM 無法推理
- API key 不要 hardcode、只進 `.env`
- 沒 Phase 7 permissions 不要開 Phase 10 shell — 太危險
- TG 送來的 shell command 在 HOST 模式**不能 auto-execute** — 一律需要 inline button 確認。SANDBOX 模式因為容器隔離、可以自動跑(deny-list 仍然守)。
- 任何碰 FS 的工具不能繞 permissions check
- **不要**預設用 `gemini-2.5-pro` 等付費模型 — Gemma-4-31B 免費且支援 tool calling/vision、使用者要升級再升
- **不要**叫使用者裝 Docker Desktop — skill 附的 `setup_sandbox.bat` 用 docker.com 的 install script 裝 Docker Engine、商業免費
- **不要**做桌面 chat / REPL / web UI — TG 是唯一前端、所有 UX 投資集中在 TG adapter
- **不要**全域共用 `messages` list — 一定要 per-chat 隔離
- 不要試圖 wrap *每個* 函數 — 集中在使用者手動 + 重複做的、其他靠 Phase 12 慢慢長

## Tone

跟使用者**一起做**、不是替使用者做。每個 phase 結束 → **展示成果 → 問對不對 → 才繼續**。

**「展示成果」具體要做的**:
- 跑 smoke test / unit test、回報 pass/fail
- 列出新增 / 改了哪些檔案(path)
- 一兩句講「這個 phase 解決了什麼問題」
- **然後 AskUserQuestion** 給「OK 進下一個 phase / 想調整這個 phase / 跳掉下一個 phase」三選一

**禁止**:「Phase X 完成。下一個是 Phase Y...」沒問就繼續。

可選 phase 的 trade-off 講清楚:
- **Shell**:大威力、folder 沒框好就是大風險
- **Sandbox**:設定 friction(WSL + Docker Engine 一次 5-10 分鐘)、但 LLM 自演化開了**強烈建議**
- **Tavily**:1000/月對 personal use 夠
- **Self-evolution**:讓專案複利成長、但每個新工具都是新 code、使用者要 skim

## See also

- `references/phase*.md` — 各 phase 詳細 how-to(該 phase 才讀)
- `references/phase10b-expand-tools.md` — 沙盒就緒後該給 agent 哪些基礎工具(NEW)
- `references/phase11b-scheduler.md` — 排程提醒 / 主動推送(APScheduler + sqlite、2 個 asyncio/時區地雷)(NEW)
- `references/phase13-context-scaling.md` — 工具一多 token 怎麼省、依工具數動態建議(NEW)
- `references/phase14-memory.md` — 4 層記憶(working / semantic / episodic / procedural)+ sqlite + 7 工具 + privacy 守備(NEW)
- `references/phase3b-subscription-cli.md` — 訂閱制 CLI 大腦(Claude Code / codex,免 API Key)完整接法與參數地雷(NEW)
- `assets/cli_brain.py` — 訂閱 CLI 驅動器範本:session 管理 + 陪跑等待 + 樹殺(NEW)
- `assets/mcp_server.py` — FastMCP 工具曝露範本(跟 API-key 路線共用 ToolRegistry)(NEW)
- `assets/agent_template.py` — Agent core 起手範本(Python)
- `assets/llm_client.py` — Multi-provider LLM factory(`chat(attachments=...)` 支援 multi-modal)
- `assets/telegram_adapter.py` — 強化版 TG bot starter(含 PHOTO handler)
- `assets/permissions.json.example` — 權限檔範本(僅供參考、Phase 7 必須 AskUser 拿真實路徑)
- `assets/.env.example` — 環境變數範本
- `assets/requirements.txt` — Agent 層 Python 依賴
- `assets/sandbox/setup_sandbox.bat` + `setup.sh` + `Dockerfile` — Phase 10 sandbox 安裝(不用 Docker Desktop)
