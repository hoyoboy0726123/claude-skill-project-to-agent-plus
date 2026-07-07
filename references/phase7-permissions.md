# Phase 7 — Permission boundaries

## ⛔ HARD RULE — DO NOT SKIP THIS PHASE'S USER ASK

When wiring Phase 7, you **MUST** explicitly ask the user which folders get `read`, `write`, and `delete` access. Writing `agent/permissions.json.example` and moving on is **NOT enough** — the example never gets copied to the real file, and even when it does the user has no chance to think about what's actually correct for their machine.

This is the single most-common failure mode of agents built with this skill: silent permission drift where `permissions.json` either doesn't exist (agent crashes), or contains a placeholder pointing at a fake folder (agent silently can't write anything). Both are bad UX.

**Required when entering this phase**:
1. From Phase 1 analysis, enumerate the project's filesystem touchpoints (output dirs, config dirs, vault paths, log dirs, etc.).
2. Call `AskUserQuestion` to confirm each folder's intended `read` / `write` / `delete` status. Use the concrete example below.
3. Write the real `agent/permissions.json` (NOT just `.example`).
4. Show the user the final JSON for sanity-check before moving on.

Failing to do this is a Phase 7 failure even if the rest of the code compiles. The harness `permissions.json.example` only exists as a fallback documentation artifact — never as a substitute for the ask.

## Why permissions matter

The agent reads and writes files. Without explicit boundaries, a single bad tool call (or worse, a malicious prompt) could read `~/.ssh/id_rsa` or wipe `~/Documents`. **Always wire permissions before any filesystem-touching tool, definitely before Phase 10 (shell).**

## The model

Three permission levels per folder:

- `read` — the agent can list / open files inside
- `write` — the agent can create / overwrite files (no delete)
- `delete` — the agent can remove files

Plus an **uncondtional deny-list** (hard-coded in `permissions.py`, not user-configurable):
- `.env`, `*.key`, `*.pem`, `id_rsa*`, `*credentials*`, `*secret*`, `*token*`

These are blocked **regardless of allowlist** — protects against typos / overly broad globs that accidentally include `.env` in a "write everything" rule.

## Concrete AskUserQuestion example

When you reach Phase 7, before writing any code, call this:

```python
AskUserQuestion(questions=[
    {
        "question": "你的專案實際路徑(我要寫進 permissions.json read/write 陣列)?",
        "header": "專案路徑",
        "multiSelect": False,
        "options": [
            {"label": "<the path you inferred from Phase 1>", "description": "..."},
            {"label": "我等一下手動貼絕對路徑", "description": "..."},
        ],
    },
    {
        "question": "agent 寫操作要鎖在哪些子目錄?(write_file / write_note 只能動這幾個)",
        "header": "Write 範圍",
        "multiSelect": True,
        "options": [
            {"label": "outputs/ 子目錄", "description": "推薦,所有產出物的 sandbox"},
            {"label": "整個專案", "description": "self-evolution 允許 LLM 改 code"},
            {"label": "額外指定的 vault / data folder", "description": "..."},
        ],
    },
    {
        "question": "delete 權限要不要開?",
        "header": "Delete",
        "multiSelect": False,
        "options": [
            {"label": "不要 — 一律不准刪", "description": "最保守、推薦"},
            {"label": "只能刪 agent/tools_proposed/", "description": "Phase 12 self-evolution 要"},
            {"label": "outputs/ 內可刪", "description": "需要清理舊產出"},
        ],
    },
])
```

Use the user's answers to write `agent/permissions.json` **with real absolute paths**. Show them the final file. **Do not write `.example` and call it done.**

## permissions.json structure

```json
{
  "read": [
    "C:/Users/me/projects/myapp/data",
    "C:/Users/me/projects/myapp/templates"
  ],
  "write": [
    "C:/Users/me/projects/myapp/output"
  ],
  "delete": []
}
```

Notes:
- Tail `/**` is optional — a folder path matches it + all descendants by default.
- Use forward slashes even on Windows (cross-platform safe).
- `permissions.json` MUST be in `.gitignore` (real paths are user-specific).

## The permission check

Every filesystem tool calls this before acting:

```python
# agent/permissions.py
class Permissions:
    def check(self, path: str | Path, op: Literal["read", "write", "delete"]) -> None:
        s = str(Path(path).resolve()).replace("\\", "/")
        # 1. Hard deny-list — blocks .env / *.key / *secret* unconditionally
        if _is_denied_file(s):
            raise PermissionDenied(f"deny list hit: {s}")
        # 2. Allowlist check
        for allowed in self._data.get(op, []):
            if _path_matches(s, allowed):
                return
        # 3. write/delete also need parent's `write` granted
        raise PermissionDenied(f"no '{op}' permission for {s}")
```

Wrap it in tools:

```python
def write_report(filename: str, content: str) -> dict:
    target = OUTPUT_DIR / filename
    try:
        permissions.check(target, "write")
    except PermissionDenied as e:
        return {"error": str(e)}
    target.write_text(content, encoding="utf-8")
    return {"ok": True, "path": str(target)}
```

## Surface permission errors clearly

When the agent hits a permission error, it should tell the user *why*, not just "couldn't do it". The orchestrator's tool result handling already passes `{"error": ...}` back to the LLM, which then explains it in chat. That's enough.

## Re-prompting for permission at runtime

When the agent wants to do something a current permission set doesn't allow, **the agent should ask the user for permission via Telegram** (don't auto-grant, don't fail silently):

```python
def _request_permission(self, path: str, op: str):
    """Ask user via Telegram to add a permission. Returns True if granted."""
    # send Telegram message with inline buttons:
    #   [Grant {op} on {path}]  [Deny]
    # block or short-poll for response, then update permissions.json on grant
    ...
```

This makes the agent feel collaborative — it's asking for capability, not silently failing.

## Anti-patterns

- ❌ **Writing `permissions.json.example` and skipping the user ask.** This is the #1 reason agents built with this skill ship broken. The example file is documentation, not config.
- ❌ A single `"all": ["~"]` permission. Defeats the point.
- ❌ Reading the user's home dir or git history. Even read-only this leaks credentials, browser cookies, etc.
- ❌ Symlink traversal. Resolve paths to canonical form (`Path.resolve()`) before checking — otherwise a symlink inside an allowed folder could escape.
- ❌ Storing permissions in code. `permissions.json` (gitignored) is the source of truth — the user can edit it without a code change.

## Checklist before moving on

- [ ] **AskUserQuestion was called and answered** (re-read the conversation — if you didn't, go back and ask now).
- [ ] `agent/permissions.json` (the REAL file, not `.example`) exists with absolute paths the user explicitly approved.
- [ ] `.gitignore` includes `agent/permissions.json`.
- [ ] Hard deny-list in `permissions.py` blocks `.env` / `*.key` / `*secret*` regardless of allowlist.
- [ ] Tools that touch the filesystem all call `permissions.check()` before acting.
- [ ] Tried writing to a non-allowlisted path and confirmed the agent refuses cleanly with a readable error message.
