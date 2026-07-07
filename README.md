# project-to-agent-plus

> 一個 Claude Code skill — 把任何現有的軟體專案,引導你走完 15 個階段,變成一個**透過 Telegram / 本機 Web 操作**的對話式 agent(多 LLM provider + 資料夾權限 + two-step write 協議 + hallucination 偵測 + 可選 shell 沙盒 + Tavily 搜尋 + self-evolution + 記憶系統)。
>
> **Plus 版新增(相對[原版](https://github.com/hoyoboy0726123/claude-skill-project-to-agent)):Phase 3 多兩個「訂閱制大腦」選項 —— 免 API Key。**

[English version](README.en.md)

## ✨ Plus 版新增了什麼

Phase 3 選 LLM 時,除了原有 5 家 API provider,多出兩個**零 API Key** 選項:

| 選項 | 需要 | 特點 |
|---|---|---|
| **Claude Code CLI(訂閱)** | 已裝 `claude` 並登入 Pro/Max | 安全邊界最嚴(工具是唯一通道、資料夾 ACL 全效),推薦預設 |
| **OpenAI codex CLI(訂閱)** | `npm i -g @openai/codex` + `codex login` | 附原生 shell 與 image_gen 生圖(走 ChatGPT 訂閱額度) |

原理:你的專案工具經 **MCP server** 曝露給官方 CLI,推理迴圈由 CLI 自己跑,吃你已付的訂閱額度。核心件全部給現成範本:

- `references/phase3b-subscription-cli.md` — 完整接法 + 兩家 CLI 全部參數地雷(實戰驗證)+ 全 15 phase 相容性對照表
- `assets/cli_brain.py` — drop-in 驅動器:per-chat session 精準續聊(`--session-id/--resume`、codex `thread_id`)、陪跑等待+跨平台樹殺、額度用罄等錯誤浮現
- `assets/mcp_server.py` — 工具曝露範本(自動剝 `**kwargs`,schema 乾淨)
- `docs/訂閱路線差異說明.md` — 七條取捨 + 決策建議(選擇前必讀)

**知情同意門**:選了訂閱選項,skill 會先亮出全部取捨(幻覺偵測替代、codex 原生 shell 不受 ACL 約束、沙盒限制、額度上限…)再讓你確認;**不選訂閱 → 行為與原版 100% 相同**。

## 📦 安裝這個 skill

```bat
git clone https://github.com/hoyoboy0726123/claude-skill-project-to-agent-plus "%USERPROFILE%\.claude\skills\claude-skill-project-to-agent-plus"
```

之後在 Claude Code 裡對任何專案說「把這個變成 agent」即觸發(skill 名:`project-to-agent-plus`,可與原版並存)。

## 它做什麼

當你說這類話的時候它會自動觸發:

- 「把這個 Python 腳本變成可以 Telegram 對話的 agent」
- 「我想讓這個 CLI 工具可以遠端用」
- 「做一個會自己寫新工具的 AI 助理」

Skill 帶 Claude(跟你)逐階段走完、最後得到一個架在你**現有專案**之上、用 Telegram(或本機 Web)操作的 agent。

## 階段總覽(Full 模式共 15 階段,下表為主線;8b/11b/13/14 詳見 SKILL.md)

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
