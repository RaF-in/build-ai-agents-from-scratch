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
    def __init__(self, *, chat_id: int | None = None, telegram_client: Any = None, cron_service: Any = None):
        self.telegram_client = telegram_client
        self.chat_id = chat_id
        self.cron_service = cron_service

    async def send_message(self, text: str) -> None:
        if not self.telegram_client or not self.chat_id:
            return
        payload = text.strip()
        if not payload:
            return
        await self.telegram_client.send_message(chat_id=self.chat_id, text=payload)


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
