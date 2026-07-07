# Phase 2 — Identify tool candidates

## Goal

Pick 5–15 functions from the project that should be exposed as agent tools. Wrap them with clean schemas. **The Phase 2 list defines what the agent can do on day 1.**

## What makes a good tool

A function is a good tool candidate when:

- **Single responsibility** — does one specific thing the user wants done
- **Explicit args** — takes typed inputs, not "everything from a config file"
- **Bounded output** — returns something the LLM can reason about (string, JSON, file path), not "side-effect only" black holes
- **Idempotent or near-idempotent** — calling twice shouldn't break things
- **The user actually uses it** — informed by Phase 1 conversation

A function is a **bad** tool candidate when:

- It's a private helper called only inside the codebase
- It has no parameters and reads from globals
- Its output is a complex Python object with no JSON representation
- It's destructive without a clear "are you sure?" path
- The user doesn't actually run it

## Tool schema (Gemini-compatible)

Every tool needs a schema for the LLM to call it. The shape:

```python
{
    "name": "snake_case_name",
    "description": "What it does, in one sentence the LLM can use to decide.",
    "parameters": {
        "type": "object",
        "properties": {
            "arg_name": {
                "type": "string",  # or integer / number / boolean / array
                "description": "What the LLM should pass here, with constraints.",
            },
        },
        "required": ["arg_name"],
    },
}
```

Three things matter most for the description:
1. **Verb-first** ("Read the latest order from the database" beats "Database access function")
2. **Mention return shape** ("Returns dict with keys: customer_id, total, status")
3. **Mention preconditions** if any ("Requires database connection — call set_db_path first")

## ⛔ Hard rule: every tool function MUST accept `**kwargs`

**Pitfall**: 後期 phases(11 web_search rate-limit / 14 memory user isolation / future tracing)會想從 framework 傳「隱性 context」(`_user_id` / `_chat_id` / `_request_id`)給工具。如果 Phase 2 wrap 的工具 signature 沒接 `**kwargs`,日後一加 inject、**所有工具集體 TypeError 崩**。

實戰回報過真實案例:加 per-user memory 後 24 個既有工具全炸,被迫一個一個改加 `**kwargs`。

**規範**:Phase 2 開始,每個 tool function 一律寫成:

```python
def my_tool(arg1: str, arg2: int = 0, **kwargs) -> dict:
    """Tool docstring. **kwargs is intentional — see Phase 2 §kwargs-rule."""
    # ignore kwargs; framework injects per-user / per-chat context here
    ...
    return {"result": ...}
```

**為什麼這條優於 ContextVar 方案**:
- `ContextVar`(phase14 推薦)是「正解、無侵入」 — Phase 14 啟用後沒問題
- 但**早期 phase 2-13 還沒 Phase 14、用戶若手動 inject 也不會炸**
- `**kwargs` 是**雙保險** — 即使有人偏離 ContextVar 用 inject、tool 不死

> 📌 **與 phase14 ContextVar 的關係**:兩者並存、不衝突。ContextVar 是首選機制;`**kwargs` 是 tool signature 的安全網,確保任何 framework 改動都不會炸 tool。

## Wrapping pattern

For each chosen function, write a wrapper in `agent/tools.py`:

```python
def _read_orders(date: str, status: str = "pending", **kwargs) -> dict:
    """Wrapper for project.orders.fetch_orders that returns a dict the LLM can read.

    **kwargs absorbs any framework-injected context (e.g. _user_id, _chat_id).
    """
    rows = fetch_orders(date_filter=date, status=status)
    return {
        "count": len(rows),
        "rows": [r.to_dict() for r in rows[:50]],  # cap output for LLM context
        "date": date,
        "status": status,
    }


READ_ORDERS_TOOL = {
    "name": "read_orders",
    "description": "Fetch orders for a given date filtered by status. Returns dict with count and up to 50 rows.",
    "parameters": {
        "type": "object",
        "properties": {
            "date": {"type": "string", "description": "Date in YYYY-MM-DD format."},
            "status": {"type": "string", "description": "Order status filter, e.g. pending / shipped / cancelled. Defaults to pending."},
        },
        "required": ["date"],
    },
}
```

Two patterns:
- **Cap big output** — if the underlying function can return huge data, slice in the wrapper (top 50 rows, first 1MB of file, summary stats). The LLM has finite context.
- **Coerce errors to dicts** — return `{"error": "...message..."}` instead of raising. The orchestrator pattern (Phase 4) handles error dicts gracefully; raised exceptions kill the loop.

## Asking the user

After drafting the candidate list, present it to the user as:

```
Phase 2 — proposed tools (n = 12):

  Read / query (5)
    1. read_orders(date, status)            ← wraps project.orders.fetch_orders
    2. get_customer(customer_id)            ← wraps project.customers.get
    ...

  Write / mutate (4)
    6. create_invoice(order_id, amount)     ← wraps project.invoices.create
    ...

  Maintenance / housekeeping (3)
    10. archive_old_data(before_date)
    ...
```

Ask them: "Which of these should make the cut? Anything missing? Anything you'd merge or split?"

Make changes based on their answer. Save the final list to `agent/tools_plan.md` before moving on.

## Common mistakes to avoid

- **Wrapping internals**: if a function is private (`_underscore_prefix`), it's almost certainly not a tool candidate.
- **Too many tools**: 30+ tools confuse the LLM. Aim for 8-15 to start; let new ones emerge through Phase 9 (self-evolution).
- **Missing types**: `parameters: {type: object, properties: {x: {type: string}}}` is the minimum. The LLM uses these to build valid arguments.
- **No description on params**: the LLM hallucinates without param descriptions. One sentence each, even if it feels obvious.
