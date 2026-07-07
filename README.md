# project-to-agent-plus

> **Plus 版新增**:Phase 3 可改選 **Claude Code CLI(Pro/Max 訂閱)** 或 **OpenAI codex CLI(ChatGPT 訂閱)** 當大腦 —— **免 API Key**,工具經 MCP server 曝露給官方 CLI(session 精準續聊、陪跑等待+樹殺、codex 提示結構、交付契約雙保險,全套實戰方法見 `references/phase3b-subscription-cli.md`)。沒選訂閱選項時,行為與原版 100% 相同。
> 原版:https://github.com/hoyoboy0726123/claude-skill-project-to-agent


> 一個 Claude Code skill — 把任何現有的軟體專案,引導你走完 12 個階段,變成一個**只透過 Telegram 操作**的對話式 agent(多 LLM provider + 資料夾權限 + two-step write 協議 + hallucination 偵測 + 可選 shell 沙盒 + Tavily 搜尋 + self-evolution)。

[English version](README.en.md)

## 它做什麼

當你說這類話的時候它會自動觸發:

- 「把這個 Python 腳本變成可以 Telegram 對話的 agent」
- 「我想讓這個 CLI 工具可以遠端用」
- 「做一個會自己寫新工具的 AI 助理」

Skill 帶 Claude(跟你)走完 12 個階段、最後得到一個架在你**現有專案**之上、用 Telegram 操作的 agent。

## 12 個階段

| # | 階段 | 做什麼 |
|---|---|---|
| 1 | **分析** | 讀你的程式、寫一段摘要、跟你確認沒誤解 |
| 2 | **工具候選** | 從專案挑 5–15 個值得包成 tool 的 function |
| 3 | **LLM 設定** | Multi-provider:Gemini(預設、免費)/ Groq / OpenAI / Anthropic / Ollama |
| 3b | **訂閱制 CLI 大腦** *(選用)* | Claude Code / codex 官方 CLI 當大腦,免 API Key(工具走 MCP) |
| 4 | **Agent 核心** | tool registry + orchestrator(planner loop)+ per-chat state |
| 5 | **Two-step write 協議** | 寫操作必預覽 → 確認 → 才執行,防 LLM 一次性寫錯 |
| 6 | **Hallucination 偵測** | LLM 宣稱「已執行」但實際沒呼工具的自動偵測 + 警告 |
| 7 | **權限邊界** | 資料夾 ACL — agent 只能碰你允許的目錄 |
| 8 | **Telegram 介面** | per-chat 隔離、polling lock、4000 字 chunking、tool progress 推送 |
| 9 | **Channel-specific 系統 prompt** | TG 通道規範 + 動態注入(今日日期、工具清單)|
| 10 | **Shell 工具** *(選用)* | Host 模式 / Sandbox 模式二選一(沙盒走自家 .bat 裝 Docker Engine、**不用 Docker Desktop**) |
| 11 | **Tavily 網路搜尋** *(選用)* | 每月免費 1000 次 + 1h cache + per-user rate limit |
| 12 | **自我進化迴圈** *(選用)* | Agent 提案新工具,你在 Telegram 按按鈕同意,evals 通過才合併 |

每跑完一個階段就 commit 到 git,任何階段出錯都能一鍵還原。

最小可用版本 = **階段 1-8**(分析 → 工具 → LLM → core → two-step → hallucination → permissions → TG);9-12 解鎖進階能力,每個都是清楚的 opt-in moment。

## 為什麼預設用 Gemini / Gemma

Google AI Studio **免費**支援 function-calling 跟 vision、不用信用卡。預設 model `gemma-4-31b-it`,quota 不夠再切到付費的 `gemini-2.5-flash` 或別家 provider — 只改 `.env` 一個變數,code 不動(`llm_client.py` 是 multi-provider factory)。

## 為什麼沙盒不用 Docker Desktop

Skill 附自己的 `setup_sandbox.bat` + `setup.sh`,在 WSL2 內透過 docker.com 官方 install script 裝 Docker Engine。**Docker Engine 開源、商業免費**;Docker Desktop 大公司用要付費,skill 預設避開那條路徑、讓使用者下游做任何用途都不踩授權雷。

## 結構

```
project-to-agent/
├── SKILL.md                            # 主流程 + 12 階段摘要(永遠在 context)
├── references/
│   ├── phase1-analyze.md
│   ├── phase2-tools.md
│   ├── phase3-llm.md                   # multi-provider LLM setup
│   ├── phase4-core.md                  # planner loop + per-chat orchestrator
│   ├── phase5-two-step-write.md        # 寫操作協議
│   ├── phase6-hallucination-detection.md
│   ├── phase7-permissions.md
│   ├── phase8-telegram.md              # per-chat / chunking / polling lock / progress
│   ├── phase9-channel-prompts.md       # channel marker + 動態注入
│   ├── phase10-shell.md                # shell + host/sandbox 選擇 + .bat 引導
│   ├── phase11-tavily.md               # search + cache + rate limit
│   └── phase12-evolve.md               # self-evolution + evals harness
├── assets/
│   ├── llm_client.py                   # multi-provider LLM factory
│   ├── telegram_adapter.py             # 強化 TG adapter
│   ├── agent_template.py
│   ├── tools_template.py
│   ├── permissions.json.example
│   ├── .env.example                    # 5 個 provider key + TG + Tavily 欄位
│   ├── requirements.txt
│   └── sandbox/
│       ├── setup_sandbox.bat           # Windows 入口
│       ├── setup.sh                    # WSL 安裝(裝 Docker Engine,不用 Docker Desktop)
│       └── Dockerfile                  # 通用最小 Python 容器
└── evals/
    └── evals.json                      # smoke test:新工具 merge 前必跑
```

## 設計哲學

- **以現有專案為起點** — 階段 1-2 包裝你現有的 code、不重寫
- **TG 是唯一前端** — 沒 REPL / 沒桌面 chat、所有 UX 投資集中在 TG adapter
- **權限永遠 explicit** — 資料夾 ACL、shell access、self-modify 都是 opt-in、預設都不開
- **Tool 把錯誤包成 dict**(`{"error": "..."}`)而非拋例外 — orchestrator loop 不會因為一個 tool 失敗就死掉
- **輸出檔自動送達** — Tool 產生檔案的時候 return 的 dict 含 `output_file` / `saved_path` / `path` 等 key,Telegram adapter 自動掃並當 document/photo 傳回 chat
- **自我進化是漸進的** — 新 tool 草稿先放 `agent/tools_proposed/`,evals 跑過 + 使用者在 TG 按下「同意」按鈕才正式合併到 `tools/`

## 貢獻

歡迎 PR,特別需要:
- 非 Python stack 的 reference(Node.js / Go / Rust)
- 更多 LLM provider 的 client class(Cohere / DeepSeek 等)
- 更多 eval 測試 case

## 授權

MIT — 看 [LICENSE](LICENSE)
