"""Telegram bot adapter — per-chat session, polling lock, tool progress, file auto-send.

Required:
    pip install python-telegram-bot>=21.0

Set in .env:
    TELEGRAM_BOT_TOKEN=<from @BotFather>
    TELEGRAM_AUTHORIZED_USERS=<comma-separated numeric user IDs from @userinfobot>
                              (leave empty = dev mode, anyone can chat — local only!)

Features:
  - Per-chat session isolation (`dict[chat_id, Orchestrator]`)
  - Polling lock (prevents two instances → TG 409 Conflict)
  - Tool progress callback (a `▸ tool_name(args)` line before each tool runs)
  - 4000-char chunking + Markdown → HTML conversion (TG hard cap is 4096)
  - File/image auto-send (tool result with `output_file`/`path`/`output_files`)
  - Shell approval inline button (Phase 10 hook — not used yet)
"""
from __future__ import annotations

import asyncio
import ctypes
import html
import json
import os
import re
import uuid
from pathlib import Path
from typing import Any, Callable

try:
    from telegram import (
        InlineKeyboardButton, InlineKeyboardMarkup, Update,
        constants as tg_constants,
    )
    from telegram.ext import (
        ApplicationBuilder, CallbackQueryHandler, CommandHandler,
        ContextTypes, MessageHandler, filters,
    )
except ImportError as e:
    raise RuntimeError(
        "python-telegram-bot not installed. pip install python-telegram-bot>=21.0"
    ) from e


TG_MSG_MAX = 4000        # < 4096 actual limit; leave headroom for HTML tags
LOCK_DIR = Path.home() / ".cache" / "agent-tg"
TYPING_REFRESH_S = 4.0


def _parse_authorized() -> set[int]:
    raw = os.environ.get("TELEGRAM_AUTHORIZED_USERS", "").strip()
    return {int(x) for x in raw.split(",") if x.strip().lstrip("-").isdigit()}


# ─────────────────────────────────────────────────────────────
# Markdown → HTML (TG-safe subset)
# ─────────────────────────────────────────────────────────────
def markdown_to_tg_html(text: str) -> str:
    """Convert a common-Markdown subset to TG HTML parse mode.

    Handles: ```code blocks```, `inline code`, **bold**, *italic*, [text](url)
    Escapes < > & so the parser doesn't crash on stray HTML."""
    out_parts: list[str] = []
    code_re = re.compile(r"```([a-zA-Z0-9_-]*)\n?(.*?)```", re.DOTALL)
    last_end = 0
    for m in code_re.finditer(text):
        out_parts.append(_process_inline_markdown(text[last_end:m.start()]))
        code_text = html.escape(m.group(2))
        out_parts.append(f"<pre><code>{code_text}</code></pre>")
        last_end = m.end()
    out_parts.append(_process_inline_markdown(text[last_end:]))
    return "".join(out_parts)


def _process_inline_markdown(text: str) -> str:
    if not text:
        return ""
    text = html.escape(text, quote=False)
    text = re.sub(r"`([^`\n]+?)`", r"<code>\1</code>", text)
    text = re.sub(r"\*\*([^*\n]+?)\*\*", r"<b>\1</b>", text)
    text = re.sub(r"(?<!\*)\*([^*\n]+?)\*(?!\*)", r"<i>\1</i>", text)
    text = re.sub(
        r"\[([^\]\n]+)\]\(([^)\s]+)\)",
        lambda m: f'<a href="{html.escape(m.group(2), quote=True)}">{m.group(1)}</a>',
        text,
    )
    return text


def chunk_text(text: str, max_len: int = TG_MSG_MAX) -> list[str]:
    """Split text into TG-sized chunks, preferring \\n\\n then \\n boundaries."""
    if len(text) <= max_len:
        return [text]
    parts: list[str] = []
    remaining = text
    while len(remaining) > max_len:
        cut = remaining.rfind("\n\n", 0, max_len)
        if cut < max_len // 4:
            cut = remaining.rfind("\n", 0, max_len)
        if cut < max_len // 4:
            cut = max_len
        parts.append(remaining[:cut])
        remaining = remaining[cut:].lstrip("\n")
    if remaining:
        parts.append(remaining)
    return parts


# ─────────────────────────────────────────────────────────────
# Polling lock — block second instance from claiming TG polling
# ─────────────────────────────────────────────────────────────
def _pid_alive(pid: int) -> bool:
    """Cross-platform 'is this PID still running?' that NEVER kills the target.

    Critical: on Windows, `os.kill(pid, 0)` calls TerminateProcess — it would
    actually kill the process. Use OpenProcess + GetExitCodeProcess instead.
    """
    if os.name == "nt":
        PROCESS_QUERY_LIMITED_INFORMATION = 0x1000
        STILL_ACTIVE = 259
        kernel32 = ctypes.windll.kernel32
        kernel32.OpenProcess.argtypes = [
            ctypes.c_uint32, ctypes.c_int, ctypes.c_uint32,
        ]
        kernel32.OpenProcess.restype = ctypes.c_void_p
        kernel32.GetExitCodeProcess.argtypes = [
            ctypes.c_void_p, ctypes.POINTER(ctypes.c_uint32),
        ]
        kernel32.GetExitCodeProcess.restype = ctypes.c_int
        kernel32.CloseHandle.argtypes = [ctypes.c_void_p]
        kernel32.CloseHandle.restype = ctypes.c_int

        h = kernel32.OpenProcess(PROCESS_QUERY_LIMITED_INFORMATION, 0, pid)
        if not h:
            return False
        try:
            exit_code = ctypes.c_uint32()
            ok = kernel32.GetExitCodeProcess(h, ctypes.byref(exit_code))
            return bool(ok) and exit_code.value == STILL_ACTIVE
        finally:
            kernel32.CloseHandle(h)
    else:
        try:
            os.kill(pid, 0)
            return True
        except (OSError, PermissionError):
            return False


class PollingLock:
    """Per-token lock file. Block second instance from claiming TG polling."""

    def __init__(self, token: str):
        LOCK_DIR.mkdir(parents=True, exist_ok=True)
        self.path = LOCK_DIR / f"poll-{token[-12:]}.lock"

    def acquire(self) -> bool:
        if self.path.exists():
            try:
                pid = int(self.path.read_text().strip())
                if _pid_alive(pid):
                    return False
            except (ValueError, OSError):
                pass  # stale lock — overwrite below
        try:
            self.path.write_text(str(os.getpid()))
            return True
        except OSError:
            return False

    def release(self):
        try:
            self.path.unlink()
        except OSError:
            pass


# ─────────────────────────────────────────────────────────────
# Tool progress formatter — customized for email-to-obsidian tools
# ─────────────────────────────────────────────────────────────
_TOOL_PROGRESS_ZH = {
    # Project tools (Phase 2)
    "fetch_url": "🌐 抓網頁",
    "list_subfolders": "📂 列 vault 子資料夾",
    "search_vault": "🔍 搜尋 vault",
    "read_note": "📄 讀筆記",
    "git_status": "🔧 查 git 狀態",
    "classify_content": "🤖 LLM 分類內容",
    "write_note": "✍️ 寫筆記",
    "update_settings": "⚙️ 更新設定",
    "git_commit_and_push": "🚀 Git commit & push",
    "current_state": "🧭 查 agent 狀態",
    # Generic file ops (Phase 7+10)
    "read_file": "📖 讀檔",
    "write_file": "✍️ 寫檔",
    "edit_file": "🔧 改檔",
    "glob_paths": "🗂 列檔",
    "grep_files": "🔎 全檔搜尋",
    "view_image": "👀 看圖",
    "ask_user": "❓ 問你",
    "done": "✅ 完成",
    # Sandbox execution
    "run_shell": "🐳 跑 shell",
    "run_python": "🐍 跑 Python",
    # Web search
    "web_search": "🌍 網路搜尋",
    # Self-evolution
    "list_proposed_tools": "📜 列待審工具",
    "propose_tool": "🧬 草稿新工具",
    "merge_proposed_tool": "🔀 合併新工具",
    "reject_proposed_tool": "🚫 棄稿新工具",
}


def format_tool_progress(name: str, args: dict) -> str:
    zh = _TOOL_PROGRESS_ZH.get(name, f"▸ {name}")
    if not args:
        return zh
    preview = {}
    for k, v in args.items():
        sv = str(v)
        preview[k] = sv if len(sv) <= 40 else sv[:40] + "..."
    arg_str = ", ".join(f"{k}={v}" for k, v in preview.items())
    return f"{zh}({arg_str})"


# ─────────────────────────────────────────────────────────────
# TG Adapter
# ─────────────────────────────────────────────────────────────
class TelegramAdapter:
    """Wraps an orchestrator factory and exposes it via Telegram.

    Tools that produce files should return JSON with one of:
        {"output_file": "/abs/path"}      |  {"saved_path": "/abs/path"}
        {"path": "/abs/path"}             |  {"output_files": ["/abs/path1", ...]}
    Image extensions (.png/.jpg/.gif/.webp) → send_photo; others → send_document.

    For email-to-obsidian specifically, `write_note(confirm=True)` returns
    `{"written_to": ".../note.md"}` — the .md gets auto-sent as a document so
    the user sees the actual file in TG.
    """

    FILE_KEYS = ("output_file", "saved_path", "path", "file", "written_to")
    FILE_LIST_KEYS = ("output_files", "files")

    def __init__(
        self,
        orchestrator_factory: Callable[[], Any],
        token: str | None = None,
    ):
        self.orch_factory = orchestrator_factory
        self.authorized = _parse_authorized()
        token = token or os.environ.get("TELEGRAM_BOT_TOKEN")
        if not token:
            raise RuntimeError("TELEGRAM_BOT_TOKEN not set in env")

        self._lock = PollingLock(token)
        if not self._lock.acquire():
            raise RuntimeError(
                "Another agent instance is polling this token. "
                "Stop it first, or delete the stale lock file under "
                f"{LOCK_DIR}/poll-{token[-12:]}.lock"
            )

        self.app = ApplicationBuilder().token(token).build()
        self.app.add_handler(CommandHandler("start", self._on_start))
        self.app.add_handler(CommandHandler("reset", self._on_reset))
        self.app.add_handler(CommandHandler("help", self._on_help))
        self.app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, self._on_text))
        self.app.add_handler(MessageHandler(filters.PHOTO, self._on_photo))
        self.app.add_handler(CallbackQueryHandler(self._on_callback))

        # Where to stash downloaded photos. WSL-accessible so sandbox can read
        # if the LLM wants to run_python on the file later.
        self._photo_dir = Path.home() / ".cache" / "agent-tg" / "photos"
        self._photo_dir.mkdir(parents=True, exist_ok=True)

        # Per-chat orchestrator instances
        self._orchs: dict[int, Any] = {}
        # Pending shell approvals: rid -> Future (Phase 10 hook)
        self._approvals: dict[str, asyncio.Future] = {}

    # ─── public approval API (sync, for shell tool in Phase 10) ─────
    def request_approval_sync(self, chat_id: int, prompt: str, timeout: int = 300) -> bool:
        loop = self.app.update_queue._loop or asyncio.get_event_loop()
        coro = self._request_approval(chat_id, prompt, timeout)
        fut = asyncio.run_coroutine_threadsafe(coro, loop)
        try:
            return fut.result(timeout=timeout + 5)
        except Exception:
            return False

    async def _request_approval(self, chat_id: int, prompt: str, timeout: int = 300) -> bool:
        rid = str(uuid.uuid4())[:8]
        loop = asyncio.get_event_loop()
        fut: asyncio.Future = loop.create_future()
        self._approvals[rid] = fut

        kb = InlineKeyboardMarkup([[
            InlineKeyboardButton("✓ 通過", callback_data=f"approve|{rid}"),
            InlineKeyboardButton("✗ 拒絕", callback_data=f"deny|{rid}"),
        ]])
        try:
            preview = prompt[:500] + ("..." if len(prompt) > 500 else "")
            await self.app.bot.send_message(
                chat_id=chat_id,
                text=f"⚠ 確認操作:\n<pre>{html.escape(preview)}</pre>",
                parse_mode=tg_constants.ParseMode.HTML,
                reply_markup=kb,
            )
        except Exception:
            self._approvals.pop(rid, None)
            return False

        try:
            return await asyncio.wait_for(fut, timeout=timeout)
        except asyncio.TimeoutError:
            return False
        finally:
            self._approvals.pop(rid, None)

    # ─── auth & per-chat orch ────────────────────────────────────
    def _ok(self, update: Update) -> bool:
        if not update.effective_user:
            return False
        if not self.authorized:
            return True  # dev mode
        return update.effective_user.id in self.authorized

    def _orch_for(self, chat_id: int):
        if chat_id not in self._orchs:
            self._orchs[chat_id] = self.orch_factory()
        return self._orchs[chat_id]

    # ─── handlers ────────────────────────────────────────────────
    async def _on_start(self, update: Update, ctx: ContextTypes.DEFAULT_TYPE):
        if not self._ok(update):
            await update.message.reply_text("⛔ not authorized")
            return
        await update.message.reply_text(
            "👋 你好,我是 email-to-obsidian agent。\n"
            "貼網址、文字、或直接問我「列出 inbox 最近的筆記」之類。\n\n"
            "可用指令:\n"
            "  /reset — 清掉這個 chat 的記憶\n"
            "  /help  — 列出我有什麼工具"
        )

    async def _on_reset(self, update: Update, ctx: ContextTypes.DEFAULT_TYPE):
        if not self._ok(update):
            return
        chat_id = update.effective_chat.id
        self._orchs.pop(chat_id, None)
        await update.message.reply_text("✅ 已清掉這個 chat 的對話記憶。")

    async def _on_help(self, update: Update, ctx: ContextTypes.DEFAULT_TYPE):
        if not self._ok(update):
            return
        try:
            names = list(self._orch_for(update.effective_chat.id).registry.names())
        except AttributeError:
            names = []
        if not names:
            await update.message.reply_text("(no tools registered)")
            return
        await update.message.reply_text(
            f"可用工具({len(names)}):\n" + "\n".join(f"• {n}" for n in names)
        )

    async def _on_text(self, update: Update, ctx: ContextTypes.DEFAULT_TYPE):
        if not self._ok(update):
            return
        text = update.message.text or ""
        chat_id = update.effective_chat.id
        orch = self._orch_for(chat_id)
        orch.add_user(text)

        stop_typing = asyncio.Event()
        typing_task = asyncio.create_task(self._typing_keepalive(chat_id, stop_typing))
        try:
            await self._stream_orchestrator(update, orch)
        except Exception as e:
            await update.message.reply_text(f"⚠ agent error: {e}")
        finally:
            stop_typing.set()
            try:
                await asyncio.wait_for(typing_task, timeout=1.0)
            except (asyncio.TimeoutError, Exception):
                if not typing_task.done():
                    typing_task.cancel()

    async def _typing_keepalive(self, chat_id: int, stop: asyncio.Event):
        while not stop.is_set():
            try:
                await self.app.bot.send_chat_action(chat_id=chat_id, action="typing")
            except Exception:
                pass
            try:
                await asyncio.wait_for(stop.wait(), timeout=TYPING_REFRESH_S)
            except asyncio.TimeoutError:
                continue

    async def _stream_orchestrator(self, update: Update, orch):
        """Iterate orch.step() and stream to TG: tool progress + final text + files."""
        loop = asyncio.get_event_loop()
        files_to_send: list[str] = []
        final_text_buf: list[str] = []

        async def push(msg: dict):
            role = msg.get("role")
            if role == "assistant":
                if msg.get("tool_calls"):
                    for tc in msg["tool_calls"]:
                        name = tc.name if hasattr(tc, "name") else tc.get("name")
                        args = tc.args if hasattr(tc, "args") else tc.get("args", {})
                        progress = format_tool_progress(name, dict(args or {}))
                        try:
                            await update.message.reply_text(progress)
                        except Exception:
                            pass
                elif msg.get("content"):
                    final_text_buf.append(msg["content"])
            elif role == "tool":
                files_to_send.extend(self._extract_files(msg.get("content", "")))

        def producer():
            try:
                for m in orch.step():
                    asyncio.run_coroutine_threadsafe(push(m), loop).result(timeout=60)
            except Exception as e:
                asyncio.run_coroutine_threadsafe(
                    push({"role": "assistant", "content": f"[orch error] {e}"}),
                    loop,
                ).result(timeout=10)

        await asyncio.to_thread(producer)

        if final_text_buf:
            await self._send_long(update, "\n\n".join(final_text_buf))
        elif not files_to_send:
            await update.message.reply_text("(沒有回應)")

        for path in files_to_send:
            await self._send_file(update, path)

    async def _on_photo(self, update: Update, ctx: ContextTypes.DEFAULT_TYPE):
        """User sent a photo (with optional caption). Download → orch.add_user
        with attachment → LLM sees the image on the next chat call.
        """
        if not self._ok(update):
            return
        msg = update.message
        if not msg or not msg.photo:
            return
        chat_id = update.effective_chat.id

        # Telegram sends multiple sizes — pick the largest
        photo = msg.photo[-1]
        try:
            tg_file = await ctx.bot.get_file(photo.file_id)
        except Exception as e:
            await msg.reply_text(f"⚠ 抓圖失敗:{e}")
            return

        # Filename: <chat>_<file_id_tail>.jpg
        local_path = self._photo_dir / f"{chat_id}_{photo.file_id[-12:]}.jpg"
        try:
            await tg_file.download_to_drive(custom_path=str(local_path))
        except Exception as e:
            await msg.reply_text(f"⚠ 下載失敗:{e}")
            return

        caption = (msg.caption or "").strip() or "(使用者傳了一張圖,沒附文字)"
        orch = self._orch_for(chat_id)
        orch.add_user(
            caption,
            attachments=[{"path": str(local_path), "mime": "image/jpeg"}],
        )

        # Same stream-orchestrator flow as _on_text
        stop_typing = asyncio.Event()
        typing_task = asyncio.create_task(self._typing_keepalive(chat_id, stop_typing))
        try:
            await self._stream_orchestrator(update, orch)
        except Exception as e:
            await msg.reply_text(f"⚠ agent error: {e}")
        finally:
            stop_typing.set()
            try:
                await asyncio.wait_for(typing_task, timeout=1.0)
            except (asyncio.TimeoutError, Exception):
                if not typing_task.done():
                    typing_task.cancel()

    async def _on_callback(self, update: Update, ctx: ContextTypes.DEFAULT_TYPE):
        q = update.callback_query
        if not q or not q.data:
            return
        if not self._ok(update):
            await q.answer("not authorized")
            return
        try:
            action, rid = q.data.split("|", 1)
        except ValueError:
            await q.answer()
            return
        fut = self._approvals.get(rid)
        if fut and not fut.done():
            fut.set_result(action == "approve")
        await q.answer()
        try:
            await q.edit_message_text(
                (q.message.text_html or q.message.text or "") + f"\n\n→ {action}d",
                parse_mode=tg_constants.ParseMode.HTML,
            )
        except Exception:
            pass

    # ─── send helpers ────────────────────────────────────────────
    async def _send_long(self, update: Update, text: str):
        for chunk in chunk_text(text):
            try:
                html_text = markdown_to_tg_html(chunk)
                await update.message.reply_text(
                    html_text, parse_mode=tg_constants.ParseMode.HTML,
                    disable_web_page_preview=True,
                )
            except Exception:
                await update.message.reply_text(chunk)

    async def _send_file(self, update: Update, path: str):
        p = Path(path)
        if not p.exists() or not p.is_file():
            return
        try:
            if p.suffix.lower() in (".png", ".jpg", ".jpeg", ".gif", ".webp"):
                with p.open("rb") as f:
                    await update.message.reply_photo(f, caption=p.name)
            else:
                with p.open("rb") as f:
                    await update.message.reply_document(f, filename=p.name)
        except Exception as e:
            await update.message.reply_text(f"(failed to send {p.name}: {e})")

    def _extract_files(self, tool_result_str: Any) -> list[str]:
        try:
            d = json.loads(tool_result_str) if isinstance(tool_result_str, str) else tool_result_str
        except Exception:
            return []
        if not isinstance(d, dict):
            return []
        out: list[str] = []
        for k in self.FILE_KEYS:
            v = d.get(k)
            if isinstance(v, str) and v:
                out.append(v)
        for k in self.FILE_LIST_KEYS:
            v = d.get(k)
            if isinstance(v, list):
                out.extend([x for x in v if isinstance(x, str)])
        return out

    # ─── run ─────────────────────────────────────────────────────
    def run(self):
        """Block-run polling. delete_webhook first so old session releases."""
        async def _pre_clear(app):
            try:
                await app.bot.delete_webhook(drop_pending_updates=True)
            except Exception:
                pass

        self.app.post_init = _pre_clear  # python-telegram-bot 21+
        if not self.authorized:
            print("[WARN] TELEGRAM_AUTHORIZED_USERS 沒設、處於 dev mode、任何人都能聊。"
                  "production 請務必設定。")
        print(f"[INFO] Telegram bot starting (polling) — authorized={self.authorized or 'ANY'}")
        try:
            self.app.run_polling(drop_pending_updates=True)
        finally:
            self._lock.release()
