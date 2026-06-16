"""The shared trio-runner (Phase 1.2 + 1.3).

``run_pipeline`` is the ONE engine: it sequences a Planner -> Generator ->
Evaluator descriptor, and is domain-blind — everything domain-specific arrives
via ``config`` (a CapabilityConfig). Swap RESEARCH_CONFIG for CODING_CONFIG and
the identical engine builds software.

A "phase runner" is just: spawn a role subagent (reusing the existing
SubagentRegistry's retry/backoff), and hand off via files in the per-run
workspace. This gives the article's "fresh context per phase" for free — each
role runs in its own in_memory() session inside run_subagent.
"""
from __future__ import annotations

import asyncio

from tool_base import AgentContext, RunBudget
from subagent_tools import run_subagent
from .config import CapabilityConfig, RoleConfig
from .todo import TodoList
from .artifacts import (
    make_run_workspace, read_final_artifact, schedule_workspace_cleanup,
    read_artifact, write_artifact,
)

# Today the topology is a constant; a Phase 9 meta-planner can emit it as JSON for
# the same executor (no "3" is hardcoded into the engine).
PGE_DEFAULT: list[str] = ["plan", "gen", "eval"]


async def spawn_role(
    role: RoleConfig,
    *,
    task: str,
    context: AgentContext,
    max_tool_calls_per_turn: int | None = None,
    timeout_s: float | None = None,
) -> str:
    """Run one role as a subagent and return its final summary text.

    Reuses ``run_subagent`` (depth/concurrency/timeout/retry from Phase 0). The
    role runs at ``context.depth + 1``; from a depth-0 pipeline context that puts
    the generator at depth 1, so it can still fan out to depth-2 workers via its
    DelegateTasksTool. Concurrency is governed by the per-run semaphore, never the
    global cap (use_global_cap=False).
    """
    sys_prompt = role.system_prompt
    if context.workspace_dir:
        sys_prompt += (
            f"\n\nYour run workspace is {context.workspace_dir}. Read inputs from "
            "and write artifacts to that directory using absolute paths."
        )
    policy = role.policy_factory() if role.policy_factory else None
    todo_list = TodoList() if role.fresh_todo_list else None

    res = await run_subagent(
        task=task,
        context=context,
        tools_override=role.tools,
        sys_prompt=sys_prompt,
        model_name=role.model,
        timeout_s=timeout_s if timeout_s is not None else role.timeout_s,
        max_tool_calls_per_turn=(
            max_tool_calls_per_turn
            if max_tool_calls_per_turn is not None
            else role.max_tool_calls_per_turn
        ),
        use_global_cap=False,                 # per-run semaphore governs concurrency
        result_name=f"role:{role.name}",
        policy=policy,
        todo_list=todo_list,
    )
    if res.error:
        return f"[{role.name} did not complete] {res.result.get('error', 'unknown error')}"
    return res.result.get("content", "")


# ---- phase runners (1.3): each spawns one role subagent --------------------

async def run_plan(brief: str, config: CapabilityConfig, context: AgentContext) -> None:
    # Planner expands a thin brief into a todo-list spec; short budget, mirrors a
    # plain SpawnSubagentTool. Its returned text IS the spec → write it to a file.
    out = await spawn_role(config.planner, task=brief, context=context)
    write_artifact(context, "spec.md", out)


async def run_generate(brief: str, config: CapabilityConfig, context: AgentContext) -> None:
    # Generator works the spec under PlanExecutePolicy (todo completion gate). It
    # gets a high per-turn cap (0.3) and the run's wall-clock as its timeout (0.4)
    # so it is not force-stopped at 10 calls or killed at 300s. It writes the final
    # report.md (and any findings/*.md) directly into the workspace.
    spec = read_artifact(context, "spec.md") or brief
    budget = context.run_budget
    await spawn_role(
        config.generator,
        task=spec,
        context=context,
        max_tool_calls_per_turn=200,
        timeout_s=budget.max_wall_clock_s if budget else None,
    )


async def run_evaluate(brief: str, config: CapabilityConfig, context: AgentContext) -> None:
    # The `auto` gate (Phase 4/5 fills in real criteria scoring). With verify="off"
    # (the default) this returns immediately, so plan→gen alone yields the result.
    if not _should_verify(config) or config.evaluator is None:
        return
    for _ in range(max(1, config.max_rounds)):
        if context.run_budget and context.run_budget.exhausted():
            break
        target = read_artifact(context, "report.md") or read_artifact(context, "spec.md")
        verdict = await spawn_role(config.evaluator, task=target, context=context)
        write_artifact(context, "verdict.md", verdict)
        if _verdict_passed(verdict):
            return
        write_artifact(context, "feedback.md", verdict)
        await run_generate(brief, config, context)   # iterate on the feedback


def _should_verify(config: CapabilityConfig) -> bool:
    # "auto" (cost-aware) selection is Phase 5; until then it behaves like "off".
    return config.verify == "on"


def _verdict_passed(verdict: str) -> bool:
    return "PASS" in (verdict or "").upper()


PHASE_RUNNERS = {"plan": run_plan, "gen": run_generate, "eval": run_evaluate}


# ---- the engine (1.2) ------------------------------------------------------

async def run_pipeline(
    brief: str,
    *,
    config: CapabilityConfig,
    context: AgentContext,
    descriptor: list[str] | None = None,
) -> str:
    """Sequence the descriptor's phases over one shared run context, then return
    the final artifact. Sets up the per-run workspace (0.2), total budget (0.6),
    and concurrency semaphore (0.4); every role/worker shares them via
    child_context. Stops cleanly and returns partial results when the budget is
    exhausted; always schedules workspace cleanup.

    NOTE: the caller owns ``context``; pass a run-scoped one (the entry tool does
    this in Phase 3), since these fields are mutated on it.
    """
    descriptor = descriptor or PGE_DEFAULT
    context.workspace_dir = make_run_workspace()
    context.run_budget = RunBudget()
    context.run_semaphore = asyncio.Semaphore(config.breadth)
    try:
        for phase in descriptor:
            if context.run_budget.exhausted():
                break   # graceful: stop here and synthesize from what exists
            await PHASE_RUNNERS[phase](brief, config, context)
        return read_final_artifact(context.workspace_dir)
    finally:
        schedule_workspace_cleanup(context.workspace_dir)
