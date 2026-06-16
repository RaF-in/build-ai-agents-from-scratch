"""Base types for agent tools.

This module is imported by both agent_tools and tool-specific modules
to avoid circular import dependencies.
"""

from __future__ import annotations

from typing import Any, Awaitable, Protocol, runtime_checkable
from abc import ABC, abstractmethod
from dataclasses import dataclass, field
import asyncio
import json
import time
from pydantic import BaseModel


@dataclass
class RunBudget:
    """Total safety rail for one run_pipeline() call (Phase 0.6).

    Distinct from the per-turn tool-call cap (Phase 0.3) and the per-spawn
    timeout (Phase 0.4): this bounds the WHOLE run — every tool call across every
    phase and worker — by both a call count and a wall-clock ceiling. One
    RunBudget is shared by reference down the whole agent tree (carried through
    ``child_context``), so a fanned-out generator and its workers all draw from
    the same counter. ``run_tool`` checks it BEFORE dispatch; when exhausted the
    run wraps up with partial results instead of crashing. ``None`` on the main
    agent's context — ordinary Q&A is never budget-stopped.
    """
    max_tool_calls: int = 120
    max_wall_clock_s: int = 1800
    started_at: float = field(default_factory=time.time)
    calls: int = 0

    def exhausted(self) -> bool:
        return (
            self.calls >= self.max_tool_calls
            or (time.time() - self.started_at) > self.max_wall_clock_s
        )


@runtime_checkable
class RunPolicy(Protocol):
    """Per-run behavior socket (Phase 0.7), consulted by AgentRuntime each turn.

    Lets a run customize three things WITHOUT the kernel learning the capability:
      * ``active_tools`` — hide tools for this run (e.g. plan-mode = read-only).
      * ``system_prompt`` — wrap or extend the base prompt.
      * ``on_idle`` — when the model emits no tool calls, decide whether the run
        is truly done (``False``) or must keep looping because work remains
        (``True``), e.g. open todos. This is the article's completion gate.

    ``DefaultPolicy`` reproduces today's exact behavior, so the main agent and any
    capability that leaves ``context.policy`` None is byte-for-byte unchanged.
    """
    def active_tools(self, all_tools: list[Any]) -> list[Any]: ...
    def system_prompt(self, base: str) -> str: ...
    async def on_idle(self, context: "AgentContext") -> bool: ...


class DefaultPolicy:
    """The no-op policy == current kernel behavior (Phase 0.7)."""
    def active_tools(self, all_tools: list[Any]) -> list[Any]:
        return all_tools

    def system_prompt(self, base: str) -> str:
        return base

    async def on_idle(self, context: "AgentContext") -> bool:
        return False


# Shared singleton: the runtime falls back to this whenever context.policy is None.
DEFAULT_POLICY = DefaultPolicy()


class AgentContext:
    def __init__(
        self,
        *,
        chat_id: int | None = None,
        telegram_client: Any = None,
        cron_service: Any = None,
        depth: int = 0,
        max_depth: int = 2,
        workspace_dir: str | None = None,
        run_semaphore: asyncio.Semaphore | None = None,
        run_budget: "RunBudget | None" = None,
        todo_list: Any = None,
        policy: "RunPolicy | None" = None,
    ):
        self.telegram_client = telegram_client
        self.chat_id = chat_id
        self.cron_service = cron_service
        # Depth budget (Phase 0.1): how deep the agent tree may grow.
        # 0 = main agent; each spawn increments depth by one. With max_depth=2
        # the tree is main(0) -> generator(1) -> worker(2); the worker is a leaf.
        self.depth = depth
        self.max_depth = max_depth
        # Per-run workspace (Phase 0.2): a scratch directory shared by every role
        # and worker in one run_pipeline() call. Phases hand off via files here
        # (planner -> spec.md, workers -> findings/*.md, evaluator -> feedback.md);
        # only short summaries flow back through tool results. None = no run
        # workspace (the main agent's default — its behavior is unchanged).
        self.workspace_dir = workspace_dir
        # Per-run concurrency budget (Phase 0.4): a semaphore OWNED by one run,
        # bounding how many workers fan out at once (e.g. breadth=5). It is
        # distinct from the global MAX_CONCURRENT_SUBAGENTS safety cap, so a
        # breadth-5 fan-out is not throttled to the global cap of 3. The delegate
        # tool (Phase 0.5) acquires it around each worker spawn. None = no run
        # (the main agent's default).
        self.run_semaphore = run_semaphore
        # Total run safety rail (Phase 0.6): ONE RunBudget shared by every role and
        # worker in a single run_pipeline() call. run_tool() checks it before each
        # dispatch and increments its counter, so the cap spans the whole agent tree
        # (generator + fanned-out workers), not one runtime. None = no run budget
        # (the main agent's default — ordinary Q&A is never budget-stopped).
        self.run_budget = run_budget
        # Shared decomposition / progress ledger (Phase 0.8 socket; the TodoList
        # primitive itself lands in Phase 1). Carried run-wide like the workspace so a
        # generator's completion gate can see open todos. Typed loosely on purpose —
        # the kernel never inspects it. None on the main agent.
        self.todo_list = todo_list
        # Per-run behavior policy (Phase 0.7). Consulted by AgentRuntime to optionally
        # hide tools, wrap the system prompt, and gate completion (on_idle). Unlike the
        # run budget/workspace, this is NOT inherited by children: each role sets its
        # own (workers stay DefaultPolicy), so it is deliberately absent from
        # child_context(). None => DefaultPolicy => today's exact behavior.
        self.policy = policy

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
        sibling spawns can never clobber each other's depth. The run workspace is
        carried forward unchanged so the whole subtree handed off files into the
        same per-run directory.

        Run-scoped state — workspace, semaphore, run_budget, todo_list — is shared
        by reference so the total budget and progress ledger span the subtree.
        ``policy`` is intentionally NOT carried forward: it is per-role (a generator
        sets its own; its workers default to DefaultPolicy).
        """
        return AgentContext(
            chat_id=self.chat_id,
            telegram_client=self.telegram_client,
            cron_service=self.cron_service,
            depth=self.depth + 1,
            max_depth=self.max_depth,
            workspace_dir=self.workspace_dir,
            run_semaphore=self.run_semaphore,
            run_budget=self.run_budget,
            todo_list=self.todo_list,
        )

    def new_run_context(self) -> "AgentContext":
        """A fresh depth-0 context for a capability run (Phase 2 entry tools).

        Copies shared services (telegram/cron/identity) but RESETS depth and leaves
        every run-scoped field — workspace, budget, semaphore, todos, policy —
        unset, so ``run_pipeline`` can populate them WITHOUT mutating the caller's
        live context. The main agent therefore never inherits a research run's
        budget or workspace. Depth-0 keeps the role/worker fan-out math correct
        (generator at depth 1, workers at depth 2 under the default max_depth=2).
        """
        return AgentContext(
            chat_id=self.chat_id,
            telegram_client=self.telegram_client,
            cron_service=self.cron_service,
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
