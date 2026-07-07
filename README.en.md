# project-to-agent-plus

> **Plus edition**: Phase 3 now offers two zero-API-key subscription brains — **Claude Code CLI (Pro/Max)** and **OpenAI codex CLI (ChatGPT plan)** — driven headlessly with your project's tools exposed over MCP (exact session resume, companion-wait + tree-kill, codex prompt structure, delivery-contract double insurance; see `references/phase3b-subscription-cli.md`). If you don't pick them, behaviour is 100% identical to the original.
> Original: https://github.com/hoyoboy0726123/claude-skill-project-to-agent

> A Claude Code skill that walks any existing software project through being transformed into a self-evolving conversational agent — wrapped tools, Gemma-4-31B brain, Telegram remote control, folder permissions, optional shell + web search.

[繁體中文](README.md)

## What it does

When you say things like:

- "Turn this Python script into an agent I can chat with on Telegram"
- "I want my CLI tool to be remote-controllable"
- "Make a self-evolving AI assistant that can write its own new tools"

…this skill kicks in and walks Claude (and you) through 9 phases that end with a working Telegram-driven agent on top of your existing project.

## The 9 phases

| # | Phase | What happens |
|---|---|---|
| 1 | **Analyze** | Read your codebase, summarize what it does, get your sign-off |
| 2 | **Tool candidates** | Pick 5–15 functions worth wrapping as agent tools |
| 3 | **LLM setup** | Gemma-4-31B via Google AI Studio (free), `.env`, retry pattern |
| 4 | **Agent core** | Tool registry + Gemini client + orchestrator (planner loop) |
| 5 | **Permissions** | Folder ACL — agent only touches what you allow |
| 6 | **Telegram adapter** | Chat to your project from anywhere; outputs auto-delivered |
| 7 | **Shell tool** *(opt-in)* | Per-call approval; lets agent run code & modify itself |
| 8 | **Tavily web search** *(opt-in)* | Free 1000 searches/month for "look it up" intents |
| 9 | **Self-evolution** *(opt-in)* | Agent proposes new tools; you approve via Telegram |

After each phase you commit to git, so rolling back any step is one command.

The minimum viable agent is **Phases 1–6**. Phases 7–9 unlock self-modification; each is a clear opt-in with explained tradeoffs.

## Why Gemma-4-31B by default?

Free tier on Google AI Studio. Supports both function-calling AND vision. The user gets a real agent without paying anything until they outgrow free tier limits. Switching to a stronger model (`gemini-2.5-flash`, `gemini-3-pro-preview`, etc.) is one string change later.

The skill bundles a `gemini_client.py` with retry logic for transient `500 INTERNAL` errors that are common on Gemma-4 free tier — so the agent works reliably even on a flaky day.

## Eval results

Tested against 3 realistic prompts (Python CLI tool / messy folder of scripts / self-evolving coder). With-skill vs baseline (Claude with no skill access):

| Eval | with-skill | baseline | Δ |
|---|---|---|---|
| python-cli-tool | **10/10** (100%) | 7/10 (70%) | +30 pp |
| vague-folder-of-scripts | **8/9** (89%) | 5/9 (56%) | +33 pp |
| self-evolving-coder | **10/10** (100%) | 3/10 (30%) | **+70 pp** |
| **Average** | **96%** | **52%** | **+44 pp** |

The biggest gap is on the self-evolving case — the baseline produced an agent that could run shell commands without folder permissions or per-call approval (a textbook safety hole). The with-skill version refuses to add shell capability without phases 5/7 in place, with an explicit deny-list (rm -rf, chmod +s, .ssh, force-push…) and three layers of safety.

## Installation

This is a Claude Code skill. Place it in your skills folder:

```bash
# Linux / macOS
git clone https://github.com/hoyoboy0726123/claude-skill-project-to-agent.git \
  ~/.claude/skills/project-to-agent

# Windows (PowerShell)
git clone https://github.com/hoyoboy0726123/claude-skill-project-to-agent.git `
  $env:USERPROFILE\.claude\skills\project-to-agent
```

After install, Claude Code will list it under available skills and trigger it on prompts about agentifying projects.

## Structure

```
project-to-agent/
├── SKILL.md                 # Workflow + phase summaries (always loaded)
├── references/              # Phase deep-dives (loaded as needed)
│   ├── phase1-analyze.md
│   ├── phase2-tools.md
│   ├── phase3-llm.md
│   ├── phase4-core.md
│   ├── phase5-permissions.md
│   ├── phase6-telegram.md
│   ├── phase7-shell.md
│   ├── phase8-tavily.md
│   └── phase9-evolve.md
├── assets/                  # Drop-in templates the user copies into their project
│   ├── agent_template.py    # Tool registry + orchestrator
│   ├── gemini_client.py     # Gemini SDK wrapper (with retry + Gemma vision quirks)
│   ├── telegram_adapter.py  # Telegram bot front-end
│   ├── tools_template.py    # Pattern for wrapping existing functions
│   ├── permissions.json.example
│   ├── .env.example
│   └── requirements.txt
└── evals/
    └── evals.json           # 3 test cases used for benchmark
```

## Key design choices

- **Existing project is the seed.** Phases 1–2 wrap your existing code; nothing is rebuilt from scratch.
- **Permissions are explicit, never assumed.** Skill bakes in folder ACL with read/write/delete and forbids shell access without it.
- **Tools coerce errors to dicts** (`{"error": "..."}`), never raise — this keeps the orchestrator loop alive when a tool fails.
- **Output files travel automatically.** Tools that produce files return JSON with an `output_file` / `saved_path` / `path` key — the Telegram adapter scans for these and delivers files as documents/photos.
- **Self-evolution is incremental.** New tool drafts go to `agent/tools_proposed/` and the user approves via Telegram inline button before they become live.

## Contributing

PRs welcome — especially:
- Additional reference files for non-Python stacks (Node.js / Go / Rust)
- Asset templates for other LLM providers (OpenAI / Mistral / local Ollama)
- More eval test cases (the current 3 cover only common patterns)

## License

MIT (see [LICENSE](LICENSE))

## Built with

- [Claude Code](https://claude.com/claude-code) skill-creator
- [Anthropic Claude Opus 4.7 (1M context)](https://www.anthropic.com)
