"""Base types for agent tools.

This module is imported by both agent_tools and tool-specific modules
to avoid circular import dependencies.
"""

from __future__ import annotations

from typing import Any, Awaitable
from abc import ABC, abstractmethod
import json
from pydantic import BaseModel


class AgentContext:
    def __init__(
        self,
        *,
        chat_id: int | None = None,
        telegram_client: Any = None,
        cron_service: Any = None,
        depth: int = 0,
        max_depth: int = 2,
    ):
        self.telegram_client = telegram_client
        self.chat_id = chat_id
        self.cron_service = cron_service
        # Depth budget (Phase 0.1): how deep the agent tree may grow.
        # 0 = main agent; each spawn increments depth by one. With max_depth=2
        # the tree is main(0) -> generator(1) -> worker(2); the worker is a leaf.
        self.depth = depth
        self.max_depth = max_depth

    async def send_message(self, text: str) -> None:
        if not self.telegram_client or not self.chat_id:
            return
        payload = text.strip()
        if not payload:
            return
        await self.telegram_client.send_message(chat_id=self.chat_id, text=payload)

    def child_context(self) -> "AgentContext":
        """Fork a context for a spawned child: same services, depth + 1.

        Returns a *fresh* object rather than mutating ``self`` so concurrent
        sibling spawns can never clobber each other's depth.
        """
        return AgentContext(
            chat_id=self.chat_id,
            telegram_client=self.telegram_client,
            cron_service=self.cron_service,
            depth=self.depth + 1,
            max_depth=self.max_depth,
        )


class ToolResult(BaseModel):
    error: bool = False
    result: dict[str, Any]
    name: str

    def to_tool_message(self, id: int) -> dict[str, Any]:
        return {
            "name": self.name,
            "role": "tool",
            "content": json.dumps(self.result),
            "tool_call_id": id
        }


class AgentTool(ABC, BaseModel):
    @classmethod
    def tool_name(cls) -> str:
        return cls.__name__

    def tool_result(self, *, error: bool = False, result: dict[str, Any]) -> ToolResult:
        return ToolResult(name=self.__class__.tool_name(), error=error, result=result)

    @classmethod
    def to_schema(cls):
        model_schema = cls.model_json_schema()
        name = cls.tool_name()
        return {
            "type": "function",
            "function": {
                "name": name,
                "description": model_schema.get("description", f"call the {name} tool"),
                "parameters": {
                    "type": "object",
                    "properties": model_schema.get("properties", {}),
                    "required": model_schema.get("required", [])
                }
            }
        }

    @abstractmethod
    async def execute(self, context: AgentContext) -> ToolResult:
        """Override this in subclasses to define tool logic."""
        raise NotImplementedError
