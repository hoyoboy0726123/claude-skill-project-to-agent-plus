# -*- coding: utf-8 -*-
"""mcp_server — 把既有 ToolRegistry 的工具曝露給 Claude Code / codex CLI(MCP stdio)。

Phase 3b 資產範本。`pip install "mcp[cli]"` 後即可用。
MCP_NAME 要跟 cli_brain.py 的 MCP_NAME 一致(它決定工具前綴 mcp__<name>__*)。

★ 最重要的坑:工具函式若被裝飾器包過,包裝層必須 @functools.wraps(fn),
  否則 FastMCP 從 inspect.signature 推 schema 會變成一個 `kw` 參數,
  所有工具在兩家 CLI 全部呼叫失敗。
"""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))  # 專案根

from mcp.server.fastmcp import FastMCP          # noqa: E402

MCP_NAME = "myagent"                             # ← 跟 cli_brain.MCP_NAME 一致
mcp = FastMCP(MCP_NAME)


def _mcp_safe(fn):
    """剝掉 **kwargs / *args 再交給 FastMCP。

    skill 的 kwargs-rule 要求每個工具接 **kwargs(框架安全網),但 FastMCP 從
    inspect.signature 推 schema 會把 `kwargs` 當成一個真參數曝露給 LLM → 雜訊
    + 可能被亂塞。這裡覆寫 __signature__ 只留具名參數。"""
    import functools
    import inspect
    sig = inspect.signature(fn)
    params = [p for p in sig.parameters.values()
              if p.kind not in (inspect.Parameter.VAR_KEYWORD,
                                inspect.Parameter.VAR_POSITIONAL)]

    @functools.wraps(fn)
    def wrapper(*a, **kw):
        return fn(*a, **kw)
    wrapper.__signature__ = sig.replace(parameters=params)
    return wrapper


def _register_all():
    """把 agent 既有的工具全部掛上 MCP(跟 API-key 路線共用同一套實作)。"""
    from agent.tool_registry import ToolRegistry
    from agent import tools as tools_pkg

    reg = ToolRegistry()
    tools_pkg.register_all(reg)
    for t in reg.all():
        # t.func 的 signature/docstring 就是 CLI 看到的工具說明 —— 寫清楚
        mcp.tool()(_mcp_safe(t.func))


if __name__ == "__main__":
    # 可選:載入 .env(工具若需要 key,例如某些 API 型工具)
    try:
        from dotenv import load_dotenv
        load_dotenv(Path(__file__).resolve().parent.parent / ".env")
    except Exception:
        pass
    _register_all()
    mcp.run()                                    # stdio
