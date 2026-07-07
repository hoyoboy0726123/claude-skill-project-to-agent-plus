# Phase 14 — Memory System(opt-in)

> 不裝記憶的 agent 等於每次見面都自我介紹一遍 —— 對 quick query / stateless 場景沒差,對「個人助理 / 長期合作」場景就是 crippling gap。

## 何時做這個 phase

✅ 加 memory:
- agent 預期被同一個使用者長期用(月計、年計)
- 使用者會反覆提到自己的偏好(常用資料夾 / 寫作風格 / 不喜歡的詞)
- 工作流會跨 session(週一聊到的事週三要繼續)

❌ 不必加:
- 純 stateless query bot(查天氣 / 計算機)
- 一次性自動化(報表生成完就閒置)
- agent 跑在 CI / cron 場景

兩種情況都 OK — 不裝就是 Phase 13 跑完直接結束。要裝就跑這 phase、產出記憶系統。

## 記憶分 4 層(對應主流 agent memory 架構)

```
┌─────────────────────────────────────────────────────────────────┐
│                       AGENT MEMORY LAYERS                        │
├─────────────────────────────────────────────────────────────────┤
│                                                                  │
│  1. WORKING (對話歷史、跨重啟持久化)                             │
│     ↓ chat_store.db messages table,cap 載入最近 30 則           │
│     ↓ ★ Phase 8b 已建 sqlite,Phase 14 在此基礎加恢復機制       │
│                                                                  │
│  2. SEMANTIC (語意、persistent)                                  │
│     ↓ 跨 session 持久的事實 + 偏好                              │
│     ↓ 例:「我的 vault 在 D:\notes」「我習慣 tag 用小寫」       │
│     ↓ Storage: memory.db facts table                            │
│                                                                  │
│  3. EPISODIC (情節、persistent + searchable)                    │
│     ↓ 過去對話的摘要、可語意檢索                                │
│     ↓ 例:「上週聊到的那個演算法是什麼?」                       │
│     ↓ Storage: memory.db episodes + embedding(numpy in-memory) │
│                                                                  │
│  4. PROCEDURAL (程序、optional、與 Phase 12 重疊)               │
│     ↓ 成功跑過的 tool 序列當 macro 記下、之後可重用             │
│     ↓ Phase 12 self-evolution 已涵蓋同樣場景、不必另做         │
│                                                                  │
└─────────────────────────────────────────────────────────────────┘
```

**MVP = 第 1 層(working 持久化)+ 第 2 層(facts)+ 第 3 層(episodes)**。第 4 層 Phase 12 cover。

> 📌 **更正過往文件**:更早版本說「working memory 是 in-memory only、重啟就沒」 — 現在不是。Phase 8b 加 `chat_store.db` 後、working memory **跨重啟也保留**;Phase 14 只是在這基礎上加「載回 orchestrator + 三層調用」的橋接邏輯。

## Storage 選型

| 工具量 | 推薦 |
|---|---|
| < 10K episodes | **sqlite + numpy in-memory cosine**(本 reference 主推) |
| 10K-100K | sqlite + sqlite-vss extension |
| 100K+ | postgres + pgvector / Chroma / Qdrant(進階,本 reference 不展開)|

對 personal agent 用例,**sqlite + numpy 5 年也不會炸**。

存哪裡:
- `~/.cache/agent-mem/<project>.db`(per-project 隔離,避免跨 agent 串味)
- 或者更隱藏:`<project_root>/.agent_memory/memory.db` + `.gitignore` 加進去

## Schema(sqlite 一個檔)

```sql
-- 語意記憶:key-value 偏好 / 事實
CREATE TABLE facts (
    key         TEXT PRIMARY KEY,
    value       TEXT NOT NULL,
    category    TEXT,                  -- 'preference' / 'fact' / 'profile'
    source      TEXT,                  -- 'user_told' / 'inferred' / 'tool_result'
    confidence  REAL DEFAULT 1.0,      -- 0-1, inferred 較低
    updated_at  INTEGER NOT NULL       -- unix epoch
);

-- 情節記憶:對話摘要 + 向量
CREATE TABLE episodes (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    chat_id     TEXT NOT NULL,
    summary     TEXT NOT NULL,         -- LLM 自動產的 1-3 句摘要
    embedding   BLOB NOT NULL,         -- numpy float32 array, ~3KB each
    tags        TEXT,                  -- comma-separated, optional
    created_at  INTEGER NOT NULL
);
CREATE INDEX idx_episodes_chat ON episodes(chat_id, created_at DESC);

-- Audit log (透明度 / debug 用)
CREATE TABLE memory_writes (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    op          TEXT NOT NULL,         -- 'remember_fact' / 'forget_fact' / 'wipe'
    detail      TEXT NOT NULL,         -- JSON
    timestamp   INTEGER NOT NULL
);
```

## 7 個新工具

| Tool | 類型 | Two-step? | 用途 |
|---|---|:---:|---|
| `remember_fact(key, value, category?, confirm)` | write | ✅ | 記一個事實 / 偏好 |
| `recall_fact(key)` | read | — | 拿 key 對應的 value |
| `list_facts(category?, limit=20)` | read | — | 列出所有記憶 |
| `forget_fact(key, confirm)` | write/delete | ✅ | 刪一條 |
| `recall_episode(query, max_results=5)` | read | — | 用 query 找過去對話摘要 |
| `wipe_memory(scope, confirm)` | destructive | ✅✅ | 一鍵清空(facts / episodes / all)|
| `memory_state()` | read | — | 環境快照:facts 數量 / episodes 數量 / 上次寫入時間 |

## Episode 自動產生(沒有 user-facing tool)

每個 chat session 結束時(`/reset` 或 idle 30 min),background 自動跑:

```python
def autoshelve_episode(orch: Orchestrator):
    if len(orch.messages) < 4:
        return  # 短對話不存
    # Ask LLM to summarize this session in 1-3 sentences
    summary = orch.llm.chat(
        messages=[
            {"role": "system", "content":
                "Summarize this conversation in 1-3 繁中 sentences. "
                "Focus on what the user wanted and what was actually done."},
            *orch.messages[1:],   # skip system prompt
        ],
        tools=None,
    ).text
    embedding = embed(summary)   # Gemini text-embedding-004 (free)
    memory.add_episode(orch.chat_id, summary, embedding)
```

**LLM 不直接呼這個** — 是 orchestrator / TG adapter `/reset` handler 自動觸發。使用者沒感覺、但下次 agent 能 recall_episode 找到。

## 記憶寫入協議(對齊 Phase 5 two-step)

```
User: 「記一下我的 vault 在 D:\notes」
  ↓
Agent: remember_fact(key="vault_path", value="D:\notes", confirm=False)
  → 拿 preview: {confirm_required, would_remember, existing_value?}
  ↓
Agent: 「我要把『vault_path = D:\notes』記下來、之後永久。確認?」
  ↓
User: 「對」
  ↓
Agent: remember_fact(key="vault_path", value="D:\notes", confirm=True)
  → {remembered, db_path}
```

關鍵點:
- **記新事實 = 寫操作 = two-step**。LLM 不能擅自記事。
- **recall 是讀 = 不必 confirm**。
- **forget / wipe = destructive**,UI 上 wipe 要顯紅字警告。

## Privacy 守備(這 phase 是 attack surface!)

| 風險 | 對策 |
|---|---|
| Agent 偷記敏感事(API key / 密碼)| Hard deny-list 在 `remember_fact` 內:value 含 `sk-` / `aiza` / `bearer` / `password` / `pwd` → 直接拒絕記 |
| 記憶檔被誤 commit | `.agent_memory/` 預設加 `.gitignore`、permissions.json 寫入 agent_memory 路徑後不開 read 給 propose_tool flow |
| 使用者忘了 agent 記了什麼 | `memory_state()` 工具開機就 inject 進 system prompt 一行:「目前記得 N 個 fact、最久 X 個月前」|
| 想 export | 提供 `python -m agent.memory_export > memory.json`(skill 提供 helper script)|
| Agent 不該記的時候硬要記 | system prompt 教 LLM:「使用者明確說『記下』才呼 remember_fact、不要主動把所有事都記」|

## Working memory 持久化 — 重啟後「記得上次聊到哪」

Phase 8b 的 `chat_store.db` 已經 persist 每筆 user / assistant / tool message,但**重啟後 `Orchestrator.messages` 是空的** — 因為 orchestrator init 只 push 一條 system prompt,不會自動撈 sqlite。

要做的是:**啟動時、從 chat_store 撈最近 N 則 load 回 `orchestrator.messages`**。

### 載入策略 — `cap=30` 的理由

```python
# agent/__init__.py (or orchestrator_factory)
def orchestrator_factory(chat_id: str, channel: str = "telegram"):
    orch = Orchestrator(llm=_llm, registry=_registry,
                         system_prompt=build_static_system_prompt(...))
    # Load last 30 messages from chat_store
    from agent.chat_store import ChatStore
    store = ChatStore()
    recent = store.get_orchestrator_messages(chat_id)[-30:]   # ← 載入 cap
    if recent:
        orch.messages.extend(recent)
    return orch
```

**為什麼 cap=30**:
- **Token 預算**:Gemini 2.5 cache 友善需要 prefix 穩定,history 太長每次 cache miss
- **LLM 注意力**:太多瑣碎細節會稀釋當下意圖,30 則 ≈ 約 5-10 輪交互(含 tool result)、剛好涵蓋「這場對話」
- **per-message ~200 tokens × 30 = 6K**,跟 system prompt 一起 ~13K,fit 128K context 寬鬆

**所有對話都備份在 sqlite**(`chat_store.db` 沒上限),30 則只是「載入 LLM context」的數量。需要更舊的 — 走 episodic recall(下節)。

### 三層調用模式 — 短期 / 中期 / 長期

當使用者問「我上次說過 X 嗎?」、agent 該怎麼找?設計三層 fallback:

```
使用者問題
   ↓
①  Working memory(最近 30 則 message)
   ↓ LLM 在 context 內看得到、直接答
   ↓ 找不到 ↓
②  Memory snapshot(system prompt 內預載 5 個 fact)
   ↓ LLM 隱性「我本來就知道」
   ↓ 找不到 ↓
③  recall_episode tool(向量檢索全部歷史摘要)
   ↓ LLM 主動呼這 tool 撈相關片段
```

### Layer 2 — Memory Snapshot(被動知道、無感)

每次 chat 開始,**前 5 個最近 fact 自動 inject 進 system prompt 動態區**:

```python
# agent/system_prompt.py
def build_dynamic_context() -> str:
    lines = [f"[今日 {now:%Y-%m-%d}]"]   # 已有
    # NEW: memory snapshot
    if memory_enabled:
        facts = memory.list_facts(limit=5, order_by="updated_at DESC")
        if facts:
            lines.append("[我記得這些事實:]")
            for f in facts:
                lines.append(f"  - {f['key']} = {f['value'][:60]}")
    return "\n".join(lines)
```

**效果**:使用者問「我喜歡的幣別?」、agent 不必呼 `recall_fact("favorite_currency")`,**在 system prompt 內就看到** `favorite_currency = PLN`,直接答。

### Layer 3 — recall_episode(主動翻箱倒櫃)

當使用者問「上個月聊到那個 X 是什麼?」、且 Layer 1+2 找不到:

```python
# system prompt 內寫死引導:
RECALL_HINT = """
如果使用者提到你印象中沒有的舊事、**不要說「我不知道」**。
先呼 recall_episode(query="關鍵字"),拿回過去對話摘要,再回答。
"""
```

LLM 看到引導 → 呼 `recall_episode(query="X")` → 拿到 top-K 相關 episode summary → 用這些 summary 答使用者。

### Autoshelve — Working → Episodic 的橋

當對話過長 / 使用者 `/reset` / idle 30min,background 自動:

```python
def autoshelve_episode(orch, user_id, chat_id):
    if len(orch.messages) < 4:
        return  # 短對話不存
    # Ask cheap LLM to summarize
    summary = orch.llm.chat([
        {"role": "system", "content":
            "Summarize this conversation in 1-3 繁中 sentences. "
            "Focus on what the user wanted and what was actually done."},
        *orch.messages[1:],  # skip system prompt
    ]).text
    embedding = embed(summary)   # Gemini text-embedding-004 (free)
    memory.add_episode(user_id, chat_id, summary, embedding)
```

**觸發點**:
- Telegram `/reset` 指令
- Streamlit「➕ 新對話」按鈕
- (可選)`Orchestrator.messages` 超過 60 則時(自動 archive 前 30)

### 完整流程圖

```
[Agent 啟動]
   ↓
chat_store.get_orchestrator_messages(chat_id)[-30:]
   ↓
orchestrator.messages = [system, ...last 30]    ← Layer 1 載入

[每次使用者送 msg]
   ↓
build_dynamic_context() 加 5 fact + 今日日期    ← Layer 2 inject
   ↓
LLM 答覆,可選擇:
   - 直接從 history + facts 答(80% case)
   - 呼 recall_episode("query") 撈舊對話   ← Layer 3 主動

[使用者 /reset 或閒置 30min]
   ↓
autoshelve_episode → 對話摘要 → embed → 存 memory.db.episodes
   ↓
chat_store + memory.db 都保留、新對話從乾淨開始
```

### Anti-patterns

- ❌ **載入全部歷史進 LLM**(`get_orchestrator_messages(chat_id)` 不加 `[-30:]`)→ 100 輪對話後 token / cache cost 爆炸
- ❌ **不做 autoshelve** → 對話歷史只增不減、永遠保不到精華;使用者隔週問「上次說的 X」找不到(在 30 則外)
- ❌ **memory snapshot 塞**所有** fact** → system prompt 變超長、cache 友善崩;只塞 top 5 by `updated_at DESC`
- ❌ **每次 chat 都呼 recall_episode** → 浪費 embedding API call;只在 LLM「沒印象」時才呼,system prompt 引導即可
- ❌ **autoshelve 用主 LLM**(gemini-2.5-pro)摘要 → 浪費錢;用 `gemini-flash-lite` / `gemma-4` 之類便宜 model

## Per-user 隔離 — ⛔ 用 ContextVar、不要 inject kwargs

memory 系統最常見的需求是「**多 user 隔離**」(facts 跟 episodes 不可跨 user 串味)。實作要從 chat_id / session_id 拿到 `current_user_id` 給 `remember_fact` / `recall_episode` 用。

### ❌ Anti-pattern:registry 強塞 `_chat_id` 到 args

**真實案例**:有 agent 為了讓 memory tools 拿到 `_chat_id`,改 `registry.run()` 在 call 前 inject:

```python
# ❌❌❌ 這樣做的下場
def run(name, args):
    args["_chat_id"] = current_chat_id  # 強塞給每個 tool
    return self._tools[name].func(**args)
```

**結果**:**所有既有 tool 函數**(`write_note` / `read_file` / `run_python` / `web_search` / 24 個工具全部)噴:

```
TypeError: write_note() got an unexpected keyword argument '_chat_id'
```

只能一個一個工具加 `**kwargs` 接,**改數十個檔**。typical leaky-abstraction antipattern。

### ✅ 正確設計:ContextVar(Python 標準庫、無侵入)

```python
# agent/user_context.py — 新增這個檔
from contextvars import ContextVar

# 全域、但 contextvar 對每個 thread / async task 隔離
current_user_id: ContextVar[str] = ContextVar(
    "current_user_id", default="anonymous"
)
current_chat_id: ContextVar[str] = ContextVar(
    "current_chat_id", default="default"
)
```

**Adapter 端 set**(每個 turn 開始時):

```python
# TG adapter._on_text:
from agent.user_context import current_user_id, current_chat_id
current_user_id.set(str(update.effective_user.id))
current_chat_id.set(str(update.effective_chat.id))
# 然後跑 orch.step()...

# Web adapter (Streamlit):
current_user_id.set(st.session_state.get("user_id", "local"))
current_chat_id.set(active_conv_id)
```

**Memory tool 端 get**:

```python
# agent/memory_tools.py
from agent.user_context import current_user_id, current_chat_id

def remember_fact(key: str, value: str, ..., confirm: bool = False) -> dict:
    user_id = current_user_id.get()
    # ... store with user_id namespace ...
    memory.remember(user_id, key, value)
```

**關鍵差別**:
- Memory tools 顯式 import `current_user_id` 並 `.get()` — 沒有「framework 把 user_id 塞給你」
- 其他 24 個 tool 函數 signature **完全不必改**
- ContextVar 自動隔離 thread / async task,Streamlit per-session 安全、TG per-chat 安全

### sqlite schema:用 user_id 當 namespace

```sql
CREATE TABLE facts (
    user_id     TEXT NOT NULL,   -- ← 加 user_id 欄、不另開 .db
    key         TEXT NOT NULL,
    value       TEXT NOT NULL,
    ...
    PRIMARY KEY (user_id, key)
);
CREATE TABLE episodes (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    user_id     TEXT NOT NULL,
    chat_id     TEXT NOT NULL,
    summary     TEXT NOT NULL,
    embedding   BLOB NOT NULL,
    ...
);
CREATE INDEX idx_facts_user ON facts(user_id);
CREATE INDEX idx_episodes_user ON episodes(user_id, created_at DESC);
```

優點 vs `<user_id>.db` 多檔方案:
- 單一 db 檔、備份簡單
- 跨 user 偶爾要查全局 stats 也能 join
- 仍然 isolated — `WHERE user_id = ?` 永遠在 query 內

**個人 use(single user)**:`current_user_id` default "anonymous" 就 work、不必另設。

### 跟 Phase 11 web_search 共用同套 ContextVar

Phase 11 的 per-user rate limit 也走同一機制:

```python
# agent/web_search.py
from agent.user_context import current_user_id
def web_search(query: str, ...) -> dict:
    user_id = current_user_id.get()
    # check _usage[user_id] rate limit
```

**結論**:`agent/user_context.py` 一個檔解決所有跨 phase 的 per-user 需求,不污染任何 tool signature。

## 跟其他 Phase 的對齊

- **Phase 4(orchestrator)**:autoshelve_episode 在 `/reset` handler 觸發、不污染 planner loop
- **Phase 5(two-step write)**:`remember_fact / forget_fact / wipe_memory` 都走
- **Phase 6(hallucination guard)**:LLM 說「我記下來了」必須對應實際 `confirm=True` 的 `remember_fact` 呼叫,否則警告
- **Phase 7(permissions)**:`~/.cache/agent-mem/<project>.db` 路徑進 permissions.json write 區
- **Phase 9(system prompt)**:`memory_state()` snapshot 放 **user message 動態 prefix**、不放 static system prompt(否則破 Phase 13 caching)
- **Phase 12(self-evolution)**:procedural memory(macro)概念跟 self-evolution 重疊、不另做
- **Phase 13(context scaling)**:memory snapshot 不能塞 cacheable prefix,動態 inject

## 實作 skeleton(production agent 該長什麼樣)

```python
# agent/memory.py
import json, sqlite3, time
from pathlib import Path
import numpy as np

DB_PATH = Path.home() / ".cache" / "agent-mem" / "<project>.db"

SENSITIVE_PATTERNS = [
    r"\bsk-[a-zA-Z0-9]{16,}",      # OpenAI key
    r"\baiza[a-zA-Z0-9_-]{30,}",   # Google API key
    r"\bbearer\s+[a-zA-Z0-9._-]+",
    r"\bpassword\b\s*[:=]",
    r"\bpwd\b\s*[:=]",
    r"\bid_rsa\b",
]


class Memory:
    def __init__(self, db_path: Path = DB_PATH):
        db_path.parent.mkdir(parents=True, exist_ok=True)
        self.db = sqlite3.connect(str(db_path))
        self._init_schema()

    def _init_schema(self):
        self.db.executescript(SCHEMA_SQL)   # CREATE TABLE ... from above

    # ─── Semantic (facts) ────────────────────────────────────
    def remember_fact(self, key: str, value: str,
                       category: str = "fact", source: str = "user_told") -> dict:
        # Hard deny-list check
        for pat in SENSITIVE_PATTERNS:
            if re.search(pat, value, re.I):
                return {"error": f"refused: value looks like a secret (pattern: {pat})"}
        now = int(time.time())
        self.db.execute(
            "INSERT OR REPLACE INTO facts(key, value, category, source, updated_at) "
            "VALUES (?, ?, ?, ?, ?)",
            (key, value, category, source, now),
        )
        self.db.commit()
        return {"remembered": True, "key": key, "updated_at": now}

    def recall_fact(self, key: str) -> dict:
        row = self.db.execute(
            "SELECT value, category, source, updated_at FROM facts WHERE key=?",
            (key,)).fetchone()
        return {"found": True, "key": key, "value": row[0],
                "category": row[1], "source": row[2], "updated_at": row[3]} if row else \
               {"found": False, "key": key}

    # ... list_facts, forget_fact, wipe_facts similar ...

    # ─── Episodic (summaries + embeddings) ───────────────────
    def add_episode(self, chat_id: str, summary: str, embedding: np.ndarray):
        self.db.execute(
            "INSERT INTO episodes(chat_id, summary, embedding, created_at) "
            "VALUES (?, ?, ?, ?)",
            (chat_id, summary, embedding.astype(np.float32).tobytes(), int(time.time())),
        )
        self.db.commit()

    def recall_episode(self, query_emb: np.ndarray, max_results: int = 5) -> list[dict]:
        rows = self.db.execute(
            "SELECT id, chat_id, summary, embedding, created_at FROM episodes"
        ).fetchall()
        if not rows:
            return []
        embeds = np.stack([np.frombuffer(r[3], dtype=np.float32) for r in rows])
        sims = embeds @ query_emb / (
            np.linalg.norm(embeds, axis=1) * np.linalg.norm(query_emb) + 1e-9
        )
        top = np.argsort(sims)[-max_results:][::-1]
        return [{
            "id": rows[i][0], "chat_id": rows[i][1],
            "summary": rows[i][2], "similarity": float(sims[i]),
            "created_at": rows[i][4],
        } for i in top]


# agent/memory_tools.py — Tool wrappers,register conditionally
# 在 agent/tools.py register_all 加:
# if os.getenv("ENABLE_MEMORY", "").lower() in ("1", "true", "yes"):
#     from agent.memory_tools import register_memory_tools
#     register_memory_tools(registry)
```

## System prompt 動態 inject(per-turn,不破 cache)

`build_dynamic_context()`(Phase 13 已建)結尾加:

```python
def build_dynamic_context() -> str:
    lines = [..., ...]  # 既有的今日日期 / vault status

    if memory_enabled:
        n_facts = memory.count_facts()
        if n_facts > 0:
            top5 = memory.list_facts(limit=5)
            lines.append(f"[Memory: {n_facts} facts known. Top 5: " +
                          ", ".join(f"{k}={v[:40]}" for k, v in top5) + "]")
    return "\n".join(lines)
```

LLM 每輪都看到「我記得這些」、會主動參考(例:使用者問「我 vault 在哪?」LLM 直接從 memory snapshot 答、不必呼 recall_fact)。

## AskUserQuestion(進入 Phase 14 時跑)

```python
AskUserQuestion(questions=[{
    "question": "要開啟長期記憶嗎?Agent 會記得你跨 session 的偏好 / 事實 / 對話摘要。",
    "header": "Memory",
    "multiSelect": False,
    "options": [
        {"label": "全開(facts + episodes + autoshelve)",
         "description": "推薦給長期助理用例;memory.db 在 .agent_memory/"},
        {"label": "只開 facts(語意),不存對話摘要",
         "description": "比較精簡;適合不想被「監聽」對話的 user"},
        {"label": "先不開、設個未來提醒",
         "description": "Phase 結束、reminder block 提醒「之後可以開」"},
    ],
}])
```

## Anti-patterns

- ❌ **把 memory snapshot 塞 static system prompt** → 每筆記憶變動都破 Phase 13 cache、白做
- ❌ **不問就自動 remember** → LLM 把所有對話內容都記、隱私事故
- ❌ **記敏感事**(API key / 密碼)→ 必 hard deny-list 擋
- ❌ **跨 user 共用 memory.db** → 多人 TG bot 場景一定要 per-user 隔離(`<user_id>.db`)
- ❌ **過度依賴 memory.db、不備份** → `.agent_memory/` 進 cron backup、或 git-crypt encrypt 後 commit(privacy 接受的話)
- ❌ **wipe 沒 double-confirm** → wipe = destructive、要兩階段 + 額外警告

## Checklist

- [ ] AskUserQuestion 跑了、使用者明確選了哪種 memory 配置
- [ ] `agent/memory.py` 建好、sqlite schema 跑成功
- [ ] 7 個工具 register、其中 3 個 write 操作走 two-step
- [ ] `~/.cache/agent-mem/` 或 `.agent_memory/` 進 permissions.json write 允許清單
- [ ] `.gitignore` 加 `agent_memory/`(避免誤 commit)
- [ ] SENSITIVE_PATTERNS deny-list 跑過至少一次驗證(試記 fake API key 看是否被擋)
- [ ] `build_dynamic_context()` 加 memory snapshot(放 user message 前綴、不污染 static prompt)
- [ ] autoshelve_episode 串到 TG `/reset` handler 跟 idle timeout
- [ ] 提供 `python -m agent.memory_export` 給使用者匯出檢視
- [ ] 跟使用者跑一次完整 flow:remember → recall(下次 chat)→ list → forget → wipe
