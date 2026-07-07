# Phase 1 — Analyze the project

## Goal

Understand what the project does well enough to wrap its functions as agent tools. **Don't move on until the user confirms your understanding.**

## How to read the project

Start with the obvious:

1. **`README.md` / `README.rst`** — the user's stated story of the project
2. **`pyproject.toml` / `package.json` / `requirements.txt`** — language + dependency hints
3. **Entry points** — `main.py`, `app.py`, `index.js`, `cli.py`, anything with `if __name__ == "__main__"`, `bin/` scripts, `scripts:` in package.json
4. **Top-level packages / src layout** — what modules exist?

For each entry point, trace the call graph 1-2 levels deep. You don't need full understanding of every helper — you need to know *what the user can actually do with this project today*.

## What to extract

Produce a one-paragraph summary covering:

- **Language & stack** (Python 3.11 / Node 20 / Go / etc.)
- **What the project does** (in user's words, not function names)
- **How users invoke it today** (CLI flag? web UI? import as library? scheduled job?)
- **Inputs/outputs** (reads from where → writes to where)
- **External services it touches** (databases, APIs, filesystem patterns)

Plus a quick list of:

- **Existing CLI commands / API endpoints / public functions** (you'll narrow this list in Phase 2)
- **Anything stateful** (config files, DB connections, in-memory caches that span calls)
- **Anything destructive** (deletes, overwrites, sends notifications, charges money)

## Confirm with the user

Show your summary back to the user as a single paragraph + bullet lists. Ask them:

- "Does this match what you do with this project?"
- "Anything I missed that you actually use day-to-day?"
- "Anything I called out that's actually unused / experimental?"

The user knows things you can't see from code (which CLIs they actually run, which features they wish existed, which are dead). **Their corrections are gold for Phase 2.**

## Edge cases

- **Mostly-data project** (data dir + a few scripts): the agent will mostly be a CLI runner + filesystem tool. That's fine — Phase 2 will be small but Phase 5 (permissions) becomes the most important piece.
- **Web app / server**: you'll likely add tools that hit the app's HTTP endpoints (start the server first, then call). Don't try to wrap every internal function — wrap the public API.
- **Library only (no entry point)**: the user wants to use the library as the tool surface. Pick the most-imported public functions. Ask the user which they actually use.
- **Project in a language you don't know**: ask the user to walk you through the entry points. Their plain-language description is more valuable than your guess.

## Output

Save your analysis to `agent/PROJECT_ANALYSIS.md` once the user signs off. This becomes the source of truth for Phase 2.
