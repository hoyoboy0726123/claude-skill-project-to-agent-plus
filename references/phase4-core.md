# Phase 4 — Agent core

## 目標

把 `llm_client.py`(Phase 3)+ tool 包(Phase 2)組起來,寫 planner loop。**這個 skill 沒有 REPL / 桌面 chat / web UI、TG 是唯一前端**(Phase 8)。Phase 4 結束只跑單元測試、不對外開放。

## 三個檔

```
agent/
├── tool_registry.py   # Tool dataclass + ToolRegistry
├── llm_client.py      # 從 Phase 3、multi-provider
├── orchestrator.py    # planner loop(每個 chat 一個 instance)
└── tools.py           # 從 Phase 2、register_all(registry)
```

## Tool registry

```python
# agent/tool_registry.py
from dataclasses import dataclass
from typing import Any, Callable


@dataclass
class Tool:
    name: str
    description: str
    parameters: dict   # JSON-schema(Phase 2 寫的)
    func: Callable

    def schema(self) -> dict:
        return {"name": self.name, "description": self.description, "parameters": self.parameters}

    def run(self, args: dict) -> Any:
        try:
            return self.func(**(args or {}))
        except TypeError as e:
            # ⚠️ 若這裡常爆 `unexpected keyword argument` — 看 Phase 2 §kwargs-rule:
            # 每個 tool function 應接 **kwargs 吸收 framework 注入的 context
            return {"error": f"bad args: {e}"}
        except Exception as e:
            return {"error": str(e)}


class ToolRegistry:
    def __init__(self):
        self._tools: dict[str, Tool] = {}

    def register(self, tool: Tool):
        self._tools[tool.name] = tool

    def names(self) -> list[str]:
        return list(self._tools.keys())

    def schemas(self) -> list[dict]:
        return [t.schema() for t in self._tools.values()]

    def run(self, name: str, args: dict) -> Any:
        t = self._tools.get(name)
        if t is None:
            return {"error": f"unknown tool: {name}"}
        return t.run(args)

    def reload(self):
        """Phase 12 self-evolution 用 — 重新掃 agent/tools/*.py 並重新註冊。"""
        import importlib, pkgutil
        from agent import tools as tools_pkg
        importlib.reload(tools_pkg)
        self._tools.clear()
        for _, name, _ in pkgutil.iter_modules(tools_pkg.__path__):
            mod = importlib.import_module(f"agent.tools.{name}")
            if hasattr(mod, "register"):
                mod.register(self)
```

## Orchestrator(planner loop)

```python
# agent/orchestrator.py
import json
from typing import Generator


# 設成你專案合理的數字
_CHAT_HISTORY_CAP = 30           # 歷史訊息 cap、防 context 無限長
_TOOL_RESULT_MAX = 16000         # 單一 tool result truncate
_MAX_ITERS = 10                  # 一個 user 訊息最多 N 輪 tool call


class Orchestrator:
    """Stateful planner loop. **每個 chat 一個 instance** — 不要全域共用一個!"""

    def __init__(self, llm, registry, system_prompt: str = "", model: str | None = None):
        self.llm = llm
        self.registry = registry
        self.system_prompt = system_prompt
        self.model = model
        self.messages: list[dict] = []
        if system_prompt:
            self.messages.append({"role": "system", "content": system_prompt})

    # ─── public API ────────────────────────────────────────────
    def add_user(self, text: str):
        self.messages.append({"role": "user", "content": text})
        self._trim_history()

    def step(self) -> Generator[dict, None, None]:
        """Yield each yielded message:
          - assistant tool_calls(沒有 .content)→ Phase 8 adapter 拿來推 progress
          - tool result → adapter 拿來掃 output_file
          - assistant final text → adapter chunked send
        """
        for _ in range(_MAX_ITERS):
            # ⛔ 每輪重抓 schemas — 不要 cache 進 self.tools / __init__。
            # Phase 12 self-evolution `reload_tools()` 後新工具靠這個立刻可用,
            # 不必重啟 session(否則 agent 會卡住跟使用者說「請重啟」、UX 廢)。
            resp = self.llm.chat(
                self.messages, tools=self.registry.schemas(), model=self.model,
            )
            text = resp.text or ""
            tool_calls = resp.function_calls or []

            asst_msg = {"role": "assistant", "content": text, "tool_calls": tool_calls}
            self.messages.append(asst_msg)
            yield asst_msg

            if not tool_calls:
                # Phase 6 hallucination 偵測(若你採用)
                if self._is_hallucinated_write(text):
                    warning = (
                        "⚠️ 我剛剛口頭說已執行、但實際上沒真的呼叫工具(系統自動偵測)。\n"
                        "請再說一次「請執行」、我會重新跑工具真正執行。\n\n"
                        "(原回覆:)\n"
                    )
                    yield {"role": "assistant", "content": warning + text, "tool_calls": []}
                return

            # Run each tool, append truncated result
            for tc in tool_calls:
                result = self.registry.run(
                    tc.name if hasattr(tc, "name") else tc.get("name"),
                    dict(tc.args if hasattr(tc, "args") else tc.get("args", {})),
                )
                result_str = json.dumps(result, default=str, ensure_ascii=False)
                if len(result_str) > _TOOL_RESULT_MAX:
                    result_str = result_str[:_TOOL_RESULT_MAX] + "\n... [truncated]"
                tool_msg = {
                    "role": "tool",
                    "tool_name": tc.name if hasattr(tc, "name") else tc.get("name"),
                    "content": result_str,
                }
                self.messages.append(tool_msg)
                yield tool_msg

        yield {"role": "assistant", "content": f"[max_iters={_MAX_ITERS} hit]", "tool_calls": []}

    # ─── internals ─────────────────────────────────────────────
    def _trim_history(self):
        """Keep system + last (cap) messages; older ones dropped."""
        if len(self.messages) <= _CHAT_HISTORY_CAP + 1:
            return
        # Keep first system + last N
        sys_msg = self.messages[0] if self.messages[0]["role"] == "system" else None
        keep = self.messages[-_CHAT_HISTORY_CAP:]
        self.messages = ([sys_msg] if sys_msg else []) + keep

    def _is_hallucinated_write(self, reply: str) -> bool:
        """Phase 6: claim word + no confirm=True tool call this turn → hallucinated."""
        if not reply:
            return False
        CLAIMS = ("已套用", "已寫入", "已建立", "已執行", "已排程", "已刪除", "套用完成", "完成寫入")
        if not any(c in reply for c in CLAIMS):
            return False
        for m in self.messages:
            for tc in (m.get("tool_calls") or []):
                args = tc.args if hasattr(tc, "args") else tc.get("args", {})
                if (args or {}).get("confirm") is True:
                    return False
        return True
```

**重點**:
- `messages` 是 instance-level、不是 class / global → 每個 chat 一個 `Orchestrator()`、隔離
- `step()` 是 generator → Phase 8 TG adapter 可以邊跑邊推進度給使用者
- `_TOOL_RESULT_MAX` 限制單一 tool result 進 history 的大小、防 token 爆
- `_is_hallucinated_write` 是 Phase 6 的偵測、orchestrator 偵到了直接 yield 警告(也可以放到 adapter、看你哪邊好整合)

## Phase 10b extensions(沙盒就緒後會加的)

Orchestrator 在 Phase 10b 開放基礎工具集後要支援兩件事 — 看 [`phase10b-expand-tools.md`](phase10b-expand-tools.md) 完整講解,這裡只摘要骨架:

```python
class Orchestrator:
    def __init__(self, ...):
        ...
        # Buffer for view_image / TG photo handler — image bytes 不能塞 messages
        # text、要 inject 成 multi-modal Part 到下一次 chat 的最後一個 user message
        self._pending_attachments: list[dict] = []   # [{"path": str, "mime": str}]

    def add_user(self, text: str, attachments: list[dict] | None = None):
        self.messages.append({"role": "user", "content": text})
        if attachments:
            self._pending_attachments.extend(attachments)

    def step(self):
        for _ in range(self.max_iters):
            # 消化 pending attachment、傳給 llm.chat() 注入到 last user Content
            attachments = self._pending_attachments or None
            self._pending_attachments = []
            resp = self.llm.chat(self.messages, tools=..., model=..., attachments=attachments)
            ...
            saw_done = saw_ask_user = False
            for tc in tool_calls:
                result = self.registry.run(tc.name, ...)
                # 工具回傳含 _image_attachment 就 buffer 給下輪 chat
                if isinstance(result, dict) and result.get("_image_attachment"):
                    self._pending_attachments.append(result["_image_attachment"])
                ...
                if tc.name == "done": saw_done = True
                elif tc.name == "ask_user": saw_ask_user = True
            # done / ask_user 提前結束 planner loop
            if saw_done or saw_ask_user:
                return
```

三件事的意義:
1. **`_pending_attachments`** — view_image tool + TG PHOTO handler 共用的緩衝,讓 image 不污染 messages text、又能在下次 chat 自動注入成 multi-modal Part
2. **`done` 提前 return** — LLM 可以明確標誌「任務完成、不要繼續 spam」,省 max_iters 跑空輪
3. **`ask_user` 提前 return** — LLM 問了問題就該停、等使用者下個 message,不要自言自語繼續

## Factory(per-chat Orchestrator)

Phase 8 TG adapter 接受一個 **factory callable** 而不是 single instance,每個 chat 第一次來時 lazy 建一個:

```python
# agent/__init__.py 或 main.py

from agent.llm_client import make_llm
from agent.tool_registry import ToolRegistry
from agent.orchestrator import Orchestrator
from agent.tools import register_all

# Shared 跨 chat:LLM client + tool registry(全域唯一就好、不必每個 chat 各一個)
_llm = make_llm()
_registry = ToolRegistry()
register_all(_registry)


def build_system_prompt(channel: str = "telegram") -> str:
    """從 Phase 9 拉這個進來、含 channel marker + 動態注入"""
    ...


def orchestrator_factory():
    """每個 chat 一個新 Orchestrator,messages 完全獨立。"""
    return Orchestrator(
        llm=_llm,
        registry=_registry,
        system_prompt=build_system_prompt(channel="telegram"),
    )
```

Phase 8 main.py:

```python
from agent.telegram_adapter import TelegramAdapter

if __name__ == "__main__":
    adapter = TelegramAdapter(orchestrator_factory=orchestrator_factory)
    adapter.run()
```

## Sequence Sanitizer — Gemini 對話語法潔癖必過(必做)

**Gemini API 對 messages 序列有極度嚴苛的協議**,違反就 `400 INVALID_ARGUMENT`:

```
正確: user → assistant(text+optional tool_calls) → tool(per tool_call) → assistant → ...
錯誤: tool → assistant(同樣的 tool_calls)              ← 順序反了
錯誤: assistant(tool_calls) → user                      ← tool_calls 後沒接 tool result
錯誤: tool → tool 沒對應 assistant tool_calls           ← orphan tool result
錯誤: user → user 連續                                   ← 失敗 turn 重 add 過
```

**真實踩坑**:用戶重啟 agent、history 載入時最後一條剛好是 `Assistant(tool_calls)`、`tool` 還沒存進 db,使用者送新訊息變成 `... → assistant(tool_calls) → user`,Gemini 400 拒絕、整個對話從這以後都死。

### 必做:`orchestrator._strip_orphan_tool_calls()` — 多 pass 直到 stable

```python
def _strip_orphan_tool_calls(self, messages: list[dict]) -> list[dict]:
    """Brute-force sequence shaper — Gemini won't take ANY of these:
       (a) assistant(tool_calls) but next msg ISN'T tool     → strip tool_calls
       (b) tool msg but prev ISN'T assistant(tool_calls)    → drop tool entirely
       (c) consecutive user / consecutive assistant         → merge content, keep latest
       (d) sequence must START with user                    → drop leading non-user
       (e) sequence to send must END with user/tool         → trailing dangling assistant→drop
    Run multiple passes until messages stop changing (deletions create orphans).
    """
    def _pass(msgs):
        out = []
        for i, m in enumerate(msgs):
            role = m.get("role")
            if role == "assistant" and m.get("tool_calls"):
                next_is_tool = (i + 1 < len(msgs)
                                and msgs[i + 1].get("role") == "tool")
                if next_is_tool:
                    out.append(m)
                else:
                    cleaned = dict(m); cleaned.pop("tool_calls", None)
                    if cleaned.get("content"):
                        out.append(cleaned)
            elif role == "tool":
                prev_ok = (out and out[-1].get("role") == "assistant"
                           and out[-1].get("tool_calls"))
                if prev_ok:
                    out.append(m)
                # else drop orphan tool result
            elif out and out[-1].get("role") == role and role in ("user", "assistant"):
                # (c) merge consecutive same-role — keep latest content but concat
                merged = dict(out[-1])
                a = merged.get("content") or ""
                b = m.get("content") or ""
                merged["content"] = (a + "\n\n" + b).strip() if a and b else (a or b)
                out[-1] = merged
            else:
                out.append(m)
        # (d) drop leading non-user
        while out and out[0].get("role") != "user":
            out.pop(0)
        return out

    cur = list(messages)
    for _ in range(5):
        nxt = _pass(cur)
        if nxt == cur: break
        cur = nxt

    # (e) trailing dangling assistant (no tool_calls, no content meaning) → drop
    # — 但若是 tool / user 結尾就保留(這正是要送 LLM 推下一步的入口)
    while cur and cur[-1].get("role") == "assistant" and not cur[-1].get("tool_calls") \
            and not cur[-1].get("content"):
        cur.pop()

    return cur
```

**設計哲學**:「**與其要求 LLM 乖乖寫歷史、不如送出前把歷史整形成 API 喜歡的樣子**」。Sanitizer 是底層 net、不期待 LLM / framework / 重啟流程永遠完美。

### 在哪呼

**每次 `orchestrator.step()` 開頭、`llm.chat(messages)` 之前**呼一次:

```python
def step(self):
    for _ in range(self.max_iters):
        sanitized = self._strip_orphan_tool_calls(self.messages)   # ★ 每輪過濾
        messages_to_send = self._inject_dynamic_context(sanitized)
        resp = self.llm.chat(messages_to_send, ...)
        ...
```

**不要只在啟動時跑一次** — 多輪對話中、中斷 / 失敗 / network error 都可能殘留 orphan。

### Anti-patterns

- ❌ **只 single-pass** — 刪掉 orphan tool 後可能讓前面 assistant 的 tool_calls 變成新的 orphan,要 multi-pass
- ❌ **mutate `self.messages`** — sanitize 應該回 copy、不污染原始歷史(留著日後 export / debug)
- ❌ **assume Gemini 是唯一嚴格的 provider** — OpenAI / Anthropic 也有類似約束,sanitizer 通用化是對的

## Host-terminal logging — 雙寫設計(必做)

**問題**:使用者在 TG / Web 看到的錯誤訊息是「文字化」的(`{"error": "..."}`),沒 stack trace、沒上下文。**真實 debug 必須回 host 端 terminal**。但如果 Orchestrator 只把 error 丟給前端、host terminal 啥都沒印,使用者 / 你會盲目猜原因。

實戰回報過的痛:tool 參數錯、user 在 TG 看到「unexpected keyword argument '_chat_id'」、回 host 終端機**什麼都沒有** — 因為 Orchestrator 把 exception 吃掉變成 dict 結果。

**解法**:**雙寫 logging**(stdout + 給前端的 text 兩條獨立路徑)。

### `agent/logger.py` — 中央 logger,3 行設定

```python
# agent/logger.py
import logging, sys, os

def setup_logging():
    """Idempotent — call once from run_bot.py / web_adapter top."""
    level = os.getenv("AGENT_LOG_LEVEL", "INFO").upper()
    fmt = "[%(asctime)s] %(levelname)-7s %(name)-20s %(message)s"
    logging.basicConfig(
        level=getattr(logging, level, logging.INFO),
        format=fmt,
        datefmt="%H:%M:%S",
        stream=sys.stderr,   # ★ stderr,不污染 stdout(streamlit/TG 都會看 stdout)
        force=True,
    )

# Convenience
log = logging.getLogger("agent")
```

`run_bot.py` 開頭呼一次 `setup_logging()`、`web_adapter_streamlit.py` 開頭也呼。

### `orchestrator.py` — error 雙寫

```python
import logging
log = logging.getLogger("agent.orchestrator")

def step(self):
    for tc in tool_calls:
        name = tc.name
        args = dict(tc.args or {})
        log.info("→ tool %s(args=%s)", name, _short(args))   # ★ host log
        try:
            result = self.registry.run(name, args)
        except Exception as e:
            log.exception("✗ tool %s raised", name)           # ★ stack trace to host
            result = {"error": f"{type(e).__name__}: {e}"}    # ★ short text to LLM/user
        if isinstance(result, dict) and "error" in result:
            log.warning("tool %s returned error: %s",          # ★ even string errors logged
                        name, result["error"][:200])
        yield {"role": "tool", "tool_name": name,
               "content": json.dumps(result, default=str)}
```

關鍵原則:
- **tool exception** → `log.exception()` 印完整 stack trace 到 stderr
- **tool returned `{"error": ...}`** → `log.warning()` 印 error 訊息
- **正常 tool call** → `log.info()` 印 tool name + 縮短的 args
- 給 LLM / 使用者看的只是短 `{"error": "..."}` dict、stack trace 不外洩

### `shell_tool.py` — sandbox 失敗 stdout / stderr 必印 host

特別重要 — sandbox 內 command 在容器跑,使用者只看到 `{"ok": False, "exit_code": 1}`:

```python
log = logging.getLogger("agent.shell")

def run(self, command, cwd=None):
    # ... (deny-list / permissions / allowlist checks)
    log.info("[%s] exec: %s (cwd=%s)", self.mode, command[:120], cwd)
    try:
        r = subprocess.run(args, capture_output=True, text=True, timeout=120)
    except subprocess.TimeoutExpired:
        log.warning("[%s] TIMEOUT after 120s: %s", self.mode, command[:120])
        return {"error": f"timeout (>120s)", "command": command}
    if r.returncode != 0:
        log.warning(
            "[%s] EXIT %d: %s\n--- stdout ---\n%s\n--- stderr ---\n%s",
            self.mode, r.returncode, command[:120],
            (r.stdout or "")[-2000:], (r.stderr or "")[-2000:],
        )
    return {"ok": r.returncode == 0, "exit_code": r.returncode, ...}
```

**沙盒模式特別好處**:LLM 寫的 Python 在 container 內炸、stack trace 從 container 流回 host stderr,使用者 / 你回 host terminal 一眼看完整錯。

### `llm_client.py` — API error / retry

```python
log = logging.getLogger("agent.llm")

def _retry(fn, max_attempts=4):
    last = None
    for i in range(max_attempts):
        try:
            return fn()
        except Exception as e:
            last = e
            if i >= len(_BACKOFF) or not _is_retryable(e):
                log.error("LLM call failed (giving up): %s", e)
                raise
            log.warning("LLM attempt %d failed (retrying in %ds): %s",
                        i + 1, _BACKOFF[i], str(e)[:200])
            time.sleep(_BACKOFF[i])
```

### 觀測什麼 — 終端機典型輸出

```
[01:23:45] INFO    agent.orchestrator   → tool web_search(args={'query': 'gemini news'})
[01:23:48] INFO    agent.orchestrator   → tool write_note(args={'title': '...', 'confirm': False})
[01:23:49] WARNING agent.shell          [sandbox] EXIT 1: python -c "import xx"
                                        --- stdout ---
                                        --- stderr ---
                                        ModuleNotFoundError: No module named 'xx'
[01:23:50] EXCEPTION agent.orchestrator ✗ tool write_note raised
                                        Traceback (most recent call last):
                                          File "...", line 234, in write_note
                                          ...
                                          TypeError: write_note() got an unexpected keyword argument '_chat_id'
```

**這就是 SKILL 文檔說「實戰回報過的高頻坑」的證據**。Logger 把 LLM/前端不會看到的 stack trace 留給開發者 debug。

### Anti-patterns

- ❌ **只用 `print()`** — 沒 level / timestamp / module 名,grep 不到、debug 全靠目測
- ❌ **logger 印到 stdout** — Streamlit 用 stdout 渲染、會撞 layout;TG 不在乎 stdout 但 stderr 是業界慣例
- ❌ **把 stack trace 也送 LLM** — 浪費 token、把實作細節給 LLM 反而誤導
- ❌ **沒 setup_logging()** — Python 預設 logging level 是 WARNING、`log.info()` 你完全看不到

## 為什麼不做 REPL

之前版本 Phase 4 結束會給一個桌面 `agent_repl.py`、使用者在 terminal 打字測試。**這個 skill 不做這件事**,理由:

- TG 是唯一前端、所有體驗投資集中在 TG adapter
- REPL 跟 TG 走的路徑不同(history、approval、訊息分段)、雙路徑會 drift
- 使用者其實少在 terminal 用、更可能在手機開 TG 試

**測試方式**:用 Phase 8 把 bot 跑起來、跟自己的 bot 私訊測。

## 單元測試(無 TG、無 LLM 也可以跑)

```python
# tests/test_orchestrator.py
def test_tool_call():
    from agent.tool_registry import Tool, ToolRegistry
    from agent.orchestrator import Orchestrator

    r = ToolRegistry()
    r.register(Tool(name="echo", description="echo input",
                    parameters={"type":"object","properties":{"text":{"type":"string"}}},
                    func=lambda text: {"out": text}))

    class FakeLLM:
        calls = 0
        def chat(self, messages, tools=None, model=None):
            from agent.llm_client import LLMResponse, ToolCall
            self.calls += 1
            if self.calls == 1:
                return LLMResponse(function_calls=[ToolCall(name="echo", args={"text":"hi"})])
            return LLMResponse(text="done")

    orch = Orchestrator(FakeLLM(), r)
    orch.add_user("test")
    msgs = list(orch.step())
    assert any(m["role"]=="tool" and "hi" in m["content"] for m in msgs)
    assert msgs[-1]["content"] == "done"
```

跑得過 = Phase 4 OK。

## 檢查清單

- [ ] `Orchestrator` 是 stateful、`messages` 為 instance 屬性、**沒有任何 class / global variable 共用**
- [ ] `orchestrator_factory()` callable 拿到 fresh instance、跨 chat 不汙染
- [ ] `_trim_history` 跟 `_TOOL_RESULT_MAX` 兩個防爆機制有實際生效(寫 unit test 驗)
- [ ] `step()` 是 generator、每個 yield 是 dict 而不是 string
- [ ] 跑 unit test 通過、不需要真 LLM key
