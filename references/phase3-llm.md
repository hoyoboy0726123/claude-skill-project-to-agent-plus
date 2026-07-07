# Phase 3 — LLM setup(multi-provider)

## 目標

讓 agent 接 LLM。skill 提供的 `assets/llm_client.py` 是一個 **factory pattern multi-provider client**、跨 5 個 provider 共用同一個 `chat(messages, tools, model)` 介面。

## 5 個支援的 provider

| Provider | Default model | 為什麼選它 | 取 key |
|---|---|---|---|
| **gemini** (預設) | `gemma-4-31b-it` | 免費 tier、Gemma 4 支援 tool calling + vision | https://aistudio.google.com/apikey |
| **groq** | `llama-3.3-70b-versatile` | 速度極快、免費 tier 大方、好 debug | https://console.groq.com/keys |
| **openai** | `gpt-4o-mini` | 最熟悉、tool calling 最穩定 | https://platform.openai.com/api-keys |
| **anthropic** | `claude-haiku-4-5` | Claude 對長 system prompt 跟複雜推理友善 | https://console.anthropic.com/settings/keys |
| **ollama** | `qwen2.5:7b` | 本地、無 key、無 quota、隱私敏感場景 | (本機跑 `ollama serve`) |

**先讓使用者選一個。** 預設建議 Gemini(完全免費、不必信用卡)。其他保留為 fallback、隨時可以切。

## 安裝

依照使用者選的 provider 裝 SDK(`llm_client.py` 用 lazy import,沒裝的 provider 不影響其他):

```bash
# 預設(Gemini)
pip install google-genai>=2.0.0 python-dotenv

# 加 Groq / OpenAI / Ollama 任一(共用 openai SDK)
pip install openai>=1.50

# 加 Anthropic
pip install anthropic>=0.40
```

## 設定 .env

```dotenv
# 至少設一個。第一個有設 key 的 provider 會被 auto-detected
GEMINI_API_KEY=AIza...

# 可選:強制 provider(蓋掉 auto-detect)
# AGENT_LLM_PROVIDER=gemini

# 可選:override model
# GEMINI_MODEL=gemini-2.5-flash
```

**`.env` 進 `.gitignore` — 先做完這步再 commit、絕對不要把 key 推上去。**

## 統一介面

不管 provider 是誰、code 長得一樣:

```python
from agent.llm_client import make_llm

client = make_llm()  # auto-pick by env
# 或顯式: client = make_llm(provider="groq")

resp = client.chat(
    messages=[
        {"role": "system", "content": "You are a helpful agent."},
        {"role": "user", "content": "say hi in one word"},
    ],
    tools=None,  # 之後 Phase 4 會帶上 tool schema
)
print(resp.text)
for tc in resp.function_calls:
    print(tc.name, tc.args)
```

`LLMResponse` shape 統一:
- `resp.text`(string)
- `resp.function_calls`(list of `ToolCall(name, args)`)
- `resp.raw`(provider 原始 response、需要 stream / token usage 等高階用途才看)

## Retry / timeout

`llm_client.py` 自帶 transient error retry:
- backoff `[3s, 8s, 20s]`(總共 ~31s,3 次重試)
- 觸發條件:HTTP 429 / 500 / 502 / 503 / 504、或 error msg 含 `overloaded` / `timeout` 等 marker
- 非 retryable error(401 invalid key、404 model 不存在)立即 raise

## Friendly error

`llm_client.py` export `friendly_error(e)`,把常見 exception 翻成繁中:

```python
try:
    resp = client.chat(messages)
except Exception as e:
    user_msg = friendly_error(e)  # "API key 沒設或無效" etc.
    print(f"⚠ {user_msg}")
```

把這個串進 Phase 8 的 TG adapter、使用者看到的就是友善訊息、不是 raw stacktrace。

## 連線測試

裝完 + key 進 .env 後跑:

```python
from dotenv import load_dotenv; load_dotenv()
from agent.llm_client import make_llm

c = make_llm()
print(c.chat([{"role": "user", "content": "say hi"}]).text)
```

噴 "Hi" 之類 → Phase 3 過。常見錯誤:

- **`KeyError: 'GEMINI_API_KEY'`** → `.env` 沒讀進來(忘了 `load_dotenv()`)或 key 名寫錯
- **`401 / API_KEY_INVALID`** → key 拼錯或過期、回 console 重產
- **`404 / model not found`** → model 名拼錯(Gemini 是 `gemma-4-31b-it`、Groq 是 `llama-3.3-70b-versatile`)
- **`429 / rate limit`** → quota 滿、retry 也救不回、等 60s 或切 provider

## Gemma 4 vision 特殊規矩(只用 vision 才相關)

`llm_client.py` 不直接提供 vision helper、但 Gemma 4 用 vision 有兩個 quirk:

1. Image `Part` **必須在** text `Part` 之前
2. `system_instruction` 必須非空(空字串 / None 會 vision miss)

要做 vision 的話照這個寫:

```python
from google.genai import types as gt
# images 在前、text 在後
parts = [gt.Part.from_bytes(data=img_bytes, mime_type="image/png"),
         gt.Part(text="這張圖在幹嘛?")]
resp = c.client.models.generate_content(
    model="gemma-4-31b-it",
    contents=[gt.Content(role="user", parts=parts)],
    config=gt.GenerateContentConfig(system_instruction="You are a vision assistant."),
)
```

Gemini 2.x / 3.x 跟 OpenAI / Anthropic 都沒這毛病、用標準 multi-modal API 就好。

## 跟 Phase 4 / 9 的關係

- Phase 4(agent core)用 `make_llm()` 拿 client、丟進 `Orchestrator`
- Phase 9(channel prompts)動態組裝 system prompt、`chat(messages, ...)` 第一條 `{"role": "system"}`
- Phase 6(hallucination 偵測)會 inspect 每輪的 `function_calls`、所以用統一的 `ToolCall` shape 很重要、跨 provider 偵測規則一致

## 檢查清單

- [ ] 使用者選了哪個 provider、為什麼(主要 use case 講清楚)
- [ ] `.env` 至少設了一個 `*_API_KEY`,且 `.env` 進 `.gitignore`
- [ ] SDK 裝了(Gemini 必裝、其他依照選擇)
- [ ] 連線測試噴對的字、沒有 exception
- [ ] retry / friendly_error 兩個 helper 跑過至少一次(可以故意打錯 key 看 friendly_error 翻什麼)
