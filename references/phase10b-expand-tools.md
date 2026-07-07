# Phase 10b — Expand the basic toolkit (after sandbox is up)

## When this phase runs

**After Phase 10 sandbox has been built and verified** (i.e., `sandbox_preflight()` returns OK and at least one `docker exec e2o-sandbox echo hello` works), and **before** Phase 11/12.

If the user chose host mode in Phase 10, skip the sandbox-only tools below (`run_python`) but still offer the rest.

## Why this phase exists

Phase 2 wraps the project's existing functions as tools. That's enough for the original Streamlit-style workflow but **dramatically under-tooled** for an LLM agent. Once the sandbox is up, the LLM has a safety net — there's no reason to keep withholding generic file ops.

Without this phase, the LLM has 10ish project-specific tools and **no way to**:
- Read an arbitrary file
- Write a config / patch a file
- Find files by glob / grep their content
- Run ad-hoc Python in a safe container
- Look at an image the user sent or another tool produced
- Ask the user a clarifying question (with options)
- Signal "I'm done, stop the loop"

The fix is to add a small, opinionated kit of "agent-fundamentals" tools. **Ask the user before adding them** — but the default recommendation is "yes, all of them".

## The toolkit (8 tools + 1 sandbox-only)

| Tool | Description | Notes |
|---|---|---|
| `read_file(path, max_bytes, encoding)` | Read text file, cap at max_bytes | Permission-checked |
| `write_file(path, content, confirm)` | Write file, two-step | Permission-checked |
| `edit_file(path, old, new, confirm, replace_all)` | String-replace edit, two-step | Permission-checked |
| `glob_paths(pattern, root, max_results)` | Glob like rg/find | Permission-checked |
| `grep_files(pattern, path, file_glob, max_results, case_insensitive)` | Regex search across files | Permission-checked |
| `view_image(path)` | Attach image to next LLM call (multi-modal) | Requires LLM client multi-modal support |
| `ask_user(question, options)` | Explicit user question | Orchestrator ends turn |
| `done(reason)` | Mark task complete | Orchestrator ends turn |
| `run_python(code, cwd, timeout)` | Run Python in sandbox container | **Sandbox mode only** |

Plus the existing `run_shell` (renamed from `shell` in original Phase 10) — in sandbox mode, allowlist gets bypassed because the container is the boundary.

## Concrete AskUserQuestion to call

```python
AskUserQuestion(questions=[{
    "question": "沙盒已就緒。要不要把這套基礎工具給 agent?(都是 LLM agent 標配、不裝就只剩專案 wrap 的工具、無法 ad-hoc 處理檔案)",
    "header": "基礎工具",
    "multiSelect": True,
    "options": [
        {"label": "read_file / glob_paths / grep_files", "description": "讀檔、列檔、全檔搜尋。基本必裝"},
        {"label": "write_file / edit_file", "description": "寫檔、改檔(都 two-step confirm)。LLM 想 patch code 才用得到"},
        {"label": "view_image", "description": "TG 收到圖 / 工具產出的圖 → LLM 視覺辨識。需 multi-modal LLM(Gemma 4 / GPT-4o / Claude 3+)"},
        {"label": "ask_user / done", "description": "互動信號:agent 反問 / 標誌結束。提升對話品質"},
        {"label": "run_python (僅 sandbox 模式)", "description": "LLM 跑任意 Python in container。配合 self-evolution 很有用"},
    ],
}])
```

Default recommendation if user says "都給":all of them. The marginal cost of an unused tool is one extra schema in every chat call (~50 tokens each).

## Implementation

### 1. Generic file ops module

Create `agent/file_tools.py`:

```python
"""File ops — host-side, permission-checked, two-step on writes."""
from pathlib import Path
import re

def read_file(path: str, max_bytes: int = 64000, encoding: str = "utf-8") -> dict:
    from agent.tools import _check  # avoid circular at import
    p = Path(path).expanduser().resolve()
    err = _check(p, "read")
    if err:
        return err
    if not p.exists() or not p.is_file():
        return {"error": f"file not found: {path}"}
    try:
        size = p.stat().st_size
        with p.open("rb") as f:
            raw = f.read(max_bytes)
        try:
            content = raw.decode(encoding)
            is_binary = False
        except UnicodeDecodeError:
            content = f"(binary {size} bytes, first 200 hex)\n" + raw[:200].hex()
            is_binary = True
        return {
            "path": str(p), "size_bytes": size, "is_binary": is_binary,
            "content": content, "truncated": size > max_bytes and not is_binary,
            "bytes_read": min(size, max_bytes),
        }
    except Exception as e:
        return {"error": str(e)}


def write_file(path: str, content: str, confirm: bool = False, encoding="utf-8") -> dict:
    from agent.tools import _check
    p = Path(path).expanduser().resolve()
    err = _check(p, "write")
    if err:
        return err
    will_overwrite = p.exists()
    if not confirm:
        existing_head = ""
        if will_overwrite and p.is_file():
            try:
                existing_head = p.read_text(encoding=encoding)[:300]
            except Exception:
                existing_head = "(cannot read existing for preview)"
        return {
            "confirm_required": True,
            "would_write_to": str(p),
            "will_overwrite": will_overwrite,
            "existing_first_300": existing_head if will_overwrite else None,
            "new_first_300": content[:300],
            "new_total_chars": len(content),
        }
    try:
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(content, encoding=encoding)
        return {"written_to": str(p), "size_bytes": p.stat().st_size, "overwrote": will_overwrite}
    except Exception as e:
        return {"error": str(e)}


def edit_file(path: str, old: str, new: str, confirm: bool = False,
              replace_all: bool = False, encoding="utf-8") -> dict:
    """String-replace edit. Two-step. Requires `old` to occur exactly once,
    unless replace_all=True. Returns occurrence count if ambiguous."""
    # ... (see assets/file_tools.py for full impl)


def glob_paths(pattern: str, root: str = ".", max_results: int = 200) -> dict:
    """Glob with rglob if pattern contains **, else glob. Permission-checked."""
    # ... (see assets/file_tools.py)


def grep_files(pattern: str, path: str = ".", file_glob: str = "**/*",
               max_results: int = 100, case_insensitive: bool = False) -> dict:
    """Regex search across files. Returns hits as [{file, line, text}]."""
    # ... (see assets/file_tools.py)
```

### 2. Interaction tools (ask_user / done)

Add to `agent/tools.py`:

```python
def _ask_user(question: str, options: list[str] | None = None) -> dict:
    return {
        "asked": question, "options": options or [],
        "next_step": "End turn; wait for user reply.",
    }

def _done(reason: str = "") -> dict:
    return {"done": True, "reason": reason}
```

Wire into orchestrator — both end the planner loop early:

```python
# orchestrator.py step() loop
saw_done = False
saw_ask_user = False
for tc in tool_calls:
    # ... run tool ...
    if name == "done": saw_done = True
    elif name == "ask_user": saw_ask_user = True
# after the for-loop:
if saw_done or saw_ask_user:
    return
```

### 3. view_image + multi-modal LLM client

This is the heaviest. Three files touched:

**`agent/file_tools.py`** — view_image returns a marker:

```python
def view_image(path: str) -> dict:
    """Attach image to NEXT chat call. Returns {_image_attachment: {path, mime}}."""
    p = Path(path).expanduser().resolve()
    # permissions.check, size cap (10MB), mime sniff by suffix
    return {
        "path": str(p), "mime": mime, "size_bytes": size,
        "_image_attachment": {"path": str(p), "mime": mime},
    }
```

**`agent/orchestrator.py`** — buffer attachments, pass through:

```python
def __init__(self, ...):
    self._pending_attachments: list[dict] = []

def add_user(self, text: str, attachments: list[dict] | None = None):
    self.messages.append({"role": "user", "content": text})
    if attachments:
        self._pending_attachments.extend(attachments)

def step(self):
    for _ in range(self.max_iters):
        attachments = self._pending_attachments or None
        self._pending_attachments = []
        resp = self.llm.chat(self.messages, tools=...,
                             attachments=attachments)
        # ... in tool-result loop:
        if isinstance(result, dict):
            att = result.get("_image_attachment")
            if att:
                self._pending_attachments.append(att)
```

**`agent/llm_client.py`** — accept `attachments` in `chat()`:

- **Gemini**: `Part.from_bytes(data=..., mime_type=...)` PREPENDED to last user Content (Gemma 4 rule: image before text). Also ensure `system_instruction` non-empty when images present (Gemma 4 vision quirk).
- **OpenAI/Groq/Ollama**: `content` becomes a list of `{type: "text", ...}` + `{type: "image_url", image_url: {url: data:URI}}` blocks.
- **Anthropic**: `content` becomes a list of `{type: "image", source: {type: "base64", media_type, data}}` + `{type: "text", text: ...}` blocks.

### 4. TG adapter — auto-attach user photos

`agent/telegram_adapter.py`:

```python
self.app.add_handler(MessageHandler(filters.PHOTO, self._on_photo))

async def _on_photo(self, update, ctx):
    photo = update.message.photo[-1]  # largest size
    tg_file = await ctx.bot.get_file(photo.file_id)
    local_path = self._photo_dir / f"{chat_id}_{photo.file_id[-12:]}.jpg"
    await tg_file.download_to_drive(custom_path=str(local_path))
    caption = (update.message.caption or "").strip() or "(使用者傳了一張圖)"
    orch.add_user(caption, attachments=[{"path": str(local_path), "mime": "image/jpeg"}])
    # ... same streaming flow as text handler ...
```

### 5. run_python in sandbox

`agent/shell_tool.py`:

```python
class SandboxShellTool(HostShellTool):
    def run_python(self, code: str, cwd=None, timeout=120) -> dict:
        """Execute Python via heredoc-stdin in container (sidesteps quoting hell)."""
        wsl_cwd = _to_wsl_path(cwd or os.getcwd())
        r = subprocess.run(
            ["wsl", "-e", "bash", "-c",
             f"docker exec -i -w {_shquote(wsl_cwd)} {self.container} python -"],
            input=code, capture_output=True, text=True, timeout=timeout,
        )
        return {"ok": ..., "exit_code": ..., "stdout": ..., "stderr": ..., ...}
```

Register conditionally:

```python
# tools.py register_all
if shell.mode == "sandbox":
    registry.register(Tool(name="run_python", ..., func=shell.run_python))
```

### 6. Run-shell sandbox mode bypass

Original Phase 10 `shell` tool used allowlist-only — even in sandbox mode it refused most commands. **Wrong design** when sandbox is up. New rule:

- **Host mode**: strict allowlist only (read-only commands; the rest refused without approval)
- **Sandbox mode**: allowlist BYPASSED; only the hard deny-list applies (rm -rf /, sudo, force-push to main, killing the container itself, etc.)

Rename `shell` → `run_shell` for consistency with `run_python`.

## System prompt addition

Add to `agent/system_prompt.py` after Phase 5/6/7 sections:

```
# 🐳 執行環境

- `run_shell` / `run_python` 跑在 {sandbox|host} 模式
  - SANDBOX = WSL Docker 容器 `e2o-sandbox`、任意 command / Python 都能跑
  - bind-mount <project_path> ↔ 同路徑(雙向寫!但 permissions 守 cwd)

**檔案操作優先序**:
1. read_file / write_file / edit_file / glob_paths / grep_files → 結構化、host 跑、permissions 守、寫操作 two-step。**首選**
2. run_python → 真正需要計算 / 第三方套件時
3. run_shell → 系統指令(git / pip)

**禁忌**:不要用 run_shell('cat foo') 取代 read_file('foo') — 後者明確、有 preview、不需 LLM 拼 shell quoting。

**互動信號**:
- ask_user(question, options?) — 需要使用者明確回答時。Orchestrator 收到會結束 turn。
- done(reason) — 任務做完、想停 loop。
```

## Anti-patterns

- ❌ Adding all 9 tools without asking the user. AskUserQuestion is mandatory.
- ❌ Wiring `run_python` in host mode (no sandbox = no isolation = LLM can `import os; shutil.rmtree('/')`).
- ❌ Skipping the orchestrator changes for ask_user / done — those tools become useless if the loop doesn't recognize them and exit early.
- ❌ Forgetting the multi-modal `system_instruction must be non-empty` Gemma 4 quirk → images get silently ignored.
- ❌ Stuffing image base64 into the tool result text. Use the `_image_attachment` marker pattern so the next chat call attaches the image as a real multi-modal Part.

## Checklist before moving on

- [ ] AskUserQuestion was called and the user picked which tools to enable.
- [ ] `agent/file_tools.py` exists with read_file / write_file / edit_file / glob_paths / grep_files / view_image (plus their schemas).
- [ ] orchestrator.py recognizes `done` / `ask_user` tool calls and exits the planner loop early.
- [ ] orchestrator.py has `_pending_attachments` buffer + step() consumes + passes to llm.chat().
- [ ] llm_client.py's chat() in EACH provider accepts `attachments=` and injects properly.
- [ ] telegram_adapter.py has a PHOTO handler that downloads + calls orch.add_user with attachment.
- [ ] If sandbox mode: SandboxShellTool has `run_python` method, registered as a tool.
- [ ] If sandbox mode: `run_shell` no longer enforces allowlist (deny-list still applies).
- [ ] System prompt updated to describe the new tools and their priority order.
- [ ] Smoke test: read_file an actual file, glob_paths a real pattern, grep_files a real term, run_python a `print(2+2)`, run_shell a write into /tmp inside container — all return expected results.
