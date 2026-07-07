# Phase 11 — Web search(opt-in)

## 何時加

agent 需要查網路即時資訊(新聞、文件、價格、最近事件)才需要。純內部專案 agent / 不上網的場景跳過整個 phase。

## 為什麼 Tavily

- 免費 tier 1000 searches/月、對 personal use 很夠
- 回傳已 cleaned 的 LLM-friendly 結果(沒 nav cruft、有 relevance score)
- 一個 API key + 一個 function、不必自己寫 scraper
- alternatives:Brave Search / Serper / DuckDuckGo — 都可、但教學以 Tavily 為主

## 取 key + 裝 SDK

```
1. https://tavily.com 註冊 → 拿 key
2. .env 加:  TAVILY_API_KEY=tvly-...
3. pip install tavily-python>=0.3.0
```

## 基礎 tool 實作

```python
# agent/tools/web_search.py
import os
from tavily import TavilyClient

_client = None
def _client_lazy():
    global _client
    if _client is None:
        _client = TavilyClient(api_key=os.environ["TAVILY_API_KEY"])
    return _client


def web_search(query: str, max_results: int = 5, search_depth: str = "basic") -> dict:
    """搜尋網路、回傳結構化結果。

    Args:
        query: 搜尋字串
        max_results: 1-10、預設 5
        search_depth: 'basic' (快、free tier 一次 1 credit) 或 'advanced' (深、一次 2 credits)

    Returns:
        {"query": str, "answer": str?(only with advanced), "results": [...]}
    """
    r = _client_lazy().search(
        query=query,
        max_results=min(max(max_results, 1), 10),
        search_depth=search_depth,
    )
    return {
        "query": query,
        "answer": r.get("answer"),
        "results": [
            {"title": x.get("title"), "url": x.get("url"),
             "content": (x.get("content") or "")[:500],  # cap each snippet
             "score": x.get("score")}
            for x in r.get("results", [])
        ],
    }


def register(registry):
    from agent.tool_registry import Tool
    registry.register(Tool(
        name="web_search",
        description="Search the web for current information. Use this when the user asks about news, recent events, or facts that may have changed.",
        parameters={
            "type": "object",
            "properties": {
                "query": {"type": "string"},
                "max_results": {"type": "integer", "minimum": 1, "maximum": 10},
                "search_depth": {"type": "string", "enum": ["basic", "advanced"]},
            },
            "required": ["query"],
        },
        func=web_search,
    ))
```

這版可以直接跑、但有 2 個 production 問題:**無 cache、無 rate limit**。下面補。

## 加 1h TTL cache

同樣的 query LLM 一輪可能呼好幾次(deduplicate 失敗時)、加 cache 一天可省一半 quota。簡單記憶體 cache 就夠:

```python
import time

_cache: dict[str, tuple[float, dict]] = {}
_CACHE_TTL_SEC = 3600  # 1 hour


def web_search(query: str, max_results: int = 5, search_depth: str = "basic") -> dict:
    cache_key = f"{search_depth}:{max_results}:{query.lower().strip()}"
    now = time.time()
    cached = _cache.get(cache_key)
    if cached and (now - cached[0]) < _CACHE_TTL_SEC:
        result = dict(cached[1])
        result["_cached"] = True
        return result

    r = _client_lazy().search(query=query, max_results=..., search_depth=...)
    result = {...}
    _cache[cache_key] = (now, result)
    return result
```

LLM 看到 `_cached: True` 可以判斷是新的還是舊的(在 prompt 寫「如果使用者明確說『重新搜』、改用 `search_depth=advanced` 強制 fresh」)。

## 加 per-user rate limit

防 LLM 失控連環搜 30 次燒光 quota:

```python
from collections import defaultdict

_usage: dict[int, list[float]] = defaultdict(list)  # user_id -> [timestamps]
_USER_LIMIT = 20            # max searches
_USER_WINDOW_SEC = 24 * 3600  # per day


def web_search(query: str, *, _user_id: int = 0, ...) -> dict:
    now = time.time()
    times = _usage[_user_id]
    # drop stale
    times[:] = [t for t in times if now - t < _USER_WINDOW_SEC]
    if len(times) >= _USER_LIMIT:
        return {
            "error": f"rate_limit:你今天已搜 {_USER_LIMIT} 次,等明天再來。",
            "next_available": min(times) + _USER_WINDOW_SEC,
        }
    times.append(now)
    # ... cache check then call Tavily ...
```

**接 `_user_id` 的問題**:LLM 不會自己傳。要從 TG `chat_id` / web `session_id` 衍生。

### ⛔ Anti-pattern:registry 盲目 inject 額外 kwargs

**不要這樣做**(看似簡單、但會破壞所有沒接該 kwarg 的 tool):

```python
# ❌ ❌ ❌
def wrapped(name, args):
    args["_user_id"] = user_id  # 強塞給每個 tool
    return original_run(name, args)
```

**為什麼炸**:`registry.run()` 最終呼 `func(**args)`,所有沒接 `_user_id` / `**kwargs` 的 tool 函數會丟:
```
TypeError: write_note() got an unexpected keyword argument '_user_id'
```

實戰回報過的案例:有人加了 per-user memory inject、結果**所有既有工具全炸**(`write_note` / `read_file` / `run_python` / Tavily ...),只能一個一個改加 `**kwargs`。

### ✅ 正確設計(三選一,由推薦到不推薦)

**A. ContextVar — 推薦**(無侵入、Python 標準庫):

```python
# agent/user_context.py
from contextvars import ContextVar
current_user_id: ContextVar[str] = ContextVar("current_user_id", default="anonymous")

# adapter / orchestrator step() 前 set:
from agent.user_context import current_user_id
current_user_id.set(chat_id)
# tools 內隨時拿:
from agent.user_context import current_user_id
def web_search(query, ...):
    user_id = current_user_id.get()
    # ... use it ...
```

優點:tool 函數 signature **完全不必改**;framework 跟 tools 解耦。

**B. 顯式 only-for-this-tool inject + whitelist**:

```python
# 只給 whitelisted tool 加 kwarg
INJECT_USER_TO = {"web_search", "remember_fact", "recall_episode"}
def wrapped(name, args):
    if name in INJECT_USER_TO:
        args["_user_id"] = user_id
    return original_run(name, args)
```

優點:只有「**設計時就決定要收 _user_id**」的 tool 收得到、其他 tool signature 安全。

**C. `func` introspection — 自動偵測**:

```python
import inspect
def wrapped(name, args):
    func = registry.get(name).func
    if "_user_id" in inspect.signature(func).parameters \
            or any(p.kind == inspect.Parameter.VAR_KEYWORD
                   for p in inspect.signature(func).parameters.values()):
        args["_user_id"] = user_id
    return original_run(name, args)
```

優點:自動正確;缺點:每 call 一次跑 inspect、稍慢。

**結論**:用 **A. ContextVar**。Phase 14 memory 系統也用同套機制拿 `current_user_id`,不必另外傳。

## Provider 切換(備案)

`assets/llm_client.py` 沒做、但你可以模仿 `make_llm()` 的 factory pattern 也做一個 `make_search_client(provider)`:

| Provider | Free tier | 特色 |
|---|---|---|
| **tavily** | 1000/月 | LLM-friendly snippets、relevance score |
| **brave** | 2000/月 | 隱私友善、no tracking |
| **serper** | 2500/搜尋 lifetime | 模擬 Google search、便宜 |
| **duckduckgo** | unlimited(無 key)| 完全免費、但結果 raw 沒 cleaned |

不建議全做、留個 stub `agent/tools/web_search.py` 後綴 `_provider="tavily"` 之類、使用者有需求再擴。

## 跟其他 phase 的關係

- Phase 9(channel prompts)在 system prompt 加「web_search 用 `{year}` 而非陳舊年份」(注入今日日期)
- Phase 12(self-evolution)agent 可能自己寫個 `_news_summary` tool 包 web_search、把多次搜尋合成成一份摘要

## 檢查清單

- [ ] Tavily key 進 .env、跑一次 `web_search("天氣")` 看是否回結果
- [ ] 同個 query 連跑兩次、第二次 result 有 `_cached: True`
- [ ] (可選)寫個 stress test:連跑 21 次、看第 21 次回 `error: rate_limit`
- [ ] 在 phase 9 channel prompt 提醒 LLM web_search 要帶當前年份
