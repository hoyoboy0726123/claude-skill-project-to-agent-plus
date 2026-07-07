# Phase 8b — Streamlit Web Adapter(本機中控台)

> 📌 v3(2026-05):範本經過多輪 production 驗證、加入下列在 v2 之後實戰補上的功能:
>
> | 功能 | 對應改動 |
> |---|---|
> | **多對話 thread 分條 + sqlite 持久化** | `assets/chat_store.py`(v2) |
> | **`st.chat_input(accept_file="multiple")` 原生上傳** | UI 整合、不另開 sidebar uploader(v2) |
> | **真 token-by-token 串流** | `llm_client.chat_stream()` + `orchestrator.step_stream()` + `text_placeholder.markdown(stream+"▌")` |
> | **LLM Provider 切換器** | sidebar radio 列 5 個 provider + ✓/✗ 偵測,看 `agent/__init__.py` 的 `detect_available_providers()` + `rebuild_llm()` |
> | **Model 動態清單** | 每個 client class 加 `list_models()` / `switch_model()` — Ollama 從 `/api/tags`,OpenAI/Gemini/Groq 從 API,Anthropic 硬編 |
> | **Friendly error mapping** | 500 / 503 / 429 / 400 / 401 / timeout 各自繁中提示 + 自動 3 次 retry(`_friendly_error()`)|
> | **History 訊息順序自動修復** | `orchestrator._strip_orphan_tool_calls()` 多 pass 直到 stable,清 orphan tool result / consecutive same role |
> | **Per-iteration assistant persist** | web_adapter 不再「整輪一次 add」,每個 LLM iteration 即時 `store.add_message(assistant, ...)` 確保 DB 內順序對 |
>
> 範本見 `assets/web_adapter_streamlit.py` + `assets/chat_store.py` + `assets/llm_client.py` + `assets/orchestrator.py` —— 四個檔對齊 production。

## 你 agent/__init__.py 需要的 helper

要讓 sidebar provider 切換 work,`agent/__init__.py` 加兩個 function:

```python
def detect_available_providers() -> dict[str, dict]:
    """For sidebar UI — which providers does this user CURRENTLY have set up?
    Returns dict keyed by provider name with {available: bool, reason: str}.
    """
    import os, requests
    out = {}
    out["gemini"] = ({"available": True, "reason": "GEMINI_API_KEY set"}
                      if os.getenv("GEMINI_API_KEY") else
                      {"available": False, "reason": "GEMINI_API_KEY 未設定"})
    out["openai"] = ({"available": True, "reason": "OPENAI_API_KEY set"}
                      if os.getenv("OPENAI_API_KEY") else
                      {"available": False, "reason": "OPENAI_API_KEY 未設定"})
    out["anthropic"] = ({"available": True, "reason": "ANTHROPIC_API_KEY set"}
                        if os.getenv("ANTHROPIC_API_KEY") else
                        {"available": False, "reason": "ANTHROPIC_API_KEY 未設定"})
    out["groq"] = ({"available": True, "reason": "GROQ_API_KEY set"}
                    if os.getenv("GROQ_API_KEY") else
                    {"available": False, "reason": "GROQ_API_KEY 未設定"})
    host = os.getenv("OLLAMA_HOST", "http://localhost:11434")
    try:
        r = requests.get(f"{host.rstrip('/')}/api/tags", timeout=2)
        r.raise_for_status()
        n = len(r.json().get("models", []))
        out["ollama"] = {"available": True,
                          "reason": f"server up at {host}, {n} model(s) pulled"}
    except Exception:
        out["ollama"] = {"available": False,
                          "reason": "找不到 Ollama (請先裝、或設 OLLAMA_HOST)"}
    return out


def rebuild_llm(provider: str):
    """Re-create LLM singleton for a different provider + re-register tools."""
    global _llm, _registry
    from agent.llm_client import make_llm
    from agent.tool_registry import ToolRegistry
    from agent.tools import register_all
    _llm = make_llm(provider=provider)
    _registry = ToolRegistry()
    register_all(_registry)
    return _llm
```

> Phase 8 把 agent 接到 Telegram、雲端對話便利但**對話內容必經第三方 server**。Phase 8b 提供 **本機 Streamlit web 介面** 取代 / 並列、**對話資料 0 packet 出本機**。共用同一個 `orchestrator_factory()`、共用同一套工具、共用同一個 LLM client。

## 何時加 Phase 8b

| 情境 | 推薦 channel |
|---|---|
| 個人手機隨時用、不在乎雲端 | **TG only**(Phase 8) |
| 公司 IT 擋 Telegram / 嚴格資安合規 / 對話內容不可外洩 | **Web only**(Phase 8b)|
| 都想要 | **TG + Web 並列**(兩個 process 跑、共用 agent core)|
| 想 100% air-gapped(連 LLM 都本機) | **Web + Ollama provider**(`.env` 設 `AGENT_LLM_PROVIDER=ollama`) |

對「不能用 TG / LINE / Teams」的場景,Phase 8b 是替代方案、不是擴充。

## 跟 TG adapter 的對稱性

| 功能 | TG adapter | Web adapter | 對稱? |
|---|---|---|---|
| Per-chat 隔離 | `dict[chat_id, Orchestrator]` | `st.session_state["orch"]`(per browser tab)| ✅ |
| 即時進度推送 | 每 tool call 前送繁中行 | `st.status` + progress lines | ✅ |
| 圖片接收 | PHOTO handler 自動下載 + attach | `st.file_uploader`(sidebar) | ✅ |
| 文字串流 | adapter 收完才一次送 | **placeholder.markdown 增量寫**(token-by-token 體感) | ✅ web 更好 |
| Markdown / 表格 | TG 用 HTML 轉換(只支援子集) | st.markdown 原生支援 | ✅ web 更好 |
| 圖片送出 | auto send_photo | `st.image` 內嵌 | ✅ |
| 檔案送出 | auto send_document | `st.download_button` | ✅ |
| 重置 | `/reset` command | sidebar 按鈕 | ✅ |
| **中斷 step loop** | `/stop` command(per-chat) | sidebar `⏹ Stop` 按鈕(`st.session_state["stop_flag"]`) | ✅ — 兩端都必做、見 phase8 §9 |
| 使用者認證 | `TELEGRAM_AUTHORIZED_USERS` 白名單 | **無預設**(localhost-only 假設) | ⚠ |
| 多人 broadcast | TG 原生 chat_id | ❌ Streamlit 1 process 1 user | ❌ |

**有意保留的差異**:
- Web 通道**可以**用按鈕(Streamlit 有原生 button widget),system prompt 透過 `<!--WEB_ONLY-->` marker 解禁「請點按鈕」這類詞
- TG 通道則**禁說**按鈕(無按鈕、會誤導使用者)

## Channel-specific system prompt(Phase 9 marker 範例)

```
<!--TG_ONLY_BEGIN-->
TG 通道:**禁用詞**「按按鈕」「點選下方」(TG 沒按鈕、誤導使用者)
應該說:「請回 yes / 好 / 確認、我就執行」
<!--TG_ONLY_END-->

<!--WEB_ONLY_BEGIN-->
Web 通道:可以說「點側邊欄上傳圖片」「按重置按鈕」等 GUI 詞
使用者操作 streamlit 介面,有 sidebar / file uploader / chat input
圖片透過 sidebar 上傳、不必使用者改字
<!--WEB_ONLY_END-->
```

`build_static_system_prompt(channel="web")` 會保留 `<!--WEB_ONLY-->` 段、strip `<!--TG_ONLY-->` 段(由 Phase 9 的 marker 機制處理)。

## 安裝

```bash
pip install streamlit>=1.30 python-dotenv
# Gemini key 已在 .env 內(GEMINI_API_KEY=...)
```

跑:
```bash
streamlit run agent/web_adapter_streamlit.py
# 瀏覽器自動開 http://localhost:8501
```

## 範本(`assets/web_adapter_streamlit.py`)做的事

| 區塊 | 用途 |
|---|---|
| `_ensure_session()` | 為這個 browser tab 建獨立 orchestrator(`channel="web"`)|
| Sidebar | 重置按鈕、檔案上傳、環境狀態(工具數 / 對話輪數)|
| `_safe_file_render(fp)` | 圖片內嵌、Markdown 預覽、其他用 download_button |
| `_run_orchestrator()` | 跑 `orch.step()` 的 generator,將每個 yield 即時 push 到 placeholder |
| `st.chat_input` | 主入口、接受文字 |
| `st.chat_message` | 渲染對話框、自動帶 user/assistant 圖示 |

**串流文字輸出** 透過 `text_placeholder.markdown("\n\n".join(final_text_parts))` 在 generator 內每收到一個 assistant chunk 就增量寫。Streamlit native 不需要 async / SSE / WebSocket。

## File / Image 上傳設計

兩個原則:
1. **`st.file_uploader` 放 sidebar、不放 chat_input**:Streamlit 1.40+ `st.chat_input(accept_file=True)` 雖然支援、但 UX 上不直覺(發送鍵點下去後檔案才送 / file 預覽小),用 sidebar 更明確「下次發送時帶這幾個檔」。
2. **快取到 `~/.cache/agent-web/uploads/`**:Streamlit rerun 頻繁(每次 widget 互動),不快取的話每次 rerun 都重寫 disk。

附件用 `pending_attachments` list 暫存、發送時 consume + 清空,跟 TG 的「圖片接到 → attach 給下次 chat」一致。

## Markdown / 表格渲染

Streamlit 的 `st.markdown(text)` **原生支援**:
- 完整 Markdown(headers / lists / code blocks / blockquotes / links)
- **GFM tables**(`| col | col |` syntax 直接渲染成表格)
- `unsafe_allow_html=True` 額外讓 `<mark>` / `<small>` / 顏色等 inline HTML 通過

對比 TG 的 HTML parse mode 只支援 `<b>` / `<i>` / `<code>` / `<a>` / `<pre>`、不支援 table — web 在這點明顯勝出。

## Streaming 文字輸出原理

```python
text_placeholder = st.empty()
final_text_parts: list[str] = []

for msg in orch.step():
    if msg["role"] == "assistant" and msg.get("content"):
        final_text_parts.append(msg["content"])
        text_placeholder.markdown("\n\n".join(final_text_parts))  # ← 增量寫
```

`text_placeholder.markdown(...)` 每次都 replace placeholder 內容,使用者體感是文字一段一段冒出來。token-by-token 串流(SSE 級)Streamlit 也有 `write_stream`(LangChain 友善)、但因為我們不用 LangChain,自己 generator + placeholder 寫法更直接。

## 安全注意

**預設 streamlit 跑 `0.0.0.0:8501`**,LAN 內任何人連得到 IP 都能用 agent。**強烈建議**:

```bash
# 只開 localhost
streamlit run agent/web_adapter_streamlit.py --server.address=127.0.0.1

# 或加 .streamlit/secrets.toml + HTTP basic auth
# 詳見 streamlit-authenticator package
```

對外暴露的場景(例如自己家裡的 NAS、想出門也能用)— 套 caddy / nginx 加 reverse proxy + HTTPS + basic auth。**不要直接把 :8501 開到公網**。

## 雙 channel 並列跑(進階)

如果同時想要 TG + Web,跑兩個 process:

```bash
# Terminal 1
python run_bot.py             # Telegram polling

# Terminal 2
streamlit run agent/web_adapter_streamlit.py
```

兩邊各自有 per-chat / per-session orchestrator,共用同一個 LLM client + tool registry。**對話歷史不互通**(Phase 14 memory 補上就會互通 — facts 跨 channel 共用)。

## Checklist

- [ ] Streamlit 已裝(`pip install streamlit>=1.30`)
- [ ] `assets/web_adapter_streamlit.py` 已 cp 到 `agent/web_adapter_streamlit.py`
- [ ] `channel="web"` 在 `orchestrator_factory()` call 內(讓 `<!--WEB_ONLY-->` marker 生效)
- [ ] Phase 9 system prompt 加 `<!--WEB_ONLY-->` 段、描述 GUI 互動(可用按鈕、可看圖、可表格)
- [ ] `--server.address=127.0.0.1` 或 reverse proxy + auth(若不只 localhost 用)
- [ ] 跑 `streamlit run` 試:傳訊息 + 上傳一張圖 + 上傳一份檔案 + 看 markdown / 表格 / streaming 文字都對

## Anti-patterns

- ❌ **直接 `streamlit run --server.address=0.0.0.0` 沒加 auth** — 任何 LAN 內裝置都能控你 agent、carries production tool execution power
- ❌ **每次 widget 互動都重 build orchestrator** — 沒走 session_state 會把 chat history 全清光
- ❌ **不用 `channel="web"`** — orchestrator 拿 TG-only prompt、agent 會跟 web user 講「按按鈕無效」之類矛盾話
- ❌ **試圖讓 Streamlit 支援多 user broadcast** — 模型不對,要多人用就上 FastAPI + WebSocket(超出 Phase 8b 範圍)
