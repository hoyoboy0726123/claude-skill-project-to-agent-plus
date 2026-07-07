# Phase 13 — Context scaling(動態建議、evaluate-and-suggest)

> 這個 phase **不是必做** — 是 skill 跑到最後評估一次「現在的工具數值不值得做 token 優化」、給使用者具體建議。**沒到門檻就老實說沒到、設個未來提醒、退場**。

## 進入條件

Phase 1-12 全跑完(或至少 1-8 MVP + 10b 基礎工具)。Agent 已經能在 TG 跑。

## 為什麼需要

工具一多,每次 chat 都要把所有 tool schema 重傳給 LLM:
- 24 工具 ≈ 4.8K token / 每次 chat
- 100 工具 ≈ 20K token / 每次 chat
- Context window 還寬鬆,但**每次都付這個 input cost**(Gemini Pro / OpenAI / Anthropic 都是真錢)

完整理論背景見 `docs/tool-context-scaling.md`(專案內有就讀那份;沒有就照本 phase 內的決策表)。

## 決策矩陣 — provider × 工具數 雙因素

**Step 1**:Claude 跑這個拿到當前工具數 + provider:

```bash
python -c "from agent import get_registry, get_llm; print(len(get_registry().names()), type(get_llm()).__name__)"
```

**Step 2**:依 provider 找對應行、再依工具數對欄,**永遠 AskUserQuestion 確認**:

### Provider × tool count 矩陣

| Provider | < 25 | 25-50 | 50-100 | 100+ | 200+ |
|---|---|---|---|---|---|
| **Gemini Free**(Gemma 4 / Flash free tier)| 退場 + reminder | **只做 trim**(caching ❌)| trim + grouping | trim + grouping + embedding | + multi-agent |
| **Gemini Paid**(Flash/Pro 付費)| 退場 + reminder | trim + caching | + grouping | + embedding | + multi-agent |
| **OpenAI**(任何 model)| 退場 + reminder | **trim 就好**(caching 自動)| + grouping | + embedding | + multi-agent |
| **Anthropic**(任何 model)| 退場 + reminder | trim + caching(`cache_control`)| + grouping | + embedding | + multi-agent |
| **Groq**(任何 model)| 退場 + reminder | trim(provider 無 caching)| trim + grouping | trim + grouping + embedding | + multi-agent |
| **Ollama**(本機)| **不必做**(沒 API cost)| 不必 | 上 grouping(context window 限制)| 上 embedding | + multi-agent |

### 各 provider caching 真實狀況(2026-05 確認)

| Provider | Caching API | Free tier | 折扣 | 何時觸發 |
|---|---|---|---|---|
| Gemini(Gemma 4)| ❌ API 不支援 | — | — | — |
| Gemini 2.5 Flash/Pro | ✅ `caches.create()` | ❌ **storage limit = 0** | 25% 原價 | 顯式 build |
| OpenAI(全系列)| ✅ 自動 | (無 free tier)| 50% 原價 | prefix > 1024 token 自動 |
| Anthropic Claude 3+ / 4 | ✅ `cache_control` | (無 free tier、$5 trial)| **10% 原價** | system/tools/messages 加標記 |
| Groq | ❌ 不支援 | (free tier 大方)| — | — |
| Ollama | ❌ 概念不適用 | (本機免費)| — | — |

### Tier-specific AskUserQuestion 範例

**Gemini Free + 25-50 工具**:
```
"目前 N 工具、預估月 token N × ... 仍在 free tier 免費額度內。
要做 trim 收緊 description 嗎?(估省 N% token、改 30-90 分鐘、不會降你成本因為本來就免費、
但留意未來工具長到 50+ 時 context 大、grouping 會更急)"
```

**OpenAI + 25-50 工具**:
```
"目前 N 工具、月成本估 $X。OpenAI 對 > 1024 token prefix 自動 cache 50% off,
不必額外做 caching code。只需要做 trim 確保 prefix 穩定 + 把 system prompt 動態部分挪到
user message。要做嗎?(估改 90 分鐘、月成本降到 $X × 0.5 + 動態部分)"
```

**Anthropic + 25-50 工具**:
```
"目前 N 工具、月成本估 $X。Anthropic 用 cache_control 顯式標記 system/tools,
cache hit 收 10% 原價。要做 trim + cache_control 串接嗎?(改 120 分鐘、月成本估降到 $X × 0.15)"
```

不論選哪個、**都要在 system prompt 加未來 reminder block**(下面 mechanism)、工具又長到下個 tier 也會自動提醒。

## 對策 1:Tool description trim

**何時做**:任何時候、最便宜、無痛。
**改動量**:30-90 分鐘(看工具數)
**省**:單獨做只省 10-15% schema token;**配 caching(對策 2)才會大省**

### 流程

把每個 `Tool(description=...)` 從平均 100 字砍到 30-40 字、詳細用法整批搬進 `agent/system_prompt.py` 的「🛠 工具用法手冊」section。

**Before**(tool description 內塞詳細):

```python
WRITE_NOTE = Tool(
    name="write_note",
    description=(
        "Write a Markdown note (YAML frontmatter + body) into the Obsidian vault. "
        "TWO-STEP: call once with confirm=False to get a preview (target path + "
        "frontmatter); show preview to user; on user confirmation, call again "
        "with the SAME args plus confirm=True to actually write. Never set "
        "confirm=True without explicit user approval."
    ),
    ...
)
```

**After**(description 精簡、詳細搬手冊):

```python
WRITE_NOTE = Tool(
    name="write_note",
    description="Write .md to vault. TWO-STEP (confirm=False→preview, True→write).",
    ...
)
```

`agent/system_prompt.py` 加入:

```python
TOOL_MANUAL = """
# 🛠 工具用法手冊

## write_note(寫 vault 筆記)
- **何時用**:使用者要存內容進 Obsidian 知識庫
- **流程**:confirm=False → 拿 preview → 文字告訴使用者要寫什麼 → 等明確同意 → 同樣參數 confirm=True
- **參數**:title(會變檔名)/ summary(3-5 句繁中)/ tags(3-7 lowercase)
  / sub_folder / body(Markdown)/ source_type ("manual"|"url")
- **回**:{written_to, rel_path, size_bytes}

## search_vault
- **何時用**:使用者問「之前寫過 X 嗎」、「找跟 Y 相關的筆記」
- **語法**:`tag:foo`、`sender:bar`、`date:>2025-01-01`、`"phrase"`、`-exclude`、純關鍵字
- **回**:{count, hits:[{title, sender, date, tags, snippet, score, rel_path}]}

## (其他工具一一條列、每個 ~80-150 字、總計 ~2K-3K 字)
"""
```

build_system_prompt() 把 `TOOL_MANUAL` 連同其他 sections 一起組裝。

### 注意

- **太短會迷路** — 30-40 字底線、不要砍到剩 10 字
- **參數 description 也 trim** — 但保留 type hint("YYYY-MM-DD format" 這種還是要)
- **完整手冊只算一次**(不像 tool schema 隨工具數膨脹)— 所以放手冊裡反而省

### 驗證

```python
# 跑前
before = sum(len(s['description']) for s in get_registry().schemas())

# 改完跑
after = sum(len(s['description']) for s in get_registry().schemas())
print(f"description chars: {before} → {after} ({100*(before-after)/before:.0f}% off)")
```

## ⚠ Caching 不能做的情況 — 不要硬上

跑 Phase 13 前**先確認 provider 能不能 caching**,不能就**不要承諾 caching 部分**,只做 trim:

```bash
python -c "
import os
from agent.llm_client import make_llm
m = make_llm()
p = type(m).__name__
free_tier_likely = (p == 'GeminiClient' and not os.getenv('GEMINI_BILLING_ENABLED'))
gemma = p == 'GeminiClient' and 'gemma' in (m.default_model or '').lower()
no_caching = p in ('GroqClient', 'OllamaClient') or gemma or free_tier_likely
print(f'provider={p}, model={m.default_model}, caching_advisable={not no_caching}')
"
```

**Free tier 真實限制**(Google AI Studio 2026-05):
- Gemma 4 系列 → `createCachedContent` API 直接 404
- Gemini 2.5 Flash/Pro free tier → 422 `TotalCachedContentStorageTokensPerModelFreeTier limit=0`

要實際 cache 必須:
- 升 Google AI Studio paid tier(信用卡)
- 或切到 OpenAI(prefix caching 自動、不必付額外 setup)
- 或切到 Anthropic(`cache_control` 顯式、$5 trial 給新帳號)

production 版的 `agent/llm_client.py:GeminiClient` 已經有 fallback 邏輯 — 試一次失敗就 `_cache_supported = False`、後續直接 skip、不浪費 latency。所以**先把 trim 做好、caching code 留著就行**、之後升 tier 自動 work。

## 對策 2:Prompt caching

**何時做**:做完對策 1 後緊跟著做。
**改動量**:30-60 分鐘
**省**:60-90% input **cost**(token 量沒減、但 cache hit 不收全價)

### 各 provider 怎麼開

#### Gemini

```python
# 建一次性 cache(system prompt + tool schemas 都打包)
from google.genai import types as gt

cache = client.caches.create(
    model="gemini-2.5-flash",
    config=gt.CreateCachedContentConfig(
        system_instruction=SYSTEM_PROMPT,   # 含對策 1 的工具手冊
        tools=tool_schemas_list,
        ttl="3600s",                         # 1h
    ),
)

# 後續 chat 用 cache name
resp = client.models.generate_content(
    model="gemini-2.5-flash",
    contents=user_message,
    config=gt.GenerateContentConfig(cached_content=cache.name),
)
```

cache hit = **25% 原價** input cost。

#### Anthropic

```python
# 在最後一個 system block 加 cache_control
system = [
    {"type": "text", "text": SYSTEM_PROMPT_INCLUDING_TOOL_MANUAL,
     "cache_control": {"type": "ephemeral"}},
]
# tools 區段也可以加 cache_control 在最後一個 tool
resp = client.messages.create(
    model="claude-haiku-4-5",
    system=system,
    tools=tools_with_cache_control,
    messages=[...],
)
```

cache hit = **10% 原價** input cost(便宜很多)。

#### OpenAI / Groq

**自動 cache**、不用改 code — prefix > 1024 token 就自動結算 50% 原價。確保 system prompt + tools 順序穩定(放最前)就好。

### 關鍵陷阱:動態內容會破 cache

system prompt 不能含每次都會變的東西(今日日期、user-specific context)— 任何 1 byte 變動整段 cache 失效。

**解法**:把動態內容(日期、vault 狀態)放在 **user message 開頭**、不要在 system prompt:

```python
# ❌ 不行 — 每天 cache miss
SYSTEM_PROMPT = f"You are... 今天是 {datetime.now()}..."

# ✅ 行 — 動態部分塞 user message,system 永遠 stable
SYSTEM_PROMPT = "You are an agent..."  # 完全 static
user_msg_with_context = f"[今日:{datetime.now():%Y-%m-%d}] {actual_user_input}"
```

### 驗證

跑兩輪同 prompt、看第二輪 `usage.cached_tokens`(各 provider 都有報)是否非零。Gemini 在 `usage_metadata.cached_content_token_count`、Anthropic 在 `usage.cache_read_input_tokens`、OpenAI 在 `usage.prompt_tokens_details.cached_tokens`。

## 對策 3:Tool grouping(只有 50+ 工具才做)

把 N 工具按 domain 分組(vault / file / web / sandbox / meta / evolution),LLM 先看到 `select_tool_group(name)` + always-on 基礎 3 個(`ask_user / done / current_state`),選了組之後第二輪才看到該組詳細 schema。

詳細實作見 `docs/tool-context-scaling.md` §3.3。

## 對策 5:Embedding routing(只有 100+ 工具才做)

每個 tool description 預先 embed → 使用者每輪 message 也 embed → cosine top-K → 只給 LLM 那 K 個。Gemini text-embedding-004 完全免費、24-100 個工具 numpy in-memory 0 元搞定。

詳細實作見 `docs/tool-context-scaling.md` §3.5(完整 80 行 `tool_routing.py`)。

## 未來提醒 mechanism — 不做也要設(provider-aware)

**重要**:不論這次選做或不做,system prompt 必須含「context scaling reminder」block,讓 agent 未來自己會偵測工具數又長、提醒使用者回來。**reminder 內容要依當前 provider 變化**:

```python
# agent/system_prompt.py
import os

def _detect_caching_available() -> tuple[bool, str]:
    """回 (can_cache, reason)。Phase 13 reminder 用這個產對的建議。"""
    from agent.llm_client import make_llm
    try:
        m = make_llm()
    except Exception as e:
        return False, f"LLM init 失敗:{e}"
    p = type(m).__name__
    model = (getattr(m, "default_model", "") or "").lower()
    if p in ("GroqClient", "OllamaClient"):
        return False, f"{p} 不支援 caching"
    if p == "GeminiClient":
        if "gemma" in model:
            return False, "Gemma 系列 API 不支援 caching"
        if not os.getenv("GEMINI_BILLING_ENABLED"):
            return False, "Gemini free tier 限額 0,要付費 tier"
    return True, "可做"


def _scaling_reminder(registry) -> str:
    n = len(registry.names())
    can_cache, why = _detect_caching_available()
    
    # tier-specific 建議
    if can_cache:
        thresholds = [
            (25, "trim + caching"),
            (50, "+ grouping"),
            (100, "+ embedding routing"),
            (200, "+ multi-agent split"),
        ]
    else:
        thresholds = [
            (25, f"trim only(caching ❌:{why})"),
            (50, "trim + grouping"),
            (100, "trim + grouping + embedding"),
            (200, "+ multi-agent split"),
        ]
    
    next_tier = next(((t, what) for t, what in thresholds if n < t), None)
    if next_tier:
        t, what = next_tier
        return (
            f"\n# 📊 Context scaling watch\n"
            f"目前 {n} 工具、caching {'可做' if can_cache else f'不可做({why})'}。\n"
            f"下個門檻 {t} 工具會建議做 `{what}`、到了會主動提醒你。\n"
        )
    return (
        f"\n# 📊 Context scaling watch\n"
        f"目前 {n} 工具、已過所有門檻、考慮 multi-agent split。\n"
        f"Caching 狀態:{'可做' if can_cache else f'不可做({why})'}\n"
    )
```

build_system_prompt() 結尾把這段加進去 — **永久存在,即使對策都不做也會提醒**、且提醒**會反映當前 provider 真實狀況**。

或更主動:寫個小 helper tool `context_scaling_status() -> dict` 讓 agent 自己呼,回 `{current_n, next_threshold, recommended_action, caching_available, caching_reason, current_cost_estimate_per_month}`。LLM 在使用者問「你最近一個月貴嗎?」之類時主動跑。

## Phase 13 對話腳本(給跑這個 phase 的 Claude 用)

```
1. Claude 拿三個事實:
   N = 工具數         (python -c "from agent import get_registry; print(len(get_registry().names()))")
   P = provider 名稱   (python -c "from agent.llm_client import make_llm; print(type(make_llm()).__name__)")
   M = model 名稱      (python -c "from agent.llm_client import make_llm; print(make_llm().default_model)")

2. Claude 推斷 caching 可用性:
   - Ollama / Groq → no caching ever
   - Gemini + 'gemma' in model → no caching (API 不支援)
   - Gemini + 'gemini' in model + 無 billing → 推測 free tier、no caching
   - 其他(OpenAI / Anthropic / Gemini paid)→ caching 可做

3. Claude 算估月成本(假設 100 chat/天):
     monthly_cost ≈ N × 0.2 KB × 1.7K × 3000 / 1_000_000 × provider_price
     # provider_price: Gemini Flash $0.075 / OpenAI gpt-4o-mini $0.15 / Anthropic Haiku $0.80

4. AskUserQuestion 對應 (N, caching_可用) 的選項:
   - "做 [tier-specific 對策組合]" → 跑 implementation flow
   - "先不做、設提醒" → 寫 reminder block 到 system prompt、退場
   - "查更多細節" → 摘要 docs/tool-context-scaling.md

5. 不論選什麼、最後一步:確保 system prompt 內有 `_scaling_reminder()` 段落、
   reminder 內容**也要 reflect 當前 provider**(例如:
   "下次工具到 50,Gemini free tier 仍無法 caching,只能上 grouping;
    若想開 caching 請切付費 tier 或換 OpenAI/Anthropic")

6. Phase 結束。Skill 14-phase 工作流真正跑完。
```

## Anti-pattern

- ❌ **不問就自動做 trim/caching** — 即使工具數到門檻、也要 AskUserQuestion。使用者可能不在乎 cost(自用 / free tier 內)、強推浪費他時間
- ❌ **24 工具就上 embedding routing** — 過度設計,trim+caching 就好
- ❌ **跨 phase / 跨 session 累積建議** — 這個 phase 是 final、跑一次就結束。下次 agent 自己會用 reminder block 提醒、不靠 skill 跑第二次
- ❌ **省成 system prompt 過 cache 上限**(Gemini Flash 4K、Pro 32K、Anthropic Sonnet 1K)— 算一下手冊長度、超過就分多段 cache 或縮短
- ❌ **把 user-specific context 塞 system prompt** — cache 永遠 miss、白做
- ❌ **Gemini free tier 答應做 caching** — API 直接擋(Gemma 404 / Flash storage limit=0)、必死。先 detect provider 跟 free tier 狀況、不能做就老實說「先 trim 收尾、未來升 paid tier 或換 OpenAI 再開」
- ❌ **OpenAI 還在實作 cache_control 之類** — OpenAI 是自動 prefix caching、不必碰 code、只要確保 prefix 穩定就行
- ❌ **Ollama / Groq 推薦 caching** — 兩者根本沒 caching API、推了使用者照著做會撞牆

## 檢查清單

- [ ] Claude 實際 query 了工具數 + provider + model name(不是猜)
- [ ] Claude 判斷了 caching 可用性(_detect_caching_available 或同等邏輯)
- [ ] 算出大概月成本給使用者看(估算公式:`N × 200_token × M_chat × 30_day × provider_price / 1M`)
- [ ] AskUserQuestion 給出 **provider × tier-appropriate** 的選項(不要對 Gemini free tier 推 caching)
- [ ] 沒對 Ollama / Groq / Gemma 推 caching
- [ ] 不論做或不做,system prompt 都加了 provider-aware 的 `_scaling_reminder()` 區段
- [ ] 若做了 caching:跑驗證(`get_llm().cache_stats()['hits'] > 0` 或 OpenAI usage `cached_tokens > 0`、description char 減少、月成本估算下降)
