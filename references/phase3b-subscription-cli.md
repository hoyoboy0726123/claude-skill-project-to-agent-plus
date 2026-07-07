# Phase 3b — 訂閱制 CLI 大腦(Claude Code / codex)

> **這是 Phase 3 的可選分支。** 使用者在 Phase 3 選了「Claude Code(訂閱)」或
> 「OpenAI codex(訂閱)」才讀這份;沒選的話 Phase 3 原有 API-key 流程完全不變。
>
> 效果:agent 的大腦改用使用者**已登入的官方 CLI**(Pro/Max 或 ChatGPT 訂閱額度),
> **不需要任何 API Key**。工具透過 MCP server 曝露給 CLI。
> 本文件所有結論皆經實戰驗證(SRT AV Studio 專案),每條「坑」都真踩過。

## 0. ⛔ 進場門 — 知情同意(選了訂閱選項後、動工前必跑)

使用者在 Phase 3 選了訂閱選項 ≠ 同意所有取捨。**先把差異亮出來、AskUserQuestion
確認,才准進本 phase**;不確認 → 回 Phase 3 原 API-key 流程,零損失。

先用一段話展示(照抄可用):

> 走訂閱大腦(免 API Key)前,這些取捨要先知道:
> - 🧠 推理迴圈在 CLI 家:幻覺偵測(Phase 6)失效 → 改用「交付契約+偵測網」雙保險
> - 📁 檔案自動傳送改「掃回覆文字」:AI 漏報路徑時由偵測網補(偶爾晚一步)
> - 🔒 安全邊界:claude 安全模式最嚴(工具=唯一通道);**codex 有原生 shell,
>   你的資料夾 ACL 管不到它**,風險 ≈ Host 模式 shell
> - 🧪 嚴格沙盒(Docker 鎖寫入)只有 claude 安全模式做得到,codex 做不到
> - 📊 額度:用你的訂閱額度,用罄會直接失敗(會顯示明確錯誤與重置時間)
> - 👥 多人 TG 場景:per-user 記憶隔離要走 env 繼承鏈(單人用不影響)
> - ✅ 好消息:two-step 確認、資料夾權限、排程照舊;self-evolution 反而更簡單

然後:

```python
AskUserQuestion(questions=[{
    "question": "了解以上取捨後,要走訂閱 CLI 路線嗎?",
    "header": "訂閱路線",
    "multiSelect": False,
    "options": [
        {"label": "走 claude(安全模式)", "description": "最嚴安全邊界:工具是唯一通道、ACL 全效。推薦"},
        {"label": "走 codex", "description": "接受其原生 shell 不受 ACL 約束(≈Host 模式風險)"},
        {"label": "兩個都接", "description": "settings 切換;安全性以 codex 的下限計"},
        {"label": "算了,回 API-key 路線", "description": "回 Phase 3 原流程,零損失"},
    ],
}])
```

選「回 API-key」→ Phase 3b 標 deleted、照原 skill 走,不留任何殘骸。

---

## 與 API-key 路線的架構差異(先懂這個再動手)

```
API-key 路線(原 Phase 3/4):
  adapter → orchestrator.step_stream()(自己跑 planner loop)
          → llm_client.chat(tools=…)(function calling)
          → registry.run(tool)                    ← 工具在同一個 process

訂閱 CLI 路線(本 phase):
  adapter → cli_brain.chat(text)(每輪 spawn 一個 CLI 行程)
          → claude -p … / codex exec …(CLI 自己就是 agentic loop)
          → 你的 MCP server(獨立行程,stdio)
          → 同一個 ToolRegistry                   ← 工具跨 process 共用
```

推論(重要):
- **orchestrator 的 planner loop 不用了**——CLI 內建 loop,你只拿最終回覆。
- **Phase 5 two-step write / Phase 7 permissions 照常有效**——它們活在工具函式裡,
  誰呼叫都一樣擋。
- **Phase 6 hallucination 偵測(orchestrator 層)在此路線失效** → 用本文的
  「交付契約 + 系統偵測網」替代(見 §7)。
- Phase 8/8b adapter 只差一行:provider 是訂閱 CLI 時改呼 `cli_brain.chat()`。

## 1. 產出物

| 檔案 | 內容 |
|---|---|
| `agent/mcp_server.py` | FastMCP 入口,把既有 ToolRegistry 的工具全曝露(copy `assets/mcp_server.py` 改) |
| `agent/cli_brain.py` | CLI 驅動器:run_claude / run_codex + session 管理 + 陪跑等待 + 樹殺(copy `assets/cli_brain.py` 改) |
| adapter 改一處 | provider ∈ {claude_cli, codex_cli} 時走 cli_brain |
| `.env` 加 | `AGENT_LLM_PROVIDER=claude_cli`(或 codex_cli) |

## 2. 前置條件(AskUserQuestion 確認)

- **Claude Code**:`npm i -g @anthropic-ai/claude-code`,跑過 `claude` 完成登入(Pro/Max)。
- **codex**:`npm i -g @openai/codex`,跑過 `codex login`(ChatGPT 方案)。
- 至少裝一個;兩個都裝使用者可隨時切換。
- 用 `shutil.which("claude"/"codex")` 偵測,找不到再試 `%APPDATA%/npm/*.cmd`(Windows npm 全域)。

## 3. MCP server(工具跨行程共用)

```python
# agent/mcp_server.py
from mcp.server.fastmcp import FastMCP
from agent.tool_registry import ToolRegistry
from agent import tools as tools_pkg

mcp = FastMCP("myagent")            # ← 名字 = 工具前綴 mcp__myagent__*
reg = ToolRegistry()
tools_pkg.register_all(reg)

for t in reg.all():
    mcp.tool()(t.func)              # FastMCP 讀 inspect.signature 生 schema
mcp.run()
```

**坑**:工具函式若被裝飾器包過,**必須 `@functools.wraps(fn)`**,否則
signature 變 `(**kw)`、所有工具 schema 全滅、兩家 CLI 全部呼叫失敗。
(skill 的 kwargs-rule 仍適用——`**kwargs` 安全網照留。)

**坑**:App 若用 `pythonw.exe` 跑,MCP server 的 command 要指到同目錄
`python.exe`(有 console 的那個)。

## 4. 兩家 CLI 的呼叫方式與參數地雷

### 4.1 Claude Code

```python
cmd = [claude, "-p",
       "--mcp-config", mcp_json,                 # {"mcpServers":{"myagent":{command,args}}}
       "--append-system-prompt-file", sys_txt,   # ★ 長中文 system 必走檔案
       "--session-id", new_uuid,                 # 首輪;續聊改 --resume <uuid>
       "--allowedTools", "mcp__myagent__*",      # ★ 多值參數:逐項 argv,不可逗號串
       "--output-format", "text",
       user_text]                                # ★ 使用者文字放「最後」
```

| 地雷 | 症狀 | 解法 |
|---|---|---|
| `--allowedTools "a,b,c"` | 全部工具被擋(整串當一個工具名) | 逐項傳 |
| headless 用 `--dangerously-skip-permissions`(互動模式沒接受過) | 進「bypass 被拒」狀態,連 allowlist 都失效 | 不用;純 allowlist |
| 長中文直接放 argv | 經 .CMD/cmd.exe 轉手**吃掉後面的旗標** | system 走 `--append-system-prompt-file`;user 文字放最後 |
| `--continue` | 撿 cwd「最近對話」,使用者自己開過 claude 就串錯 | `--session-id`/`--resume`(§5) |

claude 的 system prompt **每輪都送** → 行為守則不會流失。

### 4.2 codex

MCP 設定寫 `~/.codex/config.toml`(不走旗標):

```toml
[mcp_servers.myagent]
command = "C:/.../python.exe"
args = ["C:/.../agent/mcp_server.py"]
tool_timeout_sec = 1800        # ★ 預設只有 60 秒,長工具必被切
startup_timeout_sec = 60
```

```python
cmd = [codex, "exec",
       "--dangerously-bypass-approvals-and-sandbox",   # headless 呼叫 MCP 必須
       "--json",                                       # JSONL 結構化輸出
       # 續聊時插入:"resume", thread_id
       prompt]
```

`--json` 事件解析:
```python
o = json.loads(line)
if o["type"] == "thread.started":  thread_id = o["thread_id"]     # 存起來!
item = o.get("item") or {}
if o["type"] == "item.completed" and item.get("item_type") == "agent_message":
    finals.append(item["text"])
# 一行 JSON 都沒有(已知 --json+MCP 偶發相容問題)→ 整段當純文字 fallback
```

## 5. 對話記憶:明確 session id(per-chat)

- **claude**:首輪自產 UUID → `--session-id`;之後 `--resume <uuid>`。
- **codex**:首輪從 `thread.started` 拿 `thread_id`;之後 `exec resume <id>`。
- **per-chat 存**:`dict[chat_id → session_id]` 持久化(sqlite/json),
  對齊 skill 的 per-chat 隔離原則;`/reset` 清掉該 chat 的 session。
- **⏹ 中止/超時樹殺後必歸零**:被殺 session 停在半途 tool_use,resume 會**卡死**。

## 6. 提示工程:codex 的天性與正確結構(最重要一節)

claude 是對話助手,角色+守則全塞 system 即可。
**codex 是任務執行器**、且**沒有 system prompt 旗標**,三個實戰踩過的坑:

1. 守則放訊息尾 → codex 忘記格式(漏報交付路徑)。
2. 每輪塞完整守則牆(數百字)→ codex **回應守則本身、忽略使用者訊息**
   (症狀:不管說什麼都回「收到。」)。
3. 閒聊/詢問被當「無動作指令」→ 也回「收到」。

收斂後的正確結構:

```
首輪(開新 thread):
  [系統角色設定,請記住並遵守,之後不必覆述]
  <角色描述 + 完整行為守則>            ← 記進 session
  === 使用者訊息(只需回覆這一則)===
  <text>

續聊輪:
  [每輪守則提醒 — 遵守即可,絕對不要對本區塊本身作任何回應]
  1) 純聊天/詢問:具體回答並舉例,禁止只回「收到/了解」,不要呼叫工具
  2) 任務:做到完成;✅ 已完成 + 📄 交付檔案(完整絕對路徑)
  3) (若有非同步工具)拿到 job_id 必須輪詢到 done/failed
  === 使用者訊息(只需回覆這一則)===
  <text>
```

原則:**角色一次性(session 記憶)、守則精簡每輪、使用者訊息明確框出且放最後**。
替代方案:codex 自動讀 cwd 的 `AGENTS.md`,守則也可放那裡。

## 7. 取代 Phase 6:交付契約 + 系統偵測網

CLI 路線拿不到逐輪 tool_calls,orchestrator 式 hallucination 偵測失效。改用雙保險:

1. **提示層**:守則寫死「有產出必附『📄 交付檔案:』+ 完整絕對路徑,漏列視為未完成」。
2. **系統層**:每輪執行前後快照輸出目錄(mtime/檔名差集),回覆沒提到的新檔案,
   由 adapter 自動補一段「📄 交付檔案(系統偵測)」再送給使用者。

## 8. 行程管理:陪跑等待 + 樹殺

**教訓**:`subprocess.run(timeout=…)` 殺的是 .CMD 包裝層,node 子行程變孤兒
繼續跑完——任務成功了但永遠沒人回報。

```python
proc = subprocess.Popen(cmd,
    stdin=subprocess.DEVNULL,     # ★ pythonw/背景服務沒 stdin,不設會整個卡死
    stdout=PIPE, stderr=PIPE, text=True,
    encoding="utf-8", errors="replace",
    cwd=safe_workdir)             # 給 CLI 安全的家(如使用者授權的資料夾)
# 雙 drain 執行緒 + 每秒 poll:
#   - on_tick(elapsed) → adapter 推「⚙ 執行中…已 N 分」(TG progress callback 對齊)
#   - stop_event / 保險絲(如 60 分)才殺,且必須「樹殺」:
#     Windows: taskkill /PID <pid> /T /F
#     POSIX:   os.killpg(os.getpgid(pid), SIGKILL)(Popen 加 start_new_session=True)
```

adapter 的 `/stop`(skill Phase 8 §9 的 Stop 機制)接到 stop_event 即可,
殺完記得把該 chat 的 session id 歸零(§5)。

## 9. 長任務(工具本身要跑很久)

CLI 對單一 MCP 呼叫有逾時(codex 預設 60s;env `MCP_TOOL_TIMEOUT` **不可靠**)。
工具若會跑分鐘級,採 **Call-Now-Fetch-Later**:

```
tool(confirm=True)  → 立即回 {"job_id": …}(背景執行緒/行程去跑)
job_status(job_id, wait_sec=20) → 長輪詢 ≤25s:running / done / failed
```
守則加一條:「拿到 job_id 必須反覆輪詢 job_status 到 done/failed 才結束回覆」。
單機 agent 用執行緒即可;要跨對話存活再升級成常駐 daemon + 獨立行程。

## 10. adapter 接線(唯一要動原 code 的地方)

```python
# telegram_adapter.py / web adapter,原本:
#   for msg in orchestrator.step_stream(): ...
provider = os.environ.get("AGENT_LLM_PROVIDER", "gemini")
if provider in ("claude_cli", "codex_cli"):
    from agent import cli_brain
    reply, session = cli_brain.chat(chat_id, user_text,
                                    on_tick=progress_cb, stop_event=ev)
    send(chat_id, reply)          # 檔案偵測/縮圖照 Phase 8 既有邏輯
else:
    ...原有 orchestrator 流程,一行都不改...
```

## 11. 驗證清單(做完逐項打勾)

- [ ] CLI 能列出並呼叫全部 MCP 工具(schema 正常、無 `kw` 參數)
- [ ] 兩輪記憶:「記住暗號 X」→ 下一輪「暗號是什麼」命中(claude 與 codex 各測)
- [ ] 閒聊測試:「你可以做什麼」得到具體回答,**不是「收到」**
- [ ] two-step write:confirm=False 預覽 → confirm=True 才寫(權限 deny 路徑也測)
- [ ] /stop:樹殺乾淨(無孤兒 node)、下一句自動開新 session 不卡死
- [ ] 交付偵測網:模型漏報的新檔案被自動補上
- [ ] App/bot 重啟:session 從持久層還原,續聊接得上

## 不做的事

- **不接 Gemini 訂閱(Antigravity `agy`)**:`agy -p` 在 stdout 被程式接管時
  完全不輸出(實測),headless 無法整合,不要浪費時間。
- 不要為了串流拆 `--output-format stream-json`(claude)/逐事件轉發(codex)當 MVP——
  先用「整段回覆 + on_tick 心跳」跑通,串流是之後的 UX 加值。
- 不要讓 CLI 的 cwd = 專案目錄(它會把測試檔寫進你的 repo)。

---

## 12. 全 15 phase 相容性對照(跑完整流程時看這張)

| Phase | 訂閱 CLI 路線 | 差異說明 |
|---|:---:|---|
| 1 分析專案 | ✅ 照舊 | 無關大腦 |
| 2 挑工具候選 | ✅ 照舊 | kwargs-rule 照守;MCP 註冊時 `_mcp_safe()` 會剝掉 `**kwargs`(否則漏進 schema 變假參數) |
| 3 LLM setup | 🔀 分支點 | 選訂閱 → 本文件;兩路可並存 settings 切換 |
| 4 Agent core | ⚠️ 半適用 | **ToolRegistry 照用**(MCP server 靠它);orchestrator / llm_client / Sequence Sanitizer / per-chat history / token budget **全部不適用**(CLI 自帶迴圈與 context)。Host-terminal logging 規則仍適用(在工具裡) |
| 5 Two-step write | ✅ 照舊 | 活在工具函式內,誰呼叫都一樣擋 |
| 6 幻覺偵測 | ❌ 不適用 | 需要逐輪 tool_calls 可見度 → 改用「交付契約 + 系統偵測網」(§7) |
| 7 權限 ACL | ⚠️ 適用但有洞 | 你的工具照擋。**但 codex bypass 模式有自己的原生 shell/檔案能力,不受 permissions.json 約束**;claude 安全模式(只 allowlist mcp__*)才是「工具=唯一路徑」。要嚴格 ACL → 用 claude 安全模式;codex 路線的風險評估等同 skill 的「Host 模式 shell」 |
| 7.5 Channel gate | ✅ 照舊 | 無關大腦 |
| 8 / 8b Adapter | ⚠️ 三處改 | (1) **檔案自動偵測失效**:adapter 看不到 tool result 的 `output_file` key → 改掃「最終回覆文字」中的路徑 + 偵測網補漏;(2) tool progress callback → 換成 `on_tick` 心跳(可選:解析 `--json`/stream-json 事件);(3) `/stop` → 樹殺 + **該 chat session 歸零** |
| 9 Channel prompt | ⚠️ 改注入點 | claude:照舊(system 每輪送)。codex:動態內容(日期等)放「首輪完整區」或精簡提醒區,**別再膨脹守則牆**;「工具清單注入」直接刪——MCP 啟動時 CLI 自己會發現工具 |
| 10 Shell + 沙盒 | ⚠️ 重新想 | CLI 原生就有 shell(claude 全能模式 Bash / codex bypass)。要**強制沙盒**只有一條路:自己的 MCP shell 工具(Docker 容器)+ claude 安全模式(不開原生 Bash)。codex 路線做不到嚴格沙盒(原生 shell 關不掉) |
| 10b 基礎工具 | ⚠️ 看模式 | claude 全能模式/codex:原生已有 read/write/glob/grep → MCP 重複版多餘;claude 安全模式:你的 MCP 檔案工具=唯一檔案通道(配 ACL)→ 有價值。`ask_user`/`done` 不適用(CLI 迴圈自己收尾) |
| 11 Tavily | ✅ 大致照舊 | claude 全能模式已有原生 WebSearch(可省);安全模式/codex → MCP web_search 照做 |
| 11b 排程 | ✅ 照舊 | 排程器活在 adapter/host 側;fire 時改呼 `cli_brain.chat()`。注意:每次 fire = 消耗訂閱額度一輪 |
| 12 Self-evolution | ✅ 反而更簡單 | **hot-reload 全套不需要**:CLI 每輪 spawn 全新 MCP server 行程 → 新工具下一輪自動生效(importlib.invalidate_caches / reload_all / schemas 重抓三條 hard rule 全部免除)。approve 流程(TG button)照舊;Defense 4/5(dict wrap、register 補全)照舊 |
| 13 Context scaling | ⚠️ 表要加一行 | 訂閱 CLI:**trim only**——工具描述精簡有效;caching 不可控(claude 內建自動);真正的天花板是**訂閱額度/rate limit**(額度用罄會直接 turn.failed,見 §_codex_parse 錯誤浮現) |
| 14 Memory | ⚠️ 一個大坑 | 記憶工具(remember/recall…)= 普通 MCP 工具,照用。**但 ContextVar per-user 隔離跨不過行程**:工具跑在 CLI spawn 的 MCP server 行程裡,adapter 行程 set 的 ContextVar 拿不到!解法:adapter spawn CLI 前設環境變數(如 `AGENT_CHAT_ID`),CLI → MCP server 子行程會繼承 env,工具改讀 env。單使用者場景可直接忽略。working memory 由 CLI session 取代;episode 自動摘要若也走訂閱 CLI 要注意額度 |

**一句話心法**:凡是「活在工具函式裡」的機制(two-step、ACL、error 格式)全部照舊;
凡是「活在 orchestrator 迴圈裡」的機制(幻覺偵測、sanitizer、history 管理、hot-reload、
ContextVar)都要重新想——因為迴圈搬到 CLI 家了、工具搬到另一個行程了。
