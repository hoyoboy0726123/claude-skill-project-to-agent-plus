"""Planner loop. ONE Orchestrator instance per chat — messages are instance state.

Architecture:
    user message → llm.chat(messages, tools) → if tool_calls → run them → loop
                                            → else → final reply (+ Phase 6 check)

Phase 5 (two-step write): the agent is expected to call write tools with
confirm=False first (preview), wait for user confirmation, then call with
confirm=True. The orchestrator does not enforce this — the system prompt
(Phase 9) is what tells the LLM to follow the protocol. The hallucination
detector below catches the failure mode where the LLM *claims* to have done
something but never actually called confirm=True.
"""
from __future__ import annotations

import json
from typing import Generator

# Phrases the agent might write to falsely claim a side effect happened.
_CLAIM_MARKERS = (
    "已套用", "已寫入", "已建立", "已執行", "已排程", "已刪除",
    "已 commit", "已 push", "已儲存", "已更新",
    "套用完成", "寫入完成", "完成寫入", "已成功寫入",
)


class Orchestrator:
    """Stateful planner loop. Per-chat — never share across Telegram sessions."""

    def __init__(self, llm, registry, system_prompt: str = "",
                 model: str | None = None,
                 *,
                 chat_history_cap: int = 30,
                 tool_result_max: int = 16000,
                 max_iters: int = 10):
        self.llm = llm
        self.registry = registry
        self.system_prompt = system_prompt
        self.model = model
        self.chat_history_cap = chat_history_cap
        self.tool_result_max = tool_result_max
        self.max_iters = max_iters
        self.messages: list[dict] = []
        # Image attachments to inject into the next llm.chat() call
        # (consumed on first use). Populated by add_user(attachments=[...])
        # or by tools that return {"_image_attachment": {...}}.
        self._pending_attachments: list[dict] = []
        if system_prompt:
            self.messages.append({"role": "system", "content": system_prompt})

    # ─── public API ────────────────────────────────────────────
    def reset(self):
        self.messages = []
        self._pending_attachments = []
        if self.system_prompt:
            self.messages.append({"role": "system", "content": self.system_prompt})

    def add_user(self, text: str, attachments: list[dict] | None = None):
        """Add a user message, optionally with image attachments.

        attachments: [{"path": str, "mime": str}, ...] — images sent to LLM
        on the next chat call. Used by TelegramAdapter when user sends a photo.
        """
        self.messages.append({"role": "user", "content": text})
        if attachments:
            self._pending_attachments.extend(attachments)
        self._trim_history()

    def step_stream(self, stream_tokens: bool = True) -> Generator[dict, None, None]:
        """Streaming variant of step(). Yields additional dict shapes:
          {"role": "assistant_chunk", "content": "<partial text>"}  ← token-by-token

        plus the same shapes as step():
          {"role": "assistant", "content": "...full text...", "tool_calls": [...]}
          {"role": "tool", "tool_name": "...", "content": "<json>"}

        Falls back to non-streaming behaviour if the LLM client has no
        chat_stream method.
        """
        if not hasattr(self.llm, "chat_stream") or not stream_tokens:
            yield from self.step()
            return

        turn_tool_calls: list = []
        for _ in range(self.max_iters):
            attachments = self._pending_attachments or None
            self._pending_attachments = []
            sanitized = self._strip_orphan_tool_calls(self.messages)
            messages_to_send = self._inject_dynamic_context(sanitized)

            accumulated_text = ""
            tool_calls = []
            final_resp = None

            for kind, payload in self.llm.chat_stream(
                messages_to_send,
                tools=self.registry.schemas(),
                model=self.model,
                attachments=attachments,
            ):
                if kind == "text":
                    accumulated_text += payload
                    yield {"role": "assistant_chunk", "content": payload}
                elif kind == "tool_call":
                    tool_calls.append(payload)
                elif kind == "done":
                    final_resp = payload

            # Push the final consolidated assistant turn for downstream consumers
            # (so logic that expects step()-style messages still works)
            asst = {"role": "assistant", "content": accumulated_text,
                    "tool_calls": tool_calls}
            self.messages.append(asst)
            yield asst

            if not tool_calls:
                if (_claims_side_effect(accumulated_text)
                        and not _had_confirm_true(turn_tool_calls)):
                    warning = (
                        "[hallucination guard] 模型口頭聲稱已執行某操作、但本輪沒有"
                        "任何 confirm=True 的工具呼叫被執行。請使用者再說一次具體"
                        "指令(例如「請真的寫入」),我會重新跑工具真正執行。\n\n"
                        "------ 原回覆 ------\n"
                    )
                    yield {
                        "role": "assistant",
                        "content": warning + accumulated_text,
                        "tool_calls": [],
                        "_hallucination_warning": True,
                    }
                return

            saw_done = False
            saw_ask_user = False
            for tc in tool_calls:
                turn_tool_calls.append(tc)
                name = tc.name if hasattr(tc, "name") else tc.get("name")
                args = tc.args if hasattr(tc, "args") else tc.get("args", {})
                result = self.registry.run(name, dict(args or {}))
                if isinstance(result, dict):
                    att = result.get("_image_attachment")
                    if att:
                        self._pending_attachments.append(att)
                result_str = json.dumps(result, default=str, ensure_ascii=False)
                if len(result_str) > self.tool_result_max:
                    result_str = (
                        result_str[: self.tool_result_max]
                        + f"\n... [truncated, total {len(result_str)} chars]"
                    )
                tool_msg = {"role": "tool", "tool_name": name,
                            "content": result_str}
                self.messages.append(tool_msg)
                yield tool_msg
                if name == "done":
                    saw_done = True
                elif name == "ask_user":
                    saw_ask_user = True

            self._trim_history()
            if saw_done or saw_ask_user:
                return

        yield {
            "role": "assistant",
            "content": f"[max_iters={self.max_iters} hit; aborting loop]",
            "tool_calls": [],
        }

    def step(self) -> Generator[dict, None, None]:
        """Yield each produced message dict.

        Each yielded dict has one of these shapes:
          {"role": "assistant", "content": "...", "tool_calls": [...]}
          {"role": "tool", "tool_name": "...", "content": "<json string>"}
        The TG adapter (Phase 8) consumes this stream to push progress.
        """
        # Track which tool_calls happened in THIS step() invocation only,
        # so the hallucination check measures the current turn, not history.
        turn_tool_calls: list = []

        for _ in range(self.max_iters):
            # Consume any pending image attachments; they get injected into the
            # latest user message inside the LLM client (multi-modal Part).
            attachments = self._pending_attachments or None
            self._pending_attachments = []

            # Sanitize: Gemini rejects a chat that has function_call without
            # a paired function_response RIGHT after. This happens when:
            #   - previous turn made tool_calls but the loop bailed before
            #     persisting tool results
            #   - history was loaded from sqlite but some tool rows are missing
            # Strip any assistant message with tool_calls that isn't followed
            # by tool messages.
            sanitized = self._strip_orphan_tool_calls(self.messages)

            # Inject dynamic context (today's date + vault status) as a prefix
            # on the last user message. Sending sanitized plain would make
            # the LLM blind to the current date. We don't mutate self.messages
            # (history shouldn't repeat stale [today...] markers); only the
            # outgoing copy gets the prefix.
            messages_to_send = self._inject_dynamic_context(sanitized)

            resp = self.llm.chat(
                messages_to_send,
                tools=self.registry.schemas(),
                model=self.model,
                attachments=attachments,
            )
            text = getattr(resp, "text", "") or ""
            tool_calls = list(getattr(resp, "function_calls", None) or [])

            asst = {"role": "assistant", "content": text, "tool_calls": tool_calls}
            self.messages.append(asst)
            yield asst

            if not tool_calls:
                # Phase 6 — hallucination check
                if _claims_side_effect(text) and not _had_confirm_true(turn_tool_calls):
                    warning = (
                        "[hallucination guard] 模型口頭聲稱已執行某操作、但本輪沒有"
                        "任何 confirm=True 的工具呼叫被執行。請使用者再說一次具體"
                        "指令(例如「請真的寫入」),我會重新跑工具真正執行。\n\n"
                        "------ 原回覆 ------\n"
                    )
                    yield {
                        "role": "assistant",
                        "content": warning + text,
                        "tool_calls": [],
                        "_hallucination_warning": True,
                    }
                return

            # Run every tool call, append truncated results
            saw_done = False
            saw_ask_user = False
            for tc in tool_calls:
                turn_tool_calls.append(tc)
                name = tc.name if hasattr(tc, "name") else tc.get("name")
                args = tc.args if hasattr(tc, "args") else tc.get("args", {})
                result = self.registry.run(name, dict(args or {}))

                # view_image (and any future image-producing tool) returns
                # {"_image_attachment": {"path": ..., "mime": ...}} — buffer it
                # for the NEXT chat call so the LLM actually "sees" the image.
                if isinstance(result, dict):
                    att = result.get("_image_attachment")
                    if att:
                        self._pending_attachments.append(att)

                result_str = json.dumps(result, default=str, ensure_ascii=False)
                if len(result_str) > self.tool_result_max:
                    result_str = (
                        result_str[: self.tool_result_max]
                        + f"\n... [truncated, total {len(result_str)} chars]"
                    )
                tool_msg = {
                    "role": "tool",
                    "tool_name": name,
                    "content": result_str,
                }
                self.messages.append(tool_msg)
                yield tool_msg
                if name == "done":
                    saw_done = True
                elif name == "ask_user":
                    saw_ask_user = True

            self._trim_history()

            # Phase 10/12 — `done` / `ask_user` end the planner loop early.
            # Without this, LLM would loop one more time after calling done.
            if saw_done or saw_ask_user:
                return

        yield {
            "role": "assistant",
            "content": f"[max_iters={self.max_iters} hit; aborting loop]",
            "tool_calls": [],
        }

    # ─── internals ─────────────────────────────────────────────
    def _strip_orphan_tool_calls(self, messages: list[dict]) -> list[dict]:
        """Aggressively sanitize history so Gemini won't reject it.

        Gemini requires a strict pattern:
          user → assistant(maybe tool_calls) → tool(per tool_call) → assistant → ...

        Common violations seen in production:
          (a) assistant with tool_calls but next msg ISN'T tool   (orphan call)
          (b) tool msg but previous ISN'T assistant with tool_calls  (orphan result)
          (c) consecutive user msgs (e.g. after a failed turn we re-added)
          (d) consecutive assistant msgs

        Strategy:
          - Drop orphan tool results entirely
          - Drop tool_calls from assistant turns not followed by tool
          - Drop empty assistant turns
          - Collapse consecutive user/assistant pairs (keep last)
        Then verify result is well-formed; if a violation remains, keep stripping.
        """
        if not messages:
            return messages

        def _pass_once(msgs: list[dict]) -> list[dict]:
            out: list[dict] = []
            for i, m in enumerate(msgs):
                role = m.get("role")
                if role == "assistant" and m.get("tool_calls"):
                    # (a) check next is tool
                    next_is_tool = (i + 1 < len(msgs)
                                    and msgs[i + 1].get("role") == "tool")
                    if next_is_tool:
                        out.append(m)
                    else:
                        cleaned = dict(m)
                        cleaned.pop("tool_calls", None)
                        if cleaned.get("content"):
                            out.append(cleaned)
                        # else drop empty assistant
                elif role == "tool":
                    # (b) check previous is assistant with tool_calls
                    prev_ok = (out and out[-1].get("role") == "assistant"
                               and out[-1].get("tool_calls"))
                    if prev_ok:
                        out.append(m)
                    # else: drop orphan tool result silently
                else:
                    out.append(m)
            return out

        def _collapse_consecutive_same_role(msgs: list[dict]) -> list[dict]:
            # (c)(d) — keep last of consecutive user/assistant
            out: list[dict] = []
            for m in msgs:
                if out and out[-1].get("role") == m.get("role") \
                        and m.get("role") in ("user", "assistant") \
                        and not out[-1].get("tool_calls") \
                        and not m.get("tool_calls"):
                    # Replace with newer one
                    out[-1] = m
                else:
                    out.append(m)
            return out

        # Run multiple passes until stable (deletes might create new orphans)
        cur = list(messages)
        for _ in range(5):
            after_a = _pass_once(cur)
            after_b = _collapse_consecutive_same_role(after_a)
            if after_b == cur:
                break
            cur = after_b

        # (e) Drop leading non-user — Gemini requires sequences start with user
        while cur and cur[0].get("role") != "user":
            cur.pop(0)

        # (f) Drop trailing empty-assistant (no content, no tool_calls) — would
        # cause Gemini to reject "incomplete turn" on the next request.
        while cur and cur[-1].get("role") == "assistant" \
                and not cur[-1].get("tool_calls") \
                and not cur[-1].get("content"):
            cur.pop()

        return cur

    def _inject_dynamic_context(self, messages: list[dict]) -> list[dict]:
        """Return a SHALLOW copy of messages with dynamic context prefix on
        the most recent user message. Doesn't mutate self.messages so the
        history stays clean (no stale [今日 ...] markers in old turns).
        """
        # Lazy import to avoid circular at module import
        from agent.system_prompt import build_dynamic_context

        ctx = build_dynamic_context()
        if not ctx:
            return messages

        out = list(messages)
        for i in range(len(out) - 1, -1, -1):
            if out[i].get("role") == "user":
                m = dict(out[i])
                m["content"] = f"{ctx}\n\n{m.get('content') or ''}"
                out[i] = m
                break
        return out

    def _trim_history(self):
        """Keep system + last (cap) messages; older user/assistant/tool drop."""
        if len(self.messages) <= self.chat_history_cap + 1:
            return
        sys_msg = (
            self.messages[0]
            if self.messages and self.messages[0].get("role") == "system"
            else None
        )
        keep = self.messages[-self.chat_history_cap:]
        self.messages = ([sys_msg] if sys_msg else []) + keep


def _claims_side_effect(text: str) -> bool:
    if not text:
        return False
    return any(m in text for m in _CLAIM_MARKERS)


def _had_confirm_true(turn_calls: list) -> bool:
    for tc in turn_calls:
        args = tc.args if hasattr(tc, "args") else tc.get("args", {})
        if (args or {}).get("confirm") is True:
            return True
    return False
