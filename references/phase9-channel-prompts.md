# Phase 9 — Channel-specific system prompt + 動態注入

> 📌 Phase 8b 加入 Streamlit web 通道時,system prompt 加 `<!--WEB_ONLY_BEGIN--> ... <!--WEB_ONLY_END-->` 區段、跟 `<!--TG_ONLY-->` 並列。Web 通道**鼓勵**用 GUI 詞(「請點側邊欄上傳」「按重置按鈕」),跟 TG 通道**禁用**這些詞剛好相反。`build_static_system_prompt(channel="web")` 會自動 keep web / strip tg。詳見 phase8b reference。

> 📌 Phase 10b 啟用基礎工具集後,system prompt 還要加兩段動態注入:
>
> 1. **執行環境**:`run_shell` / `run_python` 跑在 `{host|sandbox}` 模式 + bind-mount 範圍。讓 LLM 知道寫到容器 `/tmp/*` 是真隔離、寫到 bind-mount 內會落到 host
> 2. **工具優先序**:`read_file > run_python > run_shell`(結構化首選、計算靠 Python、系統指令最後),`ask_user` / `done` 互動信號用法
>
> production 範本見 `agent/system_prompt.py` 的 `# 🐳 執行環境` 跟 `# 🧬 新工具` sections。

## 為什麼分通道

這個 skill 只做 TG bot,但 prompt 還是要做通道隔離,因為:

1. **未來擴展**:今天只 TG、明天可能加 web UI / desktop chat。Prompt 一開始就用 marker 切分、之後新通道只要 unset marker 就好、不用重寫整個 prompt
2. **省 token**:TG 沒有 inline 按鈕(對話確認)、web 有覆蓋按鈕、桌面有完整 UI。不同通道**確認流程的指示不一樣**、塞錯通道就是浪費 token 還教 LLM 走錯路
3. **動態 context**:今日日期 / 已掛 skill 清單 / in-flight 任務狀態這類資訊每輪都要新鮮、放底稿就過期

## 結構

```
SYSTEM_PROMPT
├── 底稿(共通規則:寫操作協議、tool list、Hallucination 防線)
│   <!--TG_ONLY_BEGIN-->
│   TG 通道專屬段落:
│     - 確認流程走「使用者打 yes / OK」、不靠按鈕
│     - 講「按按鈕」「點下去」這種詞是違規(TG 純文字、唯一例外是 shell approval 走 inline button)
│     - 訊息長度 < 4000 字節(TG 上限 4096)、超過 orchestrator 會自動分段
│     - 寫工具 confirm=True 跑完 → 用文字告訴使用者結果、不要假設有 UI 反饋
│   <!--TG_ONLY_END-->
│
│   <!--DESKTOP_ONLY_BEGIN-->   ← 未來擴展用、現在沒實作
│   桌面通道專屬段落 ...
│   <!--DESKTOP_ONLY_END-->
│
├── 動態注入區塊
│   ## 📅 今日日期:YYYY-MM-DD(週X)、TZ
│   ## 🔧 工具清單(LLM 看到才知道有什麼可用)
│   ## 📡 In-flight 子任務(60s 內完成的、主動報結果;有派 subagent 才有)
```

## 實作

```python
# orchestrator.py

_SYSTEM_BASE = """\
You are an agent for the user's project, controlled exclusively through Telegram.

## Tools
... (寫操作協議 from Phase 5)
... (Hallucination 防線 from Phase 6)

<!--TG_ONLY_BEGIN-->
## TG 通道規範(這個 agent 跑在 Telegram、沒有任何 GUI / 按鈕)

確認流程:
1. 寫工具 confirm=False 拿到 preview
2. 用文字告訴使用者「我要做 X、確認?」
3. 等使用者回 yes / 好 / 確認 / 套用
4. 收到 → confirm=True 重新呼叫

訊息長度:
- 單則 < 4000 字、超過 orchestrator 會自動分段(別自己分)
- code block 用 ``` 包、長內容(config / 結果 / 報表)> 30 行用摘要替代
- 不能用 inline image(TG 不支援、要傳圖請走 send_photo tool)

不准用的詞:
- 「點覆蓋按鈕」「點下面那個」「按確認」← TG 沒按鈕、會誤導使用者
- 應該說:「請回 yes 或『請套用』、我就會寫」

工具 progress:
- 每個 tool call 之前 adapter 會自動推一行繁中進度(由 _tool_progress callback 處理)
- 你不必再自己描述「我現在呼叫 X tool」
<!--TG_ONLY_END-->
"""

def build_system_prompt(channel: str = "telegram") -> str:
    base = _SYSTEM_BASE
    if channel != "telegram":
        base = re.sub(r"<!--TG_ONLY_BEGIN-->.*?<!--TG_ONLY_END-->\s*", "", base, flags=re.DOTALL)
    if channel != "desktop":
        base = re.sub(r"<!--DESKTOP_ONLY_BEGIN-->.*?<!--DESKTOP_ONLY_END-->\s*", "", base, flags=re.DOTALL)

    parts = [base]

    # 動態注入今日日期
    from datetime import datetime
    from zoneinfo import ZoneInfo
    now = datetime.now(ZoneInfo("Asia/Taipei"))
    weekday = ["週一","週二","週三","週四","週五","週六","週日"][now.weekday()]
    parts.append(
        f"\n## 📅 今日日期\n\n**{now.strftime('%Y-%m-%d')}({weekday})、{now.strftime('%H:%M')}**(Asia/Taipei)\n"
        f"web_search query 含年份請用「{now.year}」、不要用陳舊年份。"
    )

    # 動態注入已註冊工具清單
    if registry.tools:
        parts.append("\n## 🔧 可用工具(LLM 看清楚再呼)\n")
        for name, tool in registry.tools.items():
            parts.append(f"- **{name}**: {tool.description[:80]}")

    # 動態注入 in-flight 子任務(如果你有派 subagent 的設計)
    inflight = get_inflight_subagents()  # 你的實作
    if inflight:
        parts.append("\n## 📡 In-flight 子任務(60s 內完成的主動報)\n")
        for task_id, info in inflight:
            parts.append(f"- {task_id}: {info['summary'][:100]}")

    return "".join(parts)
```

## 動態注入的取捨

| 注入內容 | 該注入 | 為什麼 |
|---|---|---|
| 今日日期 | ✅ 必須 | LLM training cutoff < 今天、不注入 web_search 會打陳舊 query |
| 工具清單 | ✅ 必須 | LangChain bind_tools / Gemini SDK 都會注入 schema 但 LLM 常忘記、底稿明列效果好 |
| in-flight 子任務 | ✅(有派 subagent 才需要)| LLM 主動匯報進度、體驗好 |
| 執行環境 + 路徑映射(`mode=sandbox` 時)| ✅ Phase 10 啟用後必須 | 沒注入 → LLM 在 sandbox 內找 host 路徑、找不到後一直「自我懷疑」走錯岔路。詳見 phase10-shell.md §Step 3。 |
| 使用者過去 N 輪對話 | ❌ | 那是 history 不是 prompt、走 messages chain |
| 領域 specific 狀態(專案自家有的、例如最近執行紀錄、當前選中的物件) | ⚠ 視需求 | 對特定 agent 有用、純工具 wrap 不需要 |

## 跟 LangChain / Gemini SDK 的相容性

LangChain `ChatPromptTemplate` 或直接 `system` message,把 `build_system_prompt(channel)` 結果當 system message 第一條塞進去:

```python
messages = [
    SystemMessage(content=build_system_prompt(channel=req.channel)),
    *history,
    HumanMessage(content=user_input),
]
```

Gemini native SDK 同理、第一個 `Content(role="user")` 帶 system instructions、或用 `system_instruction` 參數(genai 2.x 支援)。

## 檢查清單

- [ ] system prompt 用 `<!--TG_ONLY-->` marker 切分(就算只有 TG 通道也加、為未來鋪路)
- [ ] `build_system_prompt(channel)` 動態組裝、不用全域變數
- [ ] 注入今日日期(必要)
- [ ] 注入工具清單(必要)
- [ ] 注入執行環境 + 路徑映射(Phase 10 啟用後必要、見 phase10-shell.md §Step 3)
- [ ] TG 段落明寫「不准用『點按鈕』『按確認』詞」(常見 LLM 誤用)
- [ ] 跟使用者一起檢查:打開 verbose log、看實際送給 LLM 的 system prompt 結尾長什麼樣、確認動態內容有進去
