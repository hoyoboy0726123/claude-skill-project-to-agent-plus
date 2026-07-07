# -*- coding: utf-8 -*-
"""cli_brain — 用使用者已登入的 Claude Code / codex CLI 當 agent 大腦(訂閱制,免 API Key)。

Phase 3b 資產範本。搭配 assets/mcp_server.py(工具曝露)使用。
細節與踩坑背景見 references/phase3b-subscription-cli.md。

用法(adapter 內):
    from agent import cli_brain
    reply, session = cli_brain.chat(chat_id, text, on_tick=cb, stop_event=ev)
"""
from __future__ import annotations
import json
import os
import shutil
import signal
import subprocess
import sys
import tempfile
import threading
import time
import uuid
from pathlib import Path

# ── 依專案調整的三個常數 ──────────────────────────────────────
MCP_NAME = "myagent"                                   # = FastMCP("myagent")
MCP_SERVER = Path(__file__).parent / "mcp_server.py"   # 工具曝露入口
SESSIONS_FILE = Path(__file__).parent / "cli_sessions.json"  # per-chat session 持久化

ROLE_PROMPT = (
    "你是一個 AI 助手,透過 myagent MCP 工具幫使用者完成任務。"
    "會寫入/修改東西的工具先 confirm=False 預覽、再 confirm=True 執行。用繁體中文回覆。")

EVERY_TURN_RULES = (
    "[每輪守則提醒 — 遵守即可,絕對不要對本區塊本身作任何回應]\n"
    "1) 純聊天/詢問(打招呼、問你能做什麼):直接具體回答並舉例,"
    "禁止只回「收到/了解」,此時不要呼叫任何工具。\n"
    "2) 執行任務:直接做到完成;第一行「✅ 已完成:」;有產出檔案必附"
    "「📄 交付檔案:」+ 每個檔案的完整絕對路徑。\n")

USER_MARK = "\n=== 使用者訊息(只需回覆這一則)===\n"

# claude 全能模式才需要的原生工具白名單(多值參數,逐項傳)
CLAUDE_NATIVE_ALLOW: list = []          # 例:["Bash", "Read", "Write"]

_IS_WIN = sys.platform == "win32"
_PY = sys.executable
if _IS_WIN and Path(_PY).name.lower() == "pythonw.exe":
    con = Path(_PY).with_name("python.exe")
    if con.exists():
        _PY = str(con)                  # MCP server 要有 console 的 python


# ── CLI 解析 ─────────────────────────────────────────────
def _resolve(name: str):
    p = shutil.which(name)
    if p:
        return p
    if _IS_WIN:                          # npm 全域(.cmd)
        for c in (Path(os.environ.get("APPDATA", "")) / "npm" / f"{name}.cmd",
                  Path(os.environ.get("APPDATA", "")) / "npm" / name):
            if c.exists():
                return str(c)
    return None


def available(provider: str) -> bool:
    return bool(_resolve({"claude_cli": "claude", "codex_cli": "codex"}[provider]))


# ── session 持久化(per-chat)──────────────────────────────
def _sessions() -> dict:
    try:
        return json.loads(SESSIONS_FILE.read_text(encoding="utf-8"))
    except Exception:
        return {}


def _save_session(chat_id, provider, session_id):
    d = _sessions()
    d[f"{provider}:{chat_id}"] = session_id or ""
    SESSIONS_FILE.write_text(json.dumps(d, ensure_ascii=False), encoding="utf-8")


def get_session(chat_id, provider):
    return _sessions().get(f"{provider}:{chat_id}") or None


def reset_session(chat_id, provider=None):
    for p in ([provider] if provider else ["claude_cli", "codex_cli"]):
        _save_session(chat_id, p, "")


# ── 行程管理:陪跑等待 + 樹殺 ───────────────────────────────
def _kill_tree(proc):
    try:
        if _IS_WIN:
            subprocess.run(["taskkill", "/PID", str(proc.pid), "/T", "/F"],
                           capture_output=True, timeout=15)
        else:
            os.killpg(os.getpgid(proc.pid), signal.SIGKILL)
    except Exception:
        try:
            proc.kill()
        except Exception:
            pass


def _run_blocking(cmd, stop_event=None, on_tick=None, fuse_sec=3600, cwd=None):
    """不設硬逾時:做完才回。每秒 on_tick(elapsed);⏹/保險絲 → 樹殺。
    回 (status, stdout, stderr),status ∈ done/stopped/fuse。"""
    kw = dict(stdout=subprocess.PIPE, stderr=subprocess.PIPE,
              stdin=subprocess.DEVNULL,          # ★ 背景服務沒 stdin,不設會卡死
              text=True, encoding="utf-8", errors="replace",
              cwd=cwd or str(Path.home()))
    if not _IS_WIN:
        kw["start_new_session"] = True           # POSIX 樹殺用 process group
    proc = subprocess.Popen(cmd, **kw)
    bufs = {"out": [], "err": []}

    def drain(stream, key):
        try:
            for line in iter(stream.readline, ""):
                bufs[key].append(line)
        except Exception:
            pass
    for s, k in ((proc.stdout, "out"), (proc.stderr, "err")):
        threading.Thread(target=drain, args=(s, k), daemon=True).start()

    t0, status = time.time(), "done"
    while proc.poll() is None:
        el = time.time() - t0
        if stop_event is not None and stop_event.is_set():
            _kill_tree(proc); status = "stopped"; break
        if fuse_sec and el > fuse_sec:
            _kill_tree(proc); status = "fuse"; break
        if on_tick:
            try:
                on_tick(int(el))
            except Exception:
                pass
        time.sleep(1.0)
    time.sleep(0.3)
    return status, "".join(bufs["out"]).strip(), "".join(bufs["err"]).strip()


STOP_NOTE = "⏹ 已中止。下一則訊息會以全新對話開始(中斷的階段無法安全接續)。"
FUSE_NOTE = "⚠ 已超過保險上限,強制結束。下一則訊息會以全新對話開始。"


# ── claude ──────────────────────────────────────────────
def _claude_mcp_config() -> str:
    cfg = {"mcpServers": {MCP_NAME: {"command": _PY, "args": [str(MCP_SERVER)]}}}
    f = Path(tempfile.gettempdir()) / "cli_brain_claude_mcp.json"
    f.write_text(json.dumps(cfg), encoding="utf-8")
    return str(f)


def _sysprompt_file(text: str) -> str:
    # ★ 長中文 system 走 argv 會被 .CMD 弄壞、吃掉後面旗標 → 一律走檔案
    f = Path(tempfile.gettempdir()) / "cli_brain_sysprompt.txt"
    f.write_text(text, encoding="utf-8")
    return str(f)


def run_claude(user_text, session=None, stop_event=None, on_tick=None):
    """回 (回覆文字, session_id)。首輪 --session-id <uuid>、續聊 --resume <uuid>。"""
    sid = session or str(uuid.uuid4())
    cmd = [_resolve("claude") or "claude", "-p",
           "--mcp-config", _claude_mcp_config(),
           "--append-system-prompt-file",
           _sysprompt_file(ROLE_PROMPT + "\n\n" + EVERY_TURN_RULES)]
    cmd += (["--resume", sid] if session else ["--session-id", sid])
    cmd += ["--allowedTools", f"mcp__{MCP_NAME}__*"] + CLAUDE_NATIVE_ALLOW
    cmd += ["--output-format", "text", user_text]     # ★ 使用者文字放最後
    status, out, err = _run_blocking(cmd, stop_event, on_tick)
    if status == "stopped":
        return STOP_NOTE, None
    if status == "fuse":
        return FUSE_NOTE, None
    return (out or f"(claude 無輸出)\n{err[-1200:]}"), sid


# ── codex ───────────────────────────────────────────────
def ensure_codex_config():
    """把 MCP server 寫進 ~/.codex/config.toml,並確保 tool_timeout_sec(預設僅 60s!)。"""
    cfg = Path.home() / ".codex" / "config.toml"
    cfg.parent.mkdir(parents=True, exist_ok=True)
    text = cfg.read_text(encoding="utf-8") if cfg.exists() else ""
    header = f"[mcp_servers.{MCP_NAME}]"
    if header in text:
        return
    cfg.write_text(text + (
        f"\n{header}\n"
        f"command = {json.dumps(_PY)}\n"
        f"args = {json.dumps([str(MCP_SERVER)])}\n"
        f"tool_timeout_sec = 1800\nstartup_timeout_sec = 60\n"), encoding="utf-8")


def _codex_parse(stdout_text):
    """--json JSONL → (最終文字, thread_id);無 JSON 行 → 純文字 fallback。
    error / turn.failed 事件(如訂閱額度用罄)轉成可讀訊息,不再默默無輸出。"""
    finals, errors, tid, any_json = [], [], None, False
    for line in (stdout_text or "").splitlines():
        try:
            o = json.loads(line.strip())
        except Exception:
            continue
        any_json = True
        if o.get("type") == "thread.started":
            tid = o.get("thread_id") or tid
        item = o.get("item") or {}
        if (o.get("type") == "item.completed"
                and (item.get("item_type") or item.get("type")) == "agent_message"):
            if item.get("text"):
                finals.append(item["text"])
        if o.get("type") == "error" and o.get("message"):
            errors.append(o["message"])
        if o.get("type") == "turn.failed":
            m = (o.get("error") or {}).get("message")
            if m and m not in errors:
                errors.append(m)
    if not any_json:
        return (stdout_text or "").strip(), None
    text = "\n\n".join(finals).strip()
    if not text and errors:
        text = "⚠ codex 錯誤:" + " / ".join(errors[:2])
    return text, tid


def run_codex(user_text, session=None, stop_event=None, on_tick=None):
    """回 (回覆文字, thread_id)。首輪灌角色+守則進 session;續聊只帶精簡提醒。
    ★ 續聊絕不可塞「完整守則牆」——codex 會回應守則本身、忽略使用者訊息。"""
    ensure_codex_config()
    if session:
        prompt = EVERY_TURN_RULES + USER_MARK + user_text
    else:
        prompt = ("[系統角色設定,請記住並遵守,之後不必覆述]\n" + ROLE_PROMPT
                  + "\n\n" + EVERY_TURN_RULES + USER_MARK + user_text)
    cmd = [_resolve("codex") or "codex", "exec",
           "--dangerously-bypass-approvals-and-sandbox", "--json"]
    cmd += (["resume", session, prompt] if session else [prompt])
    status, out, err = _run_blocking(cmd, stop_event, on_tick)
    if status == "stopped":
        return STOP_NOTE, None
    if status == "fuse":
        return FUSE_NOTE, None
    text, tid = _codex_parse(out)
    return (text or f"(codex 無輸出)\n{err[-1200:]}"), (tid or session)


# ── 對外統一入口(per-chat session 自動管理)─────────────────
def chat(chat_id, user_text, provider=None, stop_event=None, on_tick=None):
    """adapter 唯一要呼叫的函式。回 (回覆文字, session_id)。
    中止/保險絲後自動歸零 session(半途 tool_use 的 session resume 會卡死)。"""
    provider = provider or os.environ.get("AGENT_LLM_PROVIDER", "claude_cli")
    session = get_session(chat_id, provider)
    fn = {"claude_cli": run_claude, "codex_cli": run_codex}[provider]
    reply, new_session = fn(user_text, session=session,
                            stop_event=stop_event, on_tick=on_tick)
    if reply in (STOP_NOTE, FUSE_NOTE):
        new_session = None
    _save_session(chat_id, provider, new_session)
    return reply, new_session
