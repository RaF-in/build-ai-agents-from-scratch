"""The spawn_subagent tool — parent-only delegation to ephemeral children.

Deferred imports of agent/session avoid the circular dependency
(agent -> agent_tools -> subagent_tools -> agent).
"""
from __future__ import annotations

import asyncio

from tool_base import AgentContext, AgentTool, ToolResult
import telemetry
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


async def run_subagent(
    *,
    task: str,
    context: AgentContext,
    tools_override: list[type[AgentTool]],
    sys_prompt: str,
    model_name: str = SUBAGENT_MODEL,
    timeout_s: float = SUBAGENT_TIMEOUT_SECONDS,
    max_tool_calls_per_turn: int = 10,
    use_global_cap: bool = True,
    result_name: str = "SpawnSubagentTool",
    policy: object | None = None,
    todo_list: object | None = None,
) -> ToolResult:
    """Spawn one child agent with retry/backoff, a PER-SPAWN timeout, and
    depth/concurrency safety. The single low-level spawn primitive shared by the
    legacy ``SpawnSubagentTool`` and the PGE engine's role/worker spawns.

    ``context`` is the SPAWNER's context (depth d); the child runs at d+1.

    Phase 0.4 knobs:
      * ``timeout_s`` — per-spawn wall-clock budget. Quick workers keep the short
        default (300s); a long-running generator passes a much larger value so it
        is not killed mid-run (fatal blocker #2).
      * ``use_global_cap`` — True applies the global MAX_CONCURRENT_SUBAGENTS cap
        (legacy / main-agent ad-hoc spawns). False tracks history only via
        ``registry.register()`` so a per-run, semaphore-bounded fan-out is not
        throttled to the global cap of 3.

    Phase 1 (PGE) knobs — per-ROLE context overrides applied to the forked child:
      * ``policy`` — a RunPolicy for the child (e.g. the generator's
        PlanExecutePolicy). ``child_context`` deliberately does NOT inherit the
        parent's policy (Phase 0.7), so a role's policy is injected here instead.
      * ``todo_list`` — a fresh TodoList for the child (the generator plans into
        it; its completion gate reads it). None leaves the inherited value.
    """
    # Phase 3.3: one span per spawned child (role or worker). Carries the spawn
    # name, the spawner's depth, and a summary of the task. subagent.status /
    # subagent.attempts are set on every exit path; non-completed outcomes feed the
    # subagent.failures metric. No-op when telemetry is disabled.
    sa_attrs = {"subagent.name": result_name, "agent.depth": context.depth}
    sa_attrs.update(telemetry.payload("subagent.task", task))
    with telemetry.span(f"agent.subagent {result_name}", **sa_attrs) as sa:
        # --- Constraint 1: depth budget (structural; this is the safety net) ---
        # child_tools() already withholds the spawn tool past the budget, so normally
        # we never reach here too deep. Guard anyway against a misconfigured runtime.
        if context.depth + 1 > context.max_depth:
            sa.set_attribute("subagent.status", "rejected_depth")
            telemetry.record_subagent_failure(result_name, "rejected_depth")
            return ToolResult(name=result_name, error=True, result={
                "error": (
                    f"Cannot spawn: depth budget reached "
                    f"(depth={context.depth}, max_depth={context.max_depth}). "
                    "Do the work yourself."
                )
            })

        # --- Constraint 2: concurrency tracking (cap optional per Phase 0.4) ---
        if use_global_cap:
            run = subagent_registry.try_acquire(task)
            if run is None:
                sa.set_attribute("subagent.status", "rejected_concurrency")
                telemetry.record_subagent_failure(result_name, "rejected_concurrency")
                return ToolResult(name=result_name, error=True, result={
                    "error": (
                        f"Cannot spawn: {subagent_registry.max_concurrent} subagents "
                        "already running. Wait for one to finish or do the work yourself."
                    )
                })
        else:
            # Run-scoped spawn: concurrency is bounded by the per-run semaphore
            # (acquired by the caller / delegate tool), not the global cap.
            run = subagent_registry.register(task)

        last_error = ""
        try:
            for attempt in range(1, MAX_RETRIES + 2):  # 1..(1 + MAX_RETRIES)
                run.attempts = attempt
                try:
                    # --- Constraint 3: per-spawn timeout via wait_for cancellation ---
                    result_text = await asyncio.wait_for(
                        _run_child(
                            task=task,
                            context=context,
                            tools_override=tools_override,
                            sys_prompt=sys_prompt,
                            model_name=model_name,
                            max_tool_calls_per_turn=max_tool_calls_per_turn,
                            policy=policy,
                            todo_list=todo_list,
                        ),
                        timeout=timeout_s,
                    )
                    subagent_registry.complete(run, status="completed", result=result_text)
                    sa.set_attribute("subagent.status", "completed")
                    sa.set_attribute("subagent.attempts", run.attempts)
                    return ToolResult(name=result_name, result={"content": result_text})

                except asyncio.TimeoutError:
                    last_error = f"timed out after {timeout_s}s"
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
            sa.set_attribute("subagent.status", status)
            sa.set_attribute("subagent.attempts", run.attempts)
            telemetry.record_subagent_failure(result_name, status)
            return ToolResult(name=result_name, error=True, result={
                "error": f"Subagent {status} after {run.attempts} attempt(s): {last_error}"
            })

        except BaseException:
            # Cancellation / unexpected: never leak the concurrency slot.
            subagent_registry.complete(run, status="failed", result="cancelled")
            sa.set_attribute("subagent.status", "cancelled")
            telemetry.record_subagent_failure(result_name, "cancelled")
            raise


async def _run_child(
    *,
    task: str,
    context: AgentContext,
    tools_override: list[type[AgentTool]],
    sys_prompt: str,
    model_name: str,
    max_tool_calls_per_turn: int,
    policy: object | None = None,
    todo_list: object | None = None,
) -> str:
    # Deferred import breaks the import cycle.
    from agent import AgentRuntime
    from session import SessionManager

    # Fork a depth+1 context (fresh object) so this child — and any siblings
    # spawned concurrently — each track their own depth (and share the run's
    # workspace/semaphore) correctly.
    child_ctx = context.child_context()
    # Per-role overrides (Phase 1). policy is never inherited (Phase 0.7), so a
    # role that needs one (the generator) injects it here; a fresh todo_list is
    # likewise scoped to this child rather than shared from the parent.
    if policy is not None:
        child_ctx.policy = policy
    if todo_list is not None:
        child_ctx.todo_list = todo_list

    sm = SessionManager.in_memory(model_name)
    child = AgentRuntime(
        context=child_ctx,                # depth+1; cron/telegram services shared
        session_manager=sm,
        model_name=model_name,
        tools_override=tools_override,
        max_tool_calls_per_turn=max_tool_calls_per_turn,
    )
    try:
        await child.initialize()          # no replay_handler => silent worker
        has_more = await child.run(user_text=task, sys_prompt=sys_prompt)
        while has_more:
            has_more = await child.run(user_text=None, sys_prompt=None)
        return await _final_summary(sm)
    finally:
        # finally always runs — on success, failure, OR cancellation (timeout).
        # This is the `session.dispose()` equivalent: connection closed,
        # in-memory DB destroyed, no leaked resources.
        await sm.close()


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


async def delegate_to_workers(
    context: AgentContext,
    tasks: list[str],
    *,
    worker_tools: list[type[AgentTool]],
    worker_prompt: str,
    result_name: str,
    timeout_s: float = SUBAGENT_TIMEOUT_SECONDS,
    task_key: str = "task",
) -> list[dict]:
    """Fan out one worker per task, concurrently, bounded by the PER-RUN semaphore.

    This is the ONE generic fan-out mechanism (Phase 0.5). It is capability-blind:
    callers supply the worker tool set and prompt, so the research web-search
    delegate (web.tools.DelegateWebSearchTool) and any future capability's delegate
    wrapper all share this single implementation. Concurrency is confined here
    (the kernel's
    tool-dispatch loop stays sequential), and bounded by ``context.run_semaphore``
    — NOT the global cap — so a breadth-N run actually fans out N-wide. Per-task
    failures are isolated: one worker failing still returns every other worker's
    result, in input order.

    Returns a list of ``{<task_key>, ok, summary, error}`` dicts. Raises
    ``ValueError`` when there is no per-run semaphore (the caller converts that to
    a clean tool error), since this tool only makes sense inside a pipeline run.
    """
    sem = context.run_semaphore
    if sem is None:
        raise ValueError(
            "requires a per-run semaphore (must be spawned inside a pipeline run)"
        )

    async def one(task: str) -> dict:
        try:
            async with sem:                      # per-run budget, NOT the global cap
                res = await run_subagent(
                    task=task,
                    context=context,             # run_subagent forks depth+1 internally
                    tools_override=worker_tools,
                    sys_prompt=worker_prompt,
                    timeout_s=timeout_s,         # workers keep the short leash
                    use_global_cap=False,        # semaphore governs concurrency
                    result_name=result_name,
                )
            if res.error:
                return {task_key: task, "ok": False, "summary": None,
                        "error": res.result.get("error", "unknown error")}
            return {task_key: task, "ok": True,
                    "summary": res.result.get("content", ""), "error": None}
        except Exception as exc:
            # Isolate: one worker's failure must not sink the rest of the batch.
            return {task_key: task, "ok": False, "summary": None, "error": str(exc)}

    # Phase 3.4: one span over the whole fan-out. Each worker's agent.subagent span
    # nests under it (the gather runs inside this span, so the OTel context carries
    # into every task). No-op when telemetry is disabled.
    with telemetry.span("capability.delegate", **{"delegate.task_count": len(tasks)}) as dspan:
        # gather preserves order; each `one` already returns a result (never raises).
        results = await asyncio.gather(*[one(t) for t in tasks])
        ok = sum(1 for r in results if r.get("ok"))
        dspan.set_attribute("delegate.ok_count", ok)
        dspan.set_attribute("delegate.failed_count", len(results) - ok)
        return results


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
        # Legacy single-level delegation: file/shell tools scoped by depth, the
        # depth-aware prompt, the default model/timeout/cap, and the global
        # concurrency cap. Exact behavior preserved — the per-spawn overrides
        # (long timeout, high cap, no global cap) are for the PGE engine only.
        can_spawn = _child_can_spawn(context.depth, context.max_depth)
        return await run_subagent(
            task=self.task,
            context=context,
            tools_override=child_tools(context.depth, context.max_depth),
            sys_prompt=child_system_prompt(can_spawn=can_spawn),
            result_name=self.tool_name(),
        )


# NOTE: there is intentionally no generic "DelegateTasksTool" AgentTool. Parallel
# fan-out is provided by the reusable ``delegate_to_workers`` helper above; each
# capability that needs it ships a thin wrapper that pins its own worker tool set
# and prompt (e.g. web.tools.DelegateWebSearchTool scopes workers to search/fetch).
# A file/bash-worker wrapper would be added the same way when a capability needs one.
