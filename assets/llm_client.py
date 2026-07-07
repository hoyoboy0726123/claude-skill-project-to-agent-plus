"""Multi-provider LLM client. Drop into your project as agent/llm_client.py.

Supports 5 providers with a unified `chat(messages, tools, model)` interface:
  - gemini    (default; free tier on Google AI Studio)
  - groq      (free tier, fast)
  - openai    (gpt-4o-mini etc.)
  - anthropic (Claude family)
  - ollama    (local, no key; runs on http://localhost:11434)

Provider selection (in priority):
  1. Explicit constructor arg: make_llm(provider="groq")
  2. AGENT_LLM_PROVIDER env var
  3. Auto-detect from which API key is set in .env

Required packages depend on which providers you actually use:
  pip install google-genai>=2.0.0          # gemini
  pip install groq>=0.11                   # groq
  pip install openai>=1.50                 # openai (+ Ollama via base_url)
  pip install anthropic>=0.40              # anthropic

Common interface:
    client = make_llm()                                     # auto-pick
    resp = client.chat(messages, tools=tool_schemas)
    print(resp.text)
    for tc in resp.function_calls:
        print(tc.name, tc.args)
"""
from __future__ import annotations

import json
import os
import time
from dataclasses import dataclass, field
from typing import Any, Callable


# ─────────────────────────────────────────────────────────────
# Unified response shape
# ─────────────────────────────────────────────────────────────
@dataclass
class ToolCall:
    name: str
    args: dict


@dataclass
class LLMResponse:
    """All provider responses normalized into this shape."""
    text: str = ""
    function_calls: list[ToolCall] = field(default_factory=list)
    # raw provider response if caller needs anything else
    raw: Any = None


# ─────────────────────────────────────────────────────────────
# Common retry helper
# ─────────────────────────────────────────────────────────────
_RETRY_CODES = (429, 500, 502, 503, 504)
_RETRY_MARKERS = (
    "overloaded", "rate limit", "rate_limit", "ratelimit",
    "timeout", "timed out", "temporarily unavailable",
    "internal error",   # Gemini "500 INTERNAL ... Internal error encountered"
    "internal.",        # status: 'INTERNAL'
    "unavailable",
)
_BACKOFF = (3.0, 8.0, 20.0)  # 3 retries, total wait ~31s


def _is_retryable(e: Exception) -> bool:
    code = getattr(e, "code", None) or getattr(e, "status_code", None)
    if isinstance(code, int):
        if code in _RETRY_CODES:
            return True
        # 4xx (except 429) is client error — never retry
        if 400 <= code < 500:
            return False
    msg = str(e).lower()
    # Extract first 3-digit code occurrence and check it's not a hard 4xx
    import re
    m_code = re.search(r"\b([45]\d{2})\b", msg)
    if m_code:
        c = int(m_code.group(1))
        if c in _RETRY_CODES:
            return True
        if 400 <= c < 500:
            return False
    return any(m in msg for m in _RETRY_MARKERS)


def _retry(fn: Callable, max_attempts: int = 4):
    last = None
    for i in range(max_attempts):
        try:
            return fn()
        except Exception as e:
            last = e
            if i >= len(_BACKOFF) or not _is_retryable(e):
                raise
            time.sleep(_BACKOFF[i])
    if last:
        raise last


# ─────────────────────────────────────────────────────────────
# Friendly error translation (raised exceptions → user-readable hint)
# ─────────────────────────────────────────────────────────────
def friendly_error(e: Exception) -> str:
    msg = str(e)
    if "API_KEY" in msg or "api_key" in msg or "401" in msg or "authentication" in msg.lower():
        return "API key 沒設或無效;檢查 .env 對應的 *_API_KEY"
    if "404" in msg or "not found" in msg.lower():
        return "Model 找不到 — 名稱拼錯或該 provider 不支援、改一個 model 名"
    if "429" in msg or "rate" in msg.lower() or "quota" in msg.lower():
        return "Provider quota / rate limit 滿了,等 60 秒再試或切到別的 provider"
    if "timeout" in msg.lower():
        return "Provider 回應超時、可能網路慢或 model 卡死、重試"
    return msg[:200]


# ─────────────────────────────────────────────────────────────
# Provider: Gemini / Gemma (Google AI Studio)
# ─────────────────────────────────────────────────────────────
class GeminiClient:
    """google-genai>=2.0.0

    Context caching (Phase 13 / 對策 #2):
      - Set AGENT_GEMINI_CACHE=true in .env to enable.
      - On first chat() this client builds a context cache containing
        system_instruction + tools; subsequent chats reuse via cached_content.
      - Cache TTL is 1h (default); per-session signature change → rebuild.
      - If the model doesn't support caching (e.g. Gemma 4 on AI Studio),
        we catch once, log, and silently fall back to no-cache mode.
    """
    DEFAULT_MODEL = "gemma-4-31b-it"

    def __init__(self, api_key: str | None = None, default_model: str | None = None,
                 enable_cache: bool | None = None):
        try:
            from google import genai
            from google.genai import types as gtypes
        except ImportError:
            raise RuntimeError("google-genai not installed. pip install google-genai>=2.0.0")
        self._genai, self._gtypes = genai, gtypes
        self.client = genai.Client(api_key=api_key or os.environ["GEMINI_API_KEY"])
        self.default_model = default_model or os.environ.get("GEMINI_MODEL", self.DEFAULT_MODEL)

        # Caching state — populated on first chat() if enabled
        if enable_cache is None:
            enable_cache = os.environ.get("AGENT_GEMINI_CACHE", "").lower() in ("1", "true", "yes")
        self._cache_enabled: bool = bool(enable_cache)
        self._cache_supported: bool = True  # flips to False after first 4xx
        self._cache_name: str | None = None
        self._cache_signature: tuple | None = None  # (model, system_hash, tools_hash)
        self._cache_hits = 0
        self._cache_misses = 0

    def _build_or_reuse_cache(self, model: str, system: str | None,
                              tools_schemas: list[dict] | None) -> str | None:
        """Build or reuse a Gemini context cache. Returns cache.name or None.

        Cache contains system_instruction + tools (both static across the
        session). On signature change (registry reload, model swap), the
        cache is rebuilt; the old one expires by TTL (1h).
        """
        if not self._cache_enabled or not self._cache_supported:
            return None
        if not system and not tools_schemas:
            return None

        sys_hash = hash(system or "")
        tools_hash = hash(json.dumps(tools_schemas or [], sort_keys=True, default=str))
        signature = (model, sys_hash, tools_hash)
        if self._cache_name and self._cache_signature == signature:
            self._cache_hits += 1
            return self._cache_name

        # (Re)build
        gemini_tools_obj = None
        if tools_schemas:
            decls = [
                self._gtypes.FunctionDeclaration(
                    name=t["name"],
                    description=t.get("description", ""),
                    parameters_json_schema=t.get("parameters", {"type": "object"}),
                )
                for t in tools_schemas
            ]
            gemini_tools_obj = [self._gtypes.Tool(function_declarations=decls)]

        try:
            cache = self.client.caches.create(
                model=model,
                config=self._gtypes.CreateCachedContentConfig(
                    system_instruction=system or None,
                    tools=gemini_tools_obj,
                    ttl="3600s",
                ),
            )
            self._cache_name = getattr(cache, "name", None)
            self._cache_signature = signature
            self._cache_misses += 1
            return self._cache_name
        except Exception as e:
            msg = str(e)
            # Common: model doesn't support caching, or cached content too small
            # (Flash needs >= 4096 tokens, Pro needs >= 32768)
            print(f"[llm_client] context cache disabled — {msg[:200]}")
            self._cache_supported = False
            self._cache_name = None
            self._cache_signature = None
            return None

    def list_models(self, only_chat: bool = True,
                    skip_preview: bool = False) -> list[dict]:
        """List models reachable by this API key.

        Args:
            only_chat: filter to models with generateContent capability
                       (excludes embedding-only, tts-only, image-gen, video).
            skip_preview: hide models with 'preview' / 'experimental' in name.

        Returns: [{"name", "display_name", "methods": [...], "input_limit",
                  "output_limit", "supports_caching": bool}, ...]
        """
        try:
            raw = list(self.client.models.list())
        except Exception as e:
            return [{"error": str(e)}]
        out = []
        for m in raw:
            short = (m.name or "").split("/")[-1]
            methods = list(getattr(m, "supported_generation_methods", []) or [])
            if not methods:
                methods = list(getattr(m, "supported_actions", []) or [])
            if only_chat and "generateContent" not in methods:
                continue
            if skip_preview and ("preview" in short or "experimental" in short):
                continue
            out.append({
                "name": short,
                "display_name": getattr(m, "display_name", short) or short,
                "methods": methods,
                "input_limit": getattr(m, "input_token_limit", None),
                "output_limit": getattr(m, "output_token_limit", None),
                "supports_caching": "createCachedContent" in methods,
            })
        return sorted(out, key=lambda x: x["name"])

    def switch_model(self, model_name: str):
        """Change the active model. Invalidates context cache (the cache is
        per-model + signature; the next chat() will rebuild if caching enabled).
        """
        self.default_model = model_name
        # Reset cache state — old cache was bound to a specific model
        self._cache_name = None
        self._cache_signature = None

    def cache_stats(self) -> dict:
        return {
            "enabled": self._cache_enabled,
            "supported": self._cache_supported,
            "active_cache": self._cache_name,
            "hits": self._cache_hits,
            "misses": self._cache_misses,
        }

    def chat(self, messages: list[dict], tools: list[dict] | None = None,
             model: str | None = None,
             attachments: list[dict] | None = None) -> LLMResponse:
        """
        attachments: optional list of {"path": str, "mime": str} dicts. Each
        image's bytes are prepended (Gemma rule: image Part BEFORE text Part)
        to the LAST user-role Content in `contents`.
        """
        contents, system = self._convert(messages)

        # Gemma 4 vision: image parts MUST come before text in the same Content.
        # Find last user content; if attachments provided, insert image Parts first.
        if attachments and contents:
            for content in reversed(contents):
                if getattr(content, "role", None) == "user":
                    image_parts = []
                    for att in attachments:
                        try:
                            with open(att["path"], "rb") as f:
                                data = f.read()
                            image_parts.append(self._gtypes.Part.from_bytes(
                                data=data,
                                mime_type=att.get("mime") or "image/png",
                            ))
                        except Exception:
                            continue
                    if image_parts:
                        existing = list(content.parts or [])
                        content.parts = image_parts + existing
                    break

        model_to_use = model or self.default_model

        # ─── Try context caching first ──────────────────────────
        # When cache hit, system_instruction + tools are inside the cache,
        # so we DON'T re-pass them in GenerateContentConfig (would duplicate).
        cache_name = self._build_or_reuse_cache(model_to_use, system, tools)

        if cache_name:
            config = self._gtypes.GenerateContentConfig(
                cached_content=cache_name,
            )
        else:
            gemini_tools = None
            if tools:
                decls = [
                    self._gtypes.FunctionDeclaration(
                        name=t["name"],
                        description=t.get("description", ""),
                        parameters_json_schema=t.get("parameters", {"type": "object"}),
                    )
                    for t in tools
                ]
                gemini_tools = [self._gtypes.Tool(function_declarations=decls)]
            # Gemma 4 vision quirk: system_instruction must be non-empty when sending images
            sys_text = system or ("You are a helpful assistant." if attachments else None)
            config = self._gtypes.GenerateContentConfig(
                tools=gemini_tools,
                system_instruction=sys_text,
            )

        resp = _retry(lambda: self.client.models.generate_content(
            model=model_to_use, contents=contents, config=config,
        ))
        text = getattr(resp, "text", "") or ""
        calls = [ToolCall(name=tc.name, args=dict(tc.args or {}))
                 for tc in (getattr(resp, "function_calls", None) or [])]
        return LLMResponse(text=text, function_calls=calls, raw=resp)

    def chat_stream(self, messages: list[dict], tools: list[dict] | None = None,
                    model: str | None = None,
                    attachments: list[dict] | None = None):
        """Streaming variant of chat(). Yields incremental chunks:
            ("text", str)           — text token(s)
            ("tool_call", ToolCall) — emitted when a complete function call lands
            ("done", LLMResponse)   — final aggregated response at end of stream

        Gemini's stream API returns response chunks; each chunk may have text
        parts and/or function_call parts. We yield text incrementally so the UI
        can render token-by-token, then emit tool_calls + final response.
        """
        contents, system = self._convert(messages)

        if attachments and contents:
            for content in reversed(contents):
                if getattr(content, "role", None) == "user":
                    image_parts = []
                    for att in attachments:
                        try:
                            with open(att["path"], "rb") as f:
                                data = f.read()
                            image_parts.append(self._gtypes.Part.from_bytes(
                                data=data,
                                mime_type=att.get("mime") or "image/png",
                            ))
                        except Exception:
                            continue
                    if image_parts:
                        existing = list(content.parts or [])
                        content.parts = image_parts + existing
                    break

        model_to_use = model or self.default_model
        cache_name = self._build_or_reuse_cache(model_to_use, system, tools)

        if cache_name:
            config = self._gtypes.GenerateContentConfig(cached_content=cache_name)
        else:
            gemini_tools = None
            if tools:
                decls = [
                    self._gtypes.FunctionDeclaration(
                        name=t["name"],
                        description=t.get("description", ""),
                        parameters_json_schema=t.get("parameters", {"type": "object"}),
                    )
                    for t in tools
                ]
                gemini_tools = [self._gtypes.Tool(function_declarations=decls)]
            sys_text = system or ("You are a helpful assistant." if attachments else None)
            config = self._gtypes.GenerateContentConfig(
                tools=gemini_tools,
                system_instruction=sys_text,
            )

        # generate_content_stream returns iterator of chunks. We _retry the
        # FIRST chunk fetch (initial connect), once iteration starts streaming
        # errors mid-stream are surfaced directly.
        def _start_stream():
            return self.client.models.generate_content_stream(
                model=model_to_use, contents=contents, config=config,
            )
        stream = _retry(_start_stream)

        accumulated_text = []
        accumulated_calls = []
        last_chunk = None
        for chunk in stream:
            last_chunk = chunk
            # chunk.candidates[0].content.parts has text and/or function_call
            try:
                parts = chunk.candidates[0].content.parts or []
            except Exception:
                parts = []
            for part in parts:
                # text part
                text = getattr(part, "text", None)
                if text:
                    accumulated_text.append(text)
                    yield ("text", text)
                # function call part
                fc = getattr(part, "function_call", None)
                if fc and getattr(fc, "name", None):
                    tc = ToolCall(name=fc.name, args=dict(fc.args or {}))
                    accumulated_calls.append(tc)
                    yield ("tool_call", tc)

        # Final aggregated response
        final = LLMResponse(
            text="".join(accumulated_text),
            function_calls=accumulated_calls,
            raw=last_chunk,
        )
        yield ("done", final)

    def _convert(self, messages: list[dict]):
        contents, sys_parts = [], []
        for m in messages:
            role = m.get("role")
            text = m.get("content") or ""
            if role == "system":
                if text:
                    sys_parts.append(text)
                continue
            if role == "user":
                contents.append(self._gtypes.Content(
                    role="user", parts=[self._gtypes.Part(text=text)],
                ))
            elif role == "assistant":
                parts = []
                if text:
                    parts.append(self._gtypes.Part(text=text))
                for tc in m.get("tool_calls", []) or []:
                    name = tc.name if hasattr(tc, "name") else tc.get("name")
                    args = tc.args if hasattr(tc, "args") else tc.get("args", {})
                    parts.append(self._gtypes.Part(
                        function_call=self._gtypes.FunctionCall(name=name, args=dict(args or {})),
                    ))
                if parts:
                    contents.append(self._gtypes.Content(role="model", parts=parts))
            elif role == "tool":
                obj = _coerce_dict(text)
                contents.append(self._gtypes.Content(
                    role="tool",
                    parts=[self._gtypes.Part.from_function_response(
                        name=m.get("tool_name", ""), response=obj,
                    )],
                ))
        return contents, "\n\n".join(sys_parts)


# ─────────────────────────────────────────────────────────────
# Provider: OpenAI-compatible (OpenAI / Groq / Ollama share schema)
# ─────────────────────────────────────────────────────────────
class _OpenAICompatClient:
    """Base for OpenAI / Groq / Ollama (all use OpenAI SDK 兼容格式)."""

    def __init__(self, base_url: str | None = None, api_key: str | None = None,
                 default_model: str | None = None):
        try:
            from openai import OpenAI
        except ImportError:
            raise RuntimeError("openai not installed. pip install openai>=1.50")
        kw = {"api_key": api_key or "no-key-needed"}
        if base_url:
            kw["base_url"] = base_url
        self.client = OpenAI(**kw)
        self.default_model = default_model

    def chat(self, messages: list[dict], tools: list[dict] | None = None,
             model: str | None = None,
             attachments: list[dict] | None = None) -> LLMResponse:
        oai_messages = self._convert(messages)
        # OpenAI-compatible: inject image as content blocks on the last user msg
        if attachments and oai_messages:
            import base64
            for msg in reversed(oai_messages):
                if msg.get("role") == "user":
                    blocks = [{"type": "text", "text": msg.get("content") or ""}]
                    for att in attachments:
                        try:
                            with open(att["path"], "rb") as f:
                                b64 = base64.b64encode(f.read()).decode()
                            mime = att.get("mime") or "image/png"
                            blocks.insert(0, {
                                "type": "image_url",
                                "image_url": {"url": f"data:{mime};base64,{b64}"},
                            })
                        except Exception:
                            continue
                    msg["content"] = blocks
                    break

        oai_tools = None
        if tools:
            oai_tools = [
                {"type": "function",
                 "function": {
                     "name": t["name"],
                     "description": t.get("description", ""),
                     "parameters": t.get("parameters", {"type": "object"}),
                 }} for t in tools
            ]
        resp = _retry(lambda: self.client.chat.completions.create(
            model=model or self.default_model,
            messages=oai_messages,
            tools=oai_tools,
        ))
        choice = resp.choices[0].message
        text = choice.content or ""
        calls = []
        for tc in (choice.tool_calls or []):
            try:
                args = json.loads(tc.function.arguments or "{}")
            except json.JSONDecodeError:
                args = {}
            calls.append(ToolCall(name=tc.function.name, args=args))
        return LLMResponse(text=text, function_calls=calls, raw=resp)

    def _convert(self, messages: list[dict]):
        out = []
        for m in messages:
            role = m.get("role")
            text = m.get("content") or ""
            if role in ("system", "user"):
                out.append({"role": role, "content": text})
            elif role == "assistant":
                msg = {"role": "assistant", "content": text or None}
                tcs = m.get("tool_calls") or []
                if tcs:
                    msg["tool_calls"] = [
                        {"id": f"call_{i}",
                         "type": "function",
                         "function": {
                             "name": tc.name if hasattr(tc, "name") else tc.get("name"),
                             "arguments": json.dumps(
                                 tc.args if hasattr(tc, "args") else tc.get("args", {})),
                         }} for i, tc in enumerate(tcs)
                    ]
                out.append(msg)
            elif role == "tool":
                out.append({
                    "role": "tool",
                    "tool_call_id": m.get("tool_call_id", "call_0"),
                    "content": text,
                })
        return out


class OpenAIClient(_OpenAICompatClient):
    DEFAULT_MODEL = "gpt-4o-mini"

    def __init__(self, api_key: str | None = None, default_model: str | None = None):
        super().__init__(
            api_key=api_key or os.environ["OPENAI_API_KEY"],
            default_model=default_model or os.environ.get("OPENAI_MODEL", self.DEFAULT_MODEL),
        )

    def list_models(self, **kwargs) -> list[dict]:
        try:
            raw = list(self.client.models.list())
        except Exception as e:
            return [{"error": str(e)}]
        out = []
        for m in raw:
            mid = getattr(m, "id", "")
            # Skip embedding / image / audio models
            if not mid or any(t in mid for t in ("embedding", "dall-e", "tts", "whisper", "moderation")):
                continue
            out.append({
                "name": mid,
                "display_name": mid,
                "methods": ["generateContent"],
                "supports_caching": "gpt-4o" in mid or "gpt-4-turbo" in mid,
            })
        return sorted(out, key=lambda x: x["name"])

    def switch_model(self, model_name: str):
        self.default_model = model_name


class GroqClient(_OpenAICompatClient):
    DEFAULT_MODEL = "llama-3.3-70b-versatile"

    def __init__(self, api_key: str | None = None, default_model: str | None = None):
        super().__init__(
            base_url="https://api.groq.com/openai/v1",
            api_key=api_key or os.environ["GROQ_API_KEY"],
            default_model=default_model or os.environ.get("GROQ_MODEL", self.DEFAULT_MODEL),
        )

    def list_models(self, **kwargs) -> list[dict]:
        try:
            raw = list(self.client.models.list())
        except Exception as e:
            return [{"error": str(e)}]
        out = []
        for m in raw:
            mid = getattr(m, "id", "")
            if not mid or "whisper" in mid:
                continue
            out.append({
                "name": mid, "display_name": mid,
                "methods": ["generateContent"], "supports_caching": False,
            })
        return sorted(out, key=lambda x: x["name"])

    def switch_model(self, model_name: str):
        self.default_model = model_name


class OllamaClient(_OpenAICompatClient):
    """Local Ollama on http://localhost:11434 (or OLLAMA_HOST env)."""
    DEFAULT_MODEL = "qwen2.5:7b"

    def __init__(self, default_model: str | None = None):
        host = os.environ.get("OLLAMA_HOST", "http://localhost:11434")
        self.host = host.rstrip("/")
        super().__init__(
            base_url=f"{self.host}/v1",
            api_key="ollama",  # ollama ignores key
            default_model=default_model or os.environ.get("OLLAMA_MODEL", self.DEFAULT_MODEL),
        )

    def list_models(self, **kwargs) -> list[dict]:
        """List locally-pulled Ollama models via /api/tags."""
        import requests
        try:
            r = requests.get(f"{self.host}/api/tags", timeout=3)
            r.raise_for_status()
            data = r.json()
        except Exception as e:
            return [{"error": f"Ollama 連不上 ({self.host}): {e}"}]
        models = data.get("models", [])
        out = []
        for m in models:
            name = m.get("name") or m.get("model") or ""
            details = m.get("details") or {}
            out.append({
                "name": name,
                "display_name": name,
                "methods": ["generateContent"],
                "input_limit": None,
                "output_limit": None,
                "supports_caching": False,
                "size_bytes": m.get("size"),
                "parameter_size": details.get("parameter_size"),
                "quantization": details.get("quantization_level"),
            })
        return sorted(out, key=lambda x: x["name"])

    def switch_model(self, model_name: str):
        self.default_model = model_name


# ─────────────────────────────────────────────────────────────
# Provider: Anthropic
# ─────────────────────────────────────────────────────────────
class AnthropicClient:
    """anthropic>=0.40"""
    DEFAULT_MODEL = "claude-haiku-4-5-20251001"

    def __init__(self, api_key: str | None = None, default_model: str | None = None):
        try:
            from anthropic import Anthropic
        except ImportError:
            raise RuntimeError("anthropic not installed. pip install anthropic>=0.40")
        self.client = Anthropic(api_key=api_key or os.environ["ANTHROPIC_API_KEY"])
        self.default_model = default_model or os.environ.get("ANTHROPIC_MODEL", self.DEFAULT_MODEL)

    def list_models(self, **kwargs) -> list[dict]:
        # Anthropic doesn't have a list_models API; hardcode well-known models
        known = [
            "claude-opus-4-7-20251101", "claude-opus-4-6-20250915",
            "claude-sonnet-4-6-20250914", "claude-haiku-4-5-20251001",
            "claude-3-7-sonnet-20250219", "claude-3-5-sonnet-20241022",
            "claude-3-5-haiku-20241022",
        ]
        return [{"name": n, "display_name": n, "methods": ["generateContent"],
                  "supports_caching": True} for n in known]

    def switch_model(self, model_name: str):
        self.default_model = model_name

    def chat(self, messages: list[dict], tools: list[dict] | None = None,
             model: str | None = None,
             attachments: list[dict] | None = None) -> LLMResponse:
        anth_messages, system_text = self._convert(messages)
        # Anthropic vision: image blocks on the last user message
        if attachments and anth_messages:
            import base64
            for msg in reversed(anth_messages):
                if msg.get("role") == "user":
                    existing = msg.get("content")
                    if isinstance(existing, str):
                        blocks = [{"type": "text", "text": existing}]
                    elif isinstance(existing, list):
                        blocks = list(existing)
                    else:
                        blocks = []
                    image_blocks = []
                    for att in attachments:
                        try:
                            with open(att["path"], "rb") as f:
                                b64 = base64.b64encode(f.read()).decode()
                            image_blocks.append({
                                "type": "image",
                                "source": {
                                    "type": "base64",
                                    "media_type": att.get("mime") or "image/png",
                                    "data": b64,
                                },
                            })
                        except Exception:
                            continue
                    msg["content"] = image_blocks + blocks
                    break

        anth_tools = None
        if tools:
            anth_tools = [
                {"name": t["name"],
                 "description": t.get("description", ""),
                 "input_schema": t.get("parameters", {"type": "object"})}
                for t in tools
            ]
        kw = {
            "model": model or self.default_model,
            "max_tokens": 4096,
            "messages": anth_messages,
        }
        if system_text:
            kw["system"] = system_text
        if anth_tools:
            kw["tools"] = anth_tools
        resp = _retry(lambda: self.client.messages.create(**kw))
        text_parts = []
        calls = []
        for block in resp.content:
            if block.type == "text":
                text_parts.append(block.text)
            elif block.type == "tool_use":
                calls.append(ToolCall(name=block.name, args=dict(block.input or {})))
        return LLMResponse(text="".join(text_parts), function_calls=calls, raw=resp)

    def _convert(self, messages: list[dict]):
        sys_parts = []
        out = []
        for m in messages:
            role = m.get("role")
            text = m.get("content") or ""
            if role == "system":
                if text:
                    sys_parts.append(text)
                continue
            if role == "user":
                out.append({"role": "user", "content": text})
            elif role == "assistant":
                content_blocks = []
                if text:
                    content_blocks.append({"type": "text", "text": text})
                for i, tc in enumerate(m.get("tool_calls") or []):
                    content_blocks.append({
                        "type": "tool_use",
                        "id": f"toolu_{i}",
                        "name": tc.name if hasattr(tc, "name") else tc.get("name"),
                        "input": tc.args if hasattr(tc, "args") else tc.get("args", {}),
                    })
                out.append({"role": "assistant", "content": content_blocks})
            elif role == "tool":
                # Anthropic expects tool_result inside a user message
                out.append({
                    "role": "user",
                    "content": [{
                        "type": "tool_result",
                        "tool_use_id": m.get("tool_call_id", "toolu_0"),
                        "content": text,
                    }],
                })
        return out, "\n\n".join(sys_parts)


# ─────────────────────────────────────────────────────────────
# Factory
# ─────────────────────────────────────────────────────────────
def make_llm(provider: str | None = None, **kwargs) -> Any:
    """Create an LLM client. Picks provider from arg → env → auto-detect."""
    provider = (provider or os.environ.get("AGENT_LLM_PROVIDER") or "").lower().strip()
    if not provider:
        if os.environ.get("GEMINI_API_KEY"):
            provider = "gemini"
        elif os.environ.get("GROQ_API_KEY"):
            provider = "groq"
        elif os.environ.get("OPENAI_API_KEY"):
            provider = "openai"
        elif os.environ.get("ANTHROPIC_API_KEY"):
            provider = "anthropic"
        else:
            provider = "ollama"

    if provider in ("gemini", "gemma", "google"):
        return GeminiClient(**kwargs)
    if provider == "groq":
        return GroqClient(**kwargs)
    if provider == "openai":
        return OpenAIClient(**kwargs)
    if provider in ("anthropic", "claude"):
        return AnthropicClient(**kwargs)
    if provider == "ollama":
        return OllamaClient(**kwargs)
    raise ValueError(f"Unknown LLM provider: {provider!r}")


# ─────────────────────────────────────────────────────────────
# Helpers
# ─────────────────────────────────────────────────────────────
def _coerce_dict(text: str) -> dict:
    if not text:
        return {"result": ""}
    try:
        v = json.loads(text)
        return v if isinstance(v, dict) else {"result": v}
    except (json.JSONDecodeError, TypeError):
        return {"result": text}
