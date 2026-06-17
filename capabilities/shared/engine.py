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
import os
import time
from datetime import datetime, timezone

from rich import print

from tool_base import AgentContext, RunBudget
from subagent_tools import run_subagent
from .config import CapabilityConfig, RoleConfig
from .todo import TodoList
from .artifacts import (
    make_run_workspace, read_final_artifact, schedule_workspace_cleanup,
    read_artifact, write_artifact, remove_artifact,
)
from .verdict import load_verdict, record_verdict, render_feedback

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
    delegate tool (e.g. DelegateWebSearchTool). Concurrency is governed by the
    per-run semaphore, never the global cap (use_global_cap=False).
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
    # On a feedback round (Phase 4 loop), prepend the evaluator's concrete fixes so
    # the generator revises the EXISTING report.md instead of starting fresh.
    feedback = read_artifact(context, "feedback.md")
    task = spec
    if feedback.strip():
        task = (
            f"{spec}\n\n---\nA previous draft of report.md was reviewed and did NOT "
            f"pass. Revise report.md to address this feedback (read the existing "
            f"report.md and edit it):\n\n{feedback}"
        )
    budget = context.run_budget
    summary = await spawn_role(
        config.generator,
        task=task,
        context=context,
        max_tool_calls_per_turn=200,
        timeout_s=budget.max_wall_clock_s if budget else None,
    )
    # The generator's final plain-text reply is its own one-line summary. Persist it
    # so the entry tool can return a SHORT result (Phase 6 output contract) instead of
    # dumping the whole report into the caller's context. Generic: any capability's
    # generator returns a summary; the file name is engine-neutral.
    write_artifact(context, "summary.md", summary)


async def run_evaluate(brief: str, config: CapabilityConfig, context: AgentContext) -> None:
    # The GAN core (Phase 4): a skeptical evaluator scores report.md against the
    # config's weighted/thresholded criteria, gated by `verify` (off/on/auto). On a
    # failing round it writes feedback.md and re-runs the generator, bounded by
    # max_rounds + RunBudget.
    if config.evaluator is None or not should_verify(config, context):
        return
    if not read_artifact(context, "report.md").strip():
        return   # nothing to grade (generator produced no report)

    for round_index in range(1, max(1, config.max_rounds) + 1):
        if context.run_budget and context.run_budget.exhausted():
            break
        remove_artifact(context, "verdict.json")   # never read a stale round's verdict
        await spawn_role(config.evaluator, task=_eval_task(context), context=context)

        verdict = load_verdict(context)
        if verdict is None:
            # Evaluator answered without submitting a verdict. Don't loop blindly —
            # stop and return the current artifact (a calibration signal for Phase 5).
            print("[Evaluator] no verdict submitted; ending evaluation")
            return
        record_verdict(verdict, config, context=context, round_index=round_index)
        if verdict.passed(config.criteria):
            return
        write_artifact(context, "feedback.md", render_feedback(verdict, config.criteria))
        await run_generate(brief, config, context)   # iterate on the feedback


def should_verify(config: CapabilityConfig, context: AgentContext) -> bool:
    """The evaluator gate. off → never; on → always; auto → only when the task
    looks non-trivial (default-on bias for a weaker model)."""
    if config.verify == "off":
        return False
    if config.verify == "on":
        return True
    return _looks_nontrivial(context)


def _looks_nontrivial(context: AgentContext) -> bool:
    # Read the planner's spec.md (in the shared workspace) and count sub-questions.
    # The generator's todo list lives in its own child context, so the spec is the
    # signal the parent actually has at eval time.
    spec = read_artifact(context, "spec.md")
    sub_qs = [ln for ln in spec.splitlines() if ln.strip()[:1].isdigit()]
    return len(sub_qs) >= 3


def _eval_task(context: AgentContext) -> str:
    return (
        "Skeptically grade the research report. Read report.md in your run "
        "workspace, score every criterion in [0,1] with a one-line rationale, then "
        "call SubmitVerdict exactly once with your scores, rationale, and concrete "
        "feedback for any criterion below threshold."
    )


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
    started_at = time.time()
    try:
        for phase in descriptor:
            if context.run_budget.exhausted():
                break   # graceful: stop here and synthesize from what exists
            await PHASE_RUNNERS[phase](brief, config, context)
        return read_final_artifact(context.workspace_dir)
    finally:
        _log_run(brief, config, context, descriptor, started_at)   # 7.4 observability
        schedule_workspace_cleanup(context.workspace_dir)           # 7.5 TTL sweep


def _log_run(brief, config, context, descriptor, started_at) -> None:
    """Phase 7.4: assemble + persist the per-run telemetry record. Best-effort —
    never lets a logging error escape into the run's result."""
    try:
        from .run_log import write_run_log
        from .text_tools import extract_sources

        run_id = os.path.basename(context.workspace_dir or "") or "unknown"
        report = read_artifact(context, "report.md")
        spec = read_artifact(context, "spec.md")
        sub_qs = [ln.strip() for ln in spec.splitlines() if ln.strip()[:1].isdigit()]

        verdicts: list[dict] = []
        try:
            from .verdict_store import default_store
            verdicts = [
                {"round": v["round_index"], "passed": v["passed"],
                 "weighted": v["weighted"], "scores": v["scores"]}
                for v in default_store().recent(config.name)
                if v["run_id"] == run_id
            ]
        except Exception:
            pass

        budget = context.run_budget
        write_run_log({
            "run_id": run_id,
            "capability": config.name,
            "brief": brief,
            "created_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
            "duration_s": round(time.time() - started_at, 2),
            "phases": list(descriptor),
            "sub_questions": sub_qs,
            "tool_calls": budget.calls if budget else None,
            "budget_exhausted": budget.exhausted() if budget else None,
            "sources": extract_sources(report),
            "verdicts": verdicts,
            "produced_report": bool(report.strip()),
        })
    except Exception as exc:
        print(f"[run_log] not written: {exc}")
