# Phase 8 — Telegram bot adapter(強化版)

> 📌 Phase 10b 啟用 `view_image` 後,adapter 還要多一個 **`filters.PHOTO` handler**:使用者傳圖 → adapter 下載到 `~/.cache/agent-tg/photos/` → `orch.add_user(caption, attachments=[{path, mime}])` → orchestrator 的 `_pending_attachments` buffer → 下次 LLM chat 把圖注入 multi-modal Part。Production 版本見 `assets/telegram_adapter.py` 的 `_on_photo()`。

## 目標

把 Phase 4 的 orchestrator factory 接到 TG bot,讓使用者可以在手機上對話 + 收檔案。**這是這個 skill 的唯一前端**,所以 UX 投資都集中在這裡。

## 強化版 5 個重點(跟「基本 TG bot」教學的差別)

1. **per-chat session 隔離** — 全域 `dict[chat_id, Orchestrator]`,不是全域單條 history
2. **polling lock + delete_webhook** — 防 409 Conflict、防本機重啟雙開
3. **4000 字 chunking + markdown→HTML** — TG 4096 字硬上限、超過會被吃掉
4. **tool progress callback** — 每個 tool call 之前推一行進度給使用者
5. **檔案 / 圖片自動偵測** — tool result 含 `output_file/saved_path` 等 key → 自動 send

skill 的 `assets/telegram_adapter.py` 已經實作以上 5 點、直接複製到 `agent/telegram_adapter.py` 就好。下面是各機制的詳細說明。

## 1. 取 bot token + authorized users

```
1. 在 TG 開跟 @BotFather 的對話
2. /newbot → 給 bot 名字 + username → 拿到 token (字串長像 `1234:ABC...`)
3. .env 加:  TELEGRAM_BOT_TOKEN=1234:ABC...
4. 跟 @userinfobot 對話拿你自己的 user ID
5. .env 加:  TELEGRAM_AUTHORIZED_USERS=12345678,87654321  # 逗號分隔
```

`TELEGRAM_AUTHORIZED_USERS` 空 = dev mode、任何人傳訊息都會被處理。**production 一定要設**、否則 bot 被 dox 隨便人都能跑你 tool。

## 2. Per-chat session 隔離

```python
class TelegramAdapter:
    def __init__(self, orchestrator_factory, ...):
        self.orch_factory = orchestrator_factory
        self._orchs: dict[int, Any] = {}   # chat_id → Orchestrator

    def _orch_for(self, chat_id):
        if chat_id not in self._orchs:
            self._orchs[chat_id] = self.orch_factory()
        return self._orchs[chat_id]
```

**為什麼不用全域單條 history**:兩個 user 同時 chat、會看到對方對話 → privacy + UX 雙踩雷。每個 chat 一個 Orchestrator,messages 完全隔離。

`/reset` 命令清掉**這個 chat** 的 orch、其他 chat 不受影響。

## 3. Polling lock(防 409)

TG bot 同個 token 同時跑 2 個 polling client 會被 TG 拒(`409 Conflict`),且其中一個會卡住、另一個也跑不順。常見場景:
- dev 機跑著沒關、prod 機又開一份
- crash restart 後舊 process 還活著
- IDE auto-reload 兩份

`PollingLock` 用 `~/.cache/agent-tg/poll-{token末12碼}.lock` 記 PID,啟動時:
1. 檢查 lock file 在不在
2. 在 → 讀 PID,還活著 → 拒絕啟動
3. PID 死了 → 接手 lock
4. 啟動前先呼叫 `bot.delete_webhook(drop_pending_updates=True)` 強制搶占

```python
class PollingLock:
    def __init__(self, token: str):
        self.path = Path.home() / ".cache" / "agent-tg" / f"poll-{token[-12:]}.lock"

    def acquire(self) -> bool:
        if self.path.exists():
            try:
                pid = int(self.path.read_text().strip())
                if _pid_alive(pid):
                    return False   # active instance, refuse
            except ValueError:
                pass
        self.path.write_text(str(os.getpid()))
        return True
```

## 4. 訊息 chunking + markdown → HTML

TG 一則訊息硬上限 4096 字、超過 reject 整則。`assets/telegram_adapter.py` 提供:

```python
def chunk_text(text: str, max_len: int = 4000) -> list[str]:
    """切到 < max_len、優先在 \\n\\n 邊界。"""

def markdown_to_tg_html(text: str) -> str:
    """LLM 常吐 markdown,TG 不解析 markdown 要轉 HTML:
        ```code blocks```  → <pre><code>...</code></pre>
        `inline`            → <code>...</code>
        **bold**            → <b>...</b>
        *italic*            → <i>...</i>
        [text](url)         → <a href="url">text</a>
    其他 < > & 自動 escape。"""
```

送的時候:

```python
async def _send_long(self, update, text):
    for chunk in chunk_text(text):
        html_text = markdown_to_tg_html(chunk)
        try:
            await update.message.reply_text(
                html_text, parse_mode=tg_constants.ParseMode.HTML,
                disable_web_page_preview=True,
            )
        except Exception:
            await update.message.reply_text(chunk)  # fallback plain
```

## 5. Tool progress callback

LLM 跑 tool 可能要 5-30 秒、使用者不知道在做什麼會以為當機。每個 tool call **之前** 推一行進度:

```
你: 抓我這個月的銷售資料、做成圖
[bot] ▸ get_sales(year=2026, month=5)
[bot] ▸ make_chart(data="...", type="bar")
[bot] [final answer with attached chart.png]
```

實作走 orchestrator yield 的特性:

```python
async def _stream_orchestrator(self, update, orch):
    final_text_buf = []
    files_to_send = []

    async def push(msg):
        if msg["role"] == "assistant" and msg.get("tool_calls"):
            for tc in msg["tool_calls"]:
                name = tc.name if hasattr(tc, "name") else tc.get("name")
                args = tc.args if hasattr(tc, "args") else tc.get("args", {})
                await update.message.reply_text(
                    self._format_tool_progress(name, dict(args or {}))
                )
        elif msg["role"] == "assistant" and msg.get("content"):
            final_text_buf.append(msg["content"])
        elif msg["role"] == "tool":
            files_to_send.extend(self._extract_files(msg["content"]))

    # orch.step() 是 sync generator;包進 thread,每 yield 同步 push 到 TG loop
    def producer():
        for m in orch.step():
            asyncio.run_coroutine_threadsafe(push(m), loop).result(timeout=60)

    await asyncio.to_thread(producer)

    # 全部跑完才送最終文字 + 檔案
    if final_text_buf:
        await self._send_long(update, "\n\n".join(final_text_buf))
    for path in files_to_send:
        await self._send_file(update, path)
```

`_format_tool_progress(name, args)` 可以 override 給你的工具客製、預設是 `▸ tool_name(arg=value, ...)`、長 args 截到 40 字。

## 6. 檔案 / 圖片自動偵測

任何 tool 的 result(JSON dict)如果含這些 key、TG 自動 send:

| key | 動作 |
|---|---|
| `output_file` / `saved_path` / `path` / `file`(字串) | send_document(.png/.jpg 改 send_photo) |
| `output_files` / `files`(list of strings) | 逐個 send |

工具寫法:

```python
@register
def make_chart(data: str, type: str = "bar") -> dict:
    out_path = generate_chart(data, type)  # e.g. /tmp/chart_123.png
    return {"ok": True, "output_file": str(out_path), "summary": "..."}
```

TG adapter 收到 `output_file` → 自動讀檔送圖、不必 LLM 額外呼工具。

## 7. Shell approval inline button(給 Phase 10 用)

```python
def request_approval_sync(self, chat_id: int, prompt: str, timeout: int = 300) -> bool:
    """sync wrapper、給 shell tool 跨 thread 呼叫。
    送 inline keyboard 訊息 [✓ Approve / ✗ Deny]、用 asyncio.Future 等使用者點。"""
```

Phase 10 shell tool 把這個 callable 當 `approval_callback` 傳進去就好。

## 8. Typing keepalive

LLM 跑久了 TG 上面「輸入中…」動畫會消失。背景 task 每 4 秒重發一次:

```python
async def _typing_keepalive(self, chat_id, stop_event):
    while not stop_event.is_set():
        await self.app.bot.send_chat_action(chat_id, "typing")
        try:
            await asyncio.wait_for(stop_event.wait(), timeout=4.0)
        except asyncio.TimeoutError:
            continue
```

LLM 開跑前 set 一個 `stop_event`、跑完 set → 動畫停。

## 9. `/stop` 中斷機制(必做)

**坑位**:LLM 網路超時 / 工具 hang / 無限迴圈 tool call,使用者只能在 host kill process,Bot 永遠卡 typing。

### 設計:per-chat 中斷旗標 + 關鍵步驟檢查

```python
import threading

class TelegramAdapter:
    def __init__(self, ...):
        ...
        # per-chat 中斷旗標 — threading.Event 比 dict 安全(thread-safe set/clear)
        self._stop_signals: dict[int, threading.Event] = {}

    def _get_stop_event(self, chat_id: int) -> threading.Event:
        if chat_id not in self._stop_signals:
            self._stop_signals[chat_id] = threading.Event()
        return self._stop_signals[chat_id]

    async def _on_stop(self, update, ctx):
        """/stop 指令 — 通知當前 chat 的 step loop 退出。"""
        chat_id = update.effective_chat.id
        ev = self._get_stop_event(chat_id)
        ev.set()
        await update.message.reply_text("🔴 規劃已中斷。請發新訊息開啟下一輪。")

    # producer 內檢查
    def producer(orch, chat_id):
        stop_ev = self._get_stop_event(chat_id)
        for msg in orch.step_stream():
            if stop_ev.is_set():
                stop_ev.clear()                   # 用完 clear、下一輪乾淨
                raise StopRequestedException()
            asyncio.run_coroutine_threadsafe(push(msg), loop).result(timeout=60)

class StopRequestedException(Exception):
    """User pressed /stop — exit step loop cleanly."""
```

### 在哪檢查 `is_set()`

不是「每 N 毫秒檢查一次」,是**每個自然 yield point** 都檢查:

| 檢查點 | 為什麼這裡 |
|---|---|
| `orchestrator.step_stream()` for-loop 開頭 | 每輪 LLM call 之前、最自然的退出點 |
| `registry.run(tool)` 之前 | 工具還沒跑、cancel 成本 0 |
| `progress_callback(...)` 內 | producer thread push 訊息前 |
| 長跑 tool 自己的 loop(`run_shell` 看 stdout 行)| tool 自己也 take `stop_event` |

### Web (Streamlit) 端等價設計

Streamlit `st.session_state` 存 stop flag、放在 sidebar 「⏹ Stop」按鈕(`st.button("⏹ Stop")` → 設 flag),`orchestrator.step_stream()` 每輪檢查。同樣的 `StopRequestedException` 模式、UI 換成 sidebar button 而非 `/stop`。

### Anti-patterns

- ❌ **用 `sys.exit()` 跳出** — 整個 bot 死、其他 chat 受影響
- ❌ **kill thread** — Python 沒安全的 thread kill、會洩 fd / lock
- ❌ **全域單一 stop flag** — 多 chat 共用、A 按 stop 把 B 也停掉
- ❌ **set 完不 clear** — 下一輪一開始就觸發 Stop、使用者以為 bot 壞了

## 啟動 bot

```python
# main.py
from dotenv import load_dotenv; load_dotenv()
from agent import orchestrator_factory   # 從 Phase 4 拉
from agent.telegram_adapter import TelegramAdapter

if __name__ == "__main__":
    adapter = TelegramAdapter(orchestrator_factory=orchestrator_factory)
    adapter.run()
```

跑:`python main.py`、跟你自己的 bot 私訊 → 看 progress 推送、收檔案、用 `/reset` 清歷史。

## 命令列表(/help 自動列)

| 命令 | 用途 |
|---|---|
| `/start` | 顯示歡迎訊息 |
| `/reset` | 清掉**這個 chat 的** orchestrator(其他 chat 不受影響) |
| `/stop` | **中斷當前的 step loop**(LLM 卡住 / 工具 hang 時自救);per-chat 隔離 |
| `/help` | 列出已註冊的工具名稱 |

你可以加更多 `@app.add_handler(CommandHandler(...))`、例如 `/status` 列出今日 tool 呼叫次數。

## 常見問題

| 症狀 | 原因 | 解 |
|---|---|---|
| Bot 不回應 / 卡住 | 同 token 跑兩份(本機 + 雲端) | 殺掉一邊、或刪 lock file 重啟 |
| Markdown 亂掉 / 跳 parse error | `**bold**` 沒符對對 / `<` `>` 沒 escape | adapter 已 fallback 純文字,改進 `markdown_to_tg_html` |
| 訊息只送一半 | 超過 4096 字 TG truncate | adapter 自動 chunk、確認 `_send_long` 有被呼叫 |
| Progress 顯示順序亂 | thread + async 沒等待完成 | `producer()` 內 `.result(timeout=60)` 同步等 push 完才下一筆 |
| 圖片沒自動送 | tool result 沒含預定的 key | 檢查工具 return dict 是否有 `output_file` / `output_files` 之類 |

## 檢查清單

- [ ] `_orchs: dict[chat_id, Orchestrator]` 確認 per-chat 隔離(找兩個 user ID 互相聊驗)
- [ ] Polling lock 跑過、刪 lock 重啟 OK、不刪重啟拒絕(`Another agent instance is polling...`)
- [ ] 送一段超過 4000 字的 markdown → 看 chunk + HTML 渲染正常
- [ ] LLM 呼叫多個 tool → 每個 tool 前面都有 `▸` 進度行
- [ ] 寫一個 tool return `{"output_file": "/tmp/test.png"}` → 看 TG 自動送圖
- [ ] 沒設 `TELEGRAM_AUTHORIZED_USERS` 提醒使用者 prod 一定要設、demo 完
