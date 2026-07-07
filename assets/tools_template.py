"""Tool registration pattern. Drop into your project as agent/tools.py and edit.

Each "wrapped" function is the bridge between the project's existing code and the
agent. Keep wrappers thin: they translate args, cap output, coerce errors to dicts.
"""

import os
from agent.tool_registry import Tool, ToolRegistry


# ============================================================
# Example tool 1 — wraps an existing project function
# ============================================================
# from project.orders import fetch_orders  # your project's actual function

def _read_orders(date: str, status: str = "pending") -> dict:
    """Wrapper around fetch_orders. Caps output to 50 rows for LLM context."""
    try:
        # rows = fetch_orders(date_filter=date, status=status)
        rows = []  # PLACEHOLDER — replace with actual call
    except Exception as e:
        return {"error": str(e)}
    return {
        "date": date,
        "status": status,
        "count": len(rows),
        "rows": [r.to_dict() if hasattr(r, "to_dict") else r for r in rows[:50]],
    }


READ_ORDERS = Tool(
    name="read_orders",
    description=(
        "Fetch orders for a given date filtered by status. "
        "Returns dict with date, status, count, and up to 50 rows."
    ),
    parameters={
        "type": "object",
        "properties": {
            "date": {"type": "string", "description": "Date in YYYY-MM-DD format."},
            "status": {"type": "string", "description": "Order status: pending / shipped / cancelled. Default pending."},
        },
        "required": ["date"],
    },
    func=_read_orders,
)


# ============================================================
# Example tool 2 — file-touching, uses Phase 5 permissions
# ============================================================
# from agent.permissions import Permissions, PermissionDenied
# permissions = Permissions("agent/permissions.json")

def _write_report(filename: str, content: str) -> dict:
    """Save a report to the configured output dir, after checking write permission."""
    out_dir = os.environ.get("OUTPUT_DIR", "./output")
    target = os.path.join(out_dir, filename)
    # try:
    #     permissions.check(target, "write")
    # except PermissionDenied as e:
    #     return {"error": str(e)}
    os.makedirs(out_dir, exist_ok=True)
    with open(target, "w", encoding="utf-8") as f:
        f.write(content)
    return {"ok": True, "output_file": target, "size_bytes": len(content)}


WRITE_REPORT = Tool(
    name="write_report",
    description=(
        "Write a text report to the output folder. "
        "Returns {ok, output_file, size_bytes}. The output_file path will be "
        "auto-delivered to Telegram."
    ),
    parameters={
        "type": "object",
        "properties": {
            "filename": {"type": "string", "description": "Filename (no path), e.g. 'q3_summary.md'"},
            "content": {"type": "string", "description": "Full text content of the report."},
        },
        "required": ["filename", "content"],
    },
    func=_write_report,
)


# ============================================================
# Registration entry point
# ============================================================

def register_all(registry: ToolRegistry):
    """Called once at startup to register every tool."""
    registry.register(READ_ORDERS)
    registry.register(WRITE_REPORT)

    # Optional shell tool (Phase 7) — only if user opted in
    if os.environ.get("ENABLE_SHELL_TOOL", "").lower() in ("1", "true", "yes"):
        from agent.shell_tool import shell_tool, shell_schema
        registry.register(Tool(
            name=shell_schema()["name"],
            description=shell_schema()["description"],
            parameters=shell_schema()["parameters"],
            func=shell_tool,
        ))

    # Optional Tavily web search (Phase 8) — only if key set
    if os.environ.get("TAVILY_API_KEY"):
        from agent.tavily_tool import web_search, web_search_schema
        registry.register(Tool(
            name=web_search_schema()["name"],
            description=web_search_schema()["description"],
            parameters=web_search_schema()["parameters"],
            func=web_search,
        ))
