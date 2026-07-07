"""Drop-in agent core. Copy to your project as agent/orchestrator.py + agent/tool_registry.py.

Usage:
    from agent.gemini_client import GeminiClient
    from agent.tool_registry import ToolRegistry, Tool
    from agent.orchestrator import Orchestrator

    registry = ToolRegistry()
    registry.register(Tool(
        name="add",
        description="Add two numbers.",
        parameters={
            "type": "object",
            "properties": {"a": {"type": "number"}, "b": {"type": "number"}},
            "required": ["a", "b"],
        },
        func=lambda a, b: {"sum": a + b},
    ))

    orch = Orchestrator(GeminiClient(), registry, system_prompt="You add numbers.")
    orch.add_user("what is 2 plus 3")
    for msg in orch.step():
        print(msg)
"""

# ============================================================
# tool_registry.py
# ============================================================
from dataclasses import dataclass
from typing import Any, Callable


@dataclass
class Tool:
    name: str
    description: str
    parameters: dict  # JSON-schema (type=object + properties)
    func: Callable

    def schema(self) -> dict:
        return {
            "name": self.name,
            "description": self.description,
            "parameters": self.parameters,
        }

    def run(self, args: dict) -> Any:
        try:
            raw = self.func(**(args or {}))
        except TypeError as e:
            return {"error": f"bad args: {e}"}
        except Exception as e:
            import traceback
            traceback.print_exc()  # host stderr — Phase 4 logging rule
            return {"error": str(e), "type": type(e).__name__}

        # Defense 4 (Phase 12): SDK requires dict — auto-wrap to be safe.
        if isinstance(raw, dict):
            return raw
        if raw is None:
            return {"ok": True}
        return {"result": raw}


class ToolRegistry:
    def __init__(self):
        self._tools: dict[str, Tool] = {}

    def register(self, tool: Tool):
        self._tools[tool.name] = tool

    def get(self, name: str):
        return self._tools.get(name)

    def schemas(self) -> list[dict]:
        return [t.schema() for t in self._tools.values()]

    def names(self) -> list[str]:
        return list(self._tools.keys())

    def run(self, name: str, args: dict) -> Any:
        t = self._tools.get(name)
        if t is None:
            return {"error": f"unknown tool: {name}"}
        return t.run(args)


# ============================================================
# orchestrator.py
# ============================================================
import json


class Orchestrator:
    """Single-turn planner: user message → LLM → tool calls → tool results → loop → final text."""

    def __init__(self, llm, registry: ToolRegistry, model: str = None,
                 max_iters: int = 10, system_prompt: str = ""):
        self.llm = llm
        self.registry = registry
        self.model = model
        self.max_iters = max_iters
        self.system_prompt = system_prompt
        self.messages: list[dict] = []
        if system_prompt:
            self.messages.append({"role": "system", "content": system_prompt})

    def reset(self):
        self.messages = []
        if self.system_prompt:
            self.messages.append({"role": "system", "content": self.system_prompt})

    def add_user(self, text: str):
        self.messages.append({"role": "user", "content": text})

    def step(self):
        """Yields each message produced (assistant + tool results) until final answer."""
        for _ in range(self.max_iters):
            resp = self.llm.chat(
                self.messages,
                tools=self.registry.schemas(),
                model=self.model,
            )

            text = getattr(resp, "text", "") or ""
            tool_calls = list(getattr(resp, "function_calls", []) or [])

            asst = {"role": "assistant", "content": text, "tool_calls": tool_calls}
            self.messages.append(asst)
            yield asst

            if not tool_calls:
                return  # done

            for tc in tool_calls:
                args = dict(tc.args) if getattr(tc, "args", None) else {}
                result = self.registry.run(tc.name, args)
                tool_msg = {
                    "role": "tool",
                    "tool_name": tc.name,
                    "content": json.dumps(result, default=str, ensure_ascii=False),
                }
                self.messages.append(tool_msg)
                yield tool_msg

        yield {
            "role": "assistant",
            "content": f"[max_iters={self.max_iters} hit; aborting loop]",
            "tool_calls": [],
        }
