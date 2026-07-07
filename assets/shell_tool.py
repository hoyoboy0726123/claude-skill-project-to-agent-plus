"""Shell + Python execution tools — Phase 10.

Two execution modes:
  - host:    subprocess.run on the user's OS (fast, no isolation, allowlist-only)
  - sandbox: `wsl docker exec agent-sandbox bash -c '<cmd>'` — full freedom
             except for hard deny-list, because the container is the boundary

Tools exposed:
  - run_shell (was: shell) — execute a shell command
  - run_python — execute Python code via `python -c '...'`

Sandbox policy: in sandbox mode, allowlist is bypassed (any command runs),
because the container IS the isolation. Only the hard deny-list still applies.
Bind-mount writes still hit host paths though, so permissions.check(cwd, ...)
still guards the working directory.
"""
from __future__ import annotations

import os
import re
import subprocess
from typing import Callable

from agent.permissions import PermissionDenied, Permissions

# ── Hard deny-list — always blocked, no override ────────────────
DENY_PATTERNS = [
    r"\brm\s+-rf\s+/",       # rm -rf / (and variants)
    r"\bsudo\b",             # never elevate
    r"\bchmod\s+\+s",        # setuid
    r"\.ssh\b",              # touching ~/.ssh
    r"\bcrontab\s+-",        # editing cron
    r"\bdocker\s+(stop|rm|kill)\s+agent-sandbox\b",  # don't kill ourselves
    r"\bdocker\s+exec\b",    # nested docker exec — keep it simple
    r"\bmkfs\b", r"\bformat\b",  # don't format anything
    r":\(\)\{",              # fork bomb literals
    r"\bgit\s+push\s+(--force|-f|origin\s+main|origin\s+master)\b",  # protect main
]

# ── Read-only allowlist — auto-execute, no approval needed ──────
_AUTO_ALLOW_PREFIXES = (
    # Filesystem inspection
    "ls", "ls ", "pwd", "stat ", "file ", "du ", "df ",
    "cat ", "head ", "tail ", "less ", "wc ", "find ",
    "grep ", "rg ", "tree ",
    # Git inspection (read-only)
    "git status", "git log", "git diff", "git branch",
    "git show", "git remote", "git config --get",
    # Language version checks
    "python --version", "python3 --version", "pip list", "pip show ",
    "node --version", "npm --version",
    # Container introspection (sandbox-side, read-only)
    "echo ", "env", "which ", "whereis ", "uname",
    "date", "id", "whoami", "hostname",
)


def _is_auto_allowed(command: str) -> bool:
    stripped = command.strip()
    for pat in _AUTO_ALLOW_PREFIXES:
        if stripped == pat.strip() or stripped.startswith(pat):
            return True
    return False


def _hits_deny(command: str) -> str | None:
    for pat in DENY_PATTERNS:
        m = re.search(pat, command)
        if m:
            return m.group(0)
    return None


# ─────────────────────────────────────────────────────────────
# Shell tools
# ─────────────────────────────────────────────────────────────
class HostShellTool:
    """Runs the command directly on the host via subprocess.

    HOST mode is allowlist-only: read-only inspection commands auto-run, anything
    else is refused (until per-chat approval lands). Set AGENT_SANDBOX_MODE=sandbox
    if you want LLM to run arbitrary commands — the container is the boundary then.
    """

    mode = "host"

    def __init__(self, permissions: Permissions, approval_cb: Callable[[str], bool] | None = None):
        self.permissions = permissions
        self.approval_cb = approval_cb

    def _guard(self, command: str, cwd: str) -> dict | None:
        hit = _hits_deny(command)
        if hit:
            return {"error": f"blocked by deny-list pattern: {hit}", "command": command}
        try:
            self.permissions.check(cwd, "read")
        except PermissionDenied as e:
            return {"error": f"cwd not in permissions allowlist: {e}", "cwd": cwd}
        return None

    def run(self, command: str, cwd: str | None = None, timeout: int = 120) -> dict:
        cwd = cwd or os.getcwd()
        denied = self._guard(command, cwd)
        if denied:
            return denied

        # Host mode: strict allowlist (no container to fall back on)
        if not _is_auto_allowed(command):
            if self.approval_cb is None:
                return {
                    "error": "host-mode shell refuses non-allowlist commands",
                    "command": command,
                    "hint": "Set AGENT_SANDBOX_MODE=sandbox in .env to let LLM run "
                            "arbitrary commands inside the container.",
                }
            if not self.approval_cb(command):
                return {"error": "user denied", "command": command}

        try:
            r = subprocess.run(
                command, shell=True, cwd=cwd,
                capture_output=True, text=True, timeout=timeout,
            )
            return {
                "ok": r.returncode == 0,
                "exit_code": r.returncode,
                "stdout": (r.stdout or "")[-4000:],
                "stderr": (r.stderr or "")[-2000:],
                "command": command,
                "cwd": cwd,
                "mode": self.mode,
            }
        except subprocess.TimeoutExpired:
            return {"error": f"timeout (>{timeout}s)", "command": command, "mode": self.mode}
        except Exception as e:
            return {"error": str(e), "command": command, "mode": self.mode}


class SandboxShellTool(HostShellTool):
    """Runs commands inside the `agent-sandbox` Docker container via WSL.

    Sandbox mode bypasses the auto-allowlist — the container IS the boundary, so
    the LLM can run arbitrary write commands inside. Hard deny-list still applies
    (rm -rf /, sudo, killing the container itself, force-pushing to main, etc.).
    The cwd permissions.check still runs because bind-mount writes hit host paths.
    """

    mode = "sandbox"

    def __init__(self, permissions: Permissions,
                 approval_cb: Callable[[str], bool] | None = None,
                 container: str = "agent-sandbox"):
        super().__init__(permissions, approval_cb)
        self.container = container

    def run(self, command: str, cwd: str | None = None, timeout: int = 120) -> dict:
        cwd = cwd or os.getcwd()
        denied = self._guard(command, cwd)
        if denied:
            return denied

        # Sandbox: allowlist BYPASSED — container is the boundary
        wsl_cwd = _to_wsl_path(cwd)
        escaped = command.replace("'", "'\"'\"'")
        wsl_cmd = (
            f"docker exec -w {_shquote(wsl_cwd)} {self.container} "
            f"bash -c '{escaped}'"
        )

        try:
            r = subprocess.run(
                ["wsl", "-e", "bash", "-c", wsl_cmd],
                capture_output=True, text=True, timeout=timeout,
            )
            return {
                "ok": r.returncode == 0,
                "exit_code": r.returncode,
                "stdout": (r.stdout or "")[-4000:],
                "stderr": (r.stderr or "")[-2000:],
                "command": command,
                "cwd": cwd,
                "wsl_cwd": wsl_cwd,
                "mode": self.mode,
                "container": self.container,
            }
        except subprocess.TimeoutExpired:
            return {"error": f"timeout (>{timeout}s)", "command": command, "mode": self.mode}
        except Exception as e:
            return {"error": str(e), "command": command, "mode": self.mode}

    def run_python(self, code: str, cwd: str | None = None, timeout: int = 120) -> dict:
        """Run arbitrary Python in the container via `python -c`. Sandbox-only API.

        For longer scripts, write a file first (write_file) then run via run_shell.
        This is for one-liners and quick experiments.
        """
        cwd = cwd or os.getcwd()
        try:
            self.permissions.check(cwd, "read")
        except PermissionDenied as e:
            return {"error": f"cwd not in permissions allowlist: {e}", "cwd": cwd}

        # Python source passed via heredoc on container stdin — sidesteps all
        # the nested-shell quoting nightmare. Container runs `python -` reading
        # stdin.
        wsl_cwd = _to_wsl_path(cwd)
        try:
            r = subprocess.run(
                ["wsl", "-e", "bash", "-c",
                 f"docker exec -i -w {_shquote(wsl_cwd)} {self.container} python -"],
                input=code,
                capture_output=True, text=True, timeout=timeout,
            )
            return {
                "ok": r.returncode == 0,
                "exit_code": r.returncode,
                "stdout": (r.stdout or "")[-4000:],
                "stderr": (r.stderr or "")[-2000:],
                "cwd": cwd,
                "wsl_cwd": wsl_cwd,
                "mode": self.mode,
                "container": self.container,
                "lines_of_code": code.count("\n") + 1,
            }
        except subprocess.TimeoutExpired:
            return {"error": f"timeout (>{timeout}s)", "mode": self.mode}
        except Exception as e:
            return {"error": str(e), "mode": self.mode}


# ─────────────────────────────────────────────────────────────
# Helpers
# ─────────────────────────────────────────────────────────────
def _to_wsl_path(win_path: str) -> str:
    """C:\\Users\\foo -> /mnt/c/Users/foo. Used to map host paths into the container."""
    p = (win_path or "").replace("\\", "/")
    m = re.match(r"^([A-Za-z]):/(.*)$", p)
    if m:
        return f"/mnt/{m.group(1).lower()}/{m.group(2)}"
    return p


def _shquote(s: str) -> str:
    """Minimal POSIX shell quoting."""
    if not s:
        return "''"
    if re.match(r"^[A-Za-z0-9_/.@:%+,=-]+$", s):
        return s
    return "'" + s.replace("'", "'\"'\"'") + "'"


# ─────────────────────────────────────────────────────────────
# Tool schema (Phase 4 register_all hooks into this)
# ─────────────────────────────────────────────────────────────
def run_shell_schema() -> dict:
    return {
        "type": "object",
        "properties": {
            "command": {
                "type": "string",
                "description": (
                    "Shell command to execute. In SANDBOX mode (recommended) any "
                    "command runs — the container is the boundary. In HOST mode "
                    "only the read-only allowlist runs (ls, cat, git status/log, "
                    "etc.). Hard-blocked in either mode: rm -rf /, sudo, chmod +s, "
                    ".ssh, killing the container, force-push to main."
                ),
            },
            "cwd": {
                "type": "string",
                "description": (
                    "Working directory (absolute path). Must be inside "
                    "permissions.json `read` allowlist. Defaults to project root."
                ),
            },
            "timeout": {
                "type": "integer",
                "description": "Seconds before SIGKILL (default 120, max 600).",
            },
        },
        "required": ["command"],
    }


def run_python_schema() -> dict:
    return {
        "type": "object",
        "properties": {
            "code": {
                "type": "string",
                "description": (
                    "Python source code. Runs via `python -` (heredoc-style stdin) "
                    "inside the sandbox container. Use print() for output. For "
                    "scripts longer than ~50 lines, write_file first then run via "
                    "run_shell('python /path/to/script.py'). SANDBOX MODE ONLY."
                ),
            },
            "cwd": {
                "type": "string",
                "description": "Working directory inside the container.",
            },
            "timeout": {"type": "integer", "description": "Seconds (default 120)."},
        },
        "required": ["code"],
    }


def make_shell_tool(permissions: Permissions, mode: str | None = None,
                    approval_cb=None):
    """Factory. Returns HostShellTool or SandboxShellTool based on AGENT_SANDBOX_MODE.

    If mode='sandbox' explicitly and pre-flight fails → raise RuntimeError
    (NOT silent fallback to host — that would erase the user's safety intent).
    The caller can catch and decide whether to disable shell tool entirely.
    """
    mode = (mode or os.getenv("AGENT_SANDBOX_MODE", "host")).lower().strip()
    if mode == "sandbox":
        from agent.sandbox_preflight import preflight_sandbox
        res = preflight_sandbox()
        if not res.ok:
            failures = "\n  - ".join(res.failures) if hasattr(res, "failures") and res.failures else "(see preflight result)"
            raise RuntimeError(
                "Sandbox pre-flight FAILED — refusing to start.\n"
                f"  - {failures}\n"
                "Fix: run sandbox\\setup_sandbox.bat, or explicitly set "
                "AGENT_SANDBOX_MODE=host in .env if you accept host-mode risk."
            )
        return SandboxShellTool(permissions, approval_cb)
    return HostShellTool(permissions, approval_cb)
