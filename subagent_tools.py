"""The spawn_subagent tool — parent-only delegation to ephemeral children.

Deferred imports of agent/session avoid the circular dependency
(agent -> agent_tools -> subagent_tools -> agent).
"""
from __future__ import annotations

import asyncio

from tool_base import AgentContext, AgentTool, ToolResult
from subagent import (
    subagent_registry,
    SUBAGENT_TIMEOUT_SECONDS, SUBAGENT_MODEL, child_system_prompt,
    MAX_RETRIES, RETRY_BASE_DELAY, RETRY_ON_TIMEOUT,
)


def _child_can_spawn(spawner_depth: int, max_depth: int) -> bool:
    """Whether a child spawned by an agent at ``spawner_depth`` may itself spawn.

    The child sits at ``spawner_depth + 1``; it keeps the spawn tool only while
    that depth is still below ``max_depth`` (so its own children stay in budget).
    """
    return spawner_depth + 1 < max_depth


def child_tools(spawner_depth: int, max_depth: int) -> list[type[AgentTool]]:
    """The tool universe handed to a child spawned by an agent at ``spawner_depth``.

    File/shell tools always; SpawnSubagentTool only while the depth budget allows
    further fan-out. The depth limit is therefore *structural* — a leaf worker
    literally lacks the tool, so it cannot recurse. Resolved lazily so this module
    never imports agent_tools at load time (agent_tools imports *us*, so a
    top-level import back would be a circular dependency).
    """
    from agent_tools import ReadTool, WriteTool, EditTool, BashTool
    base: list[type[AgentTool]] = [ReadTool, WriteTool, EditTool, BashTool]
    if _child_can_spawn(spawner_depth, max_depth):
        base.append(SpawnSubagentTool)
    return base


class SpawnSubagentTool(AgentTool):
    """Delegate a focused, self-contained task to a child agent.

    Use this to offload well-scoped work (research a pattern, document an API,
    review test coverage) so your own context stays clean. The child runs in an
    isolated, in-memory session with only file/shell tools, and CANNOT spawn
    further subagents. You receive only its final summary as the tool result.

    Args:
        task: A complete, standalone instruction. The child sees nothing of this
              conversation — include every detail it needs (paths, goals, format).
    """
    task: str

    async def execute(self, context: AgentContext) -> ToolResult:
        # --- Constraint 1: depth budget (structural; this is the safety net) ---
        # child_tools() already withholds this tool past the budget, so normally
        # we never reach here too deep. Guard anyway against a misconfigured runtime.
        if context.depth + 1 > context.max_depth:
            return self.tool_result(error=True, result={
                "error": (
                    f"Cannot spawn: depth budget reached "
                    f"(depth={context.depth}, max_depth={context.max_depth}). "
                    "Do the work yourself."
                )
            })

        # --- Constraint 2: concurrency cap (one slot for the whole run) ---
        run = subagent_registry.try_acquire(self.task)
        if run is None:
            return self.tool_result(error=True, result={
                "error": (
                    f"Cannot spawn: {subagent_registry.max_concurrent} subagents "
                    "already running. Wait for one to finish or do the work yourself."
                )
            })

        last_error = ""
        try:
            for attempt in range(1, MAX_RETRIES + 2):  # 1..(1 + MAX_RETRIES)
                run.attempts = attempt
                try:
                    # --- Constraint 3: timeout via wait_for cancellation ---
                    result_text = await asyncio.wait_for(
                        self._run_child(context),
                        timeout=SUBAGENT_TIMEOUT_SECONDS,
                    )
                    subagent_registry.complete(run, status="completed", result=result_text)
                    return self.tool_result(result={"content": result_text})

                except asyncio.TimeoutError:
                    last_error = f"timed out after {SUBAGENT_TIMEOUT_SECONDS}s"
                    if not RETRY_ON_TIMEOUT:
                        break  # fail fast — don't freeze the parent for minutes
                except Exception as e:
                    # Errors come back as content, never a crash (parent decides next step).
                    last_error = str(e)

                # Exhausted attempts? stop. Otherwise back off and retry a fresh child.
                if attempt >= MAX_RETRIES + 1:
                    break
                await asyncio.sleep(RETRY_BASE_DELAY * (2 ** (attempt - 1)))

            status = "expired" if "timed out" in last_error else "failed"
            subagent_registry.complete(run, status=status, result=last_error)
            return self.tool_result(error=True, result={
                "error": f"Subagent {status} after {run.attempts} attempt(s): {last_error}"
            })

        except BaseException:
            # Cancellation / unexpected: never leak the concurrency slot.
            subagent_registry.complete(run, status="failed", result="cancelled")
            raise

    async def _run_child(self, context: AgentContext) -> str:
        # Deferred import breaks the import cycle.
        from agent import AgentRuntime
        from session import SessionManager

        # Fork a depth+1 context (fresh object) so this child — and any siblings
        # spawned concurrently — each track their own depth correctly.
        child_ctx = context.child_context()
        can_spawn = _child_can_spawn(context.depth, context.max_depth)

        sm = SessionManager.in_memory(SUBAGENT_MODEL)
        child = AgentRuntime(
            context=child_ctx,                # depth+1; cron/telegram services shared
            session_manager=sm,
            model_name=SUBAGENT_MODEL,
            # Tools scoped by the *spawner's* depth: spawn tool only while in budget.
            tools_override=child_tools(context.depth, context.max_depth),
        )
        try:
            await child.initialize()          # no replay_handler => silent worker
            has_more = await child.run(
                user_text=self.task,
                sys_prompt=child_system_prompt(can_spawn=can_spawn),
            )
            while has_more:
                has_more = await child.run(user_text=None, sys_prompt=None)
            return await self._final_summary(sm)
        finally:
            # finally always runs — on success, failure, OR cancellation (timeout).
            # This is the `session.dispose()` equivalent: connection closed,
            # in-memory DB destroyed, no leaked resources.
            await sm.close()

    @staticmethod
    async def _final_summary(sm) -> str:
        """The child's last assistant text — the only thing that flows to the parent."""
        from session import SessionEvent, TextPart
        events = await sm.load_messages()
        for event in reversed(events):
            if isinstance(event, SessionEvent) and event.role == "assistant":
                texts = [p.text for p in event.parts if isinstance(p, TextPart) and p.text.strip()]
                if texts:
                    return "\n".join(texts)
        return "(subagent finished without producing a text summary)"
