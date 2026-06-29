"""Standalone checks for Phase 4 (run: `uv run python testing_files/test_phase4_evaluator.py`).

Covers:
  4.2  Verdict — score clamping, missing-score-as-0, hard-threshold passed(),
       weighted_score, failures(). SubmitVerdict writes verdict.json; load_verdict
       round-trips; the B-fallback parser salvages JSON from prose.
  4.3  should_verify off/on/auto + _looks_nontrivial (spec-driven gate).
  loop full plan->gen->eval against a fake LLM: round 1 FAIL -> feedback ->
       generator revises -> round 2 PASS; and a no-verdict evaluator ends cleanly.

No network: acompletion is faked; the evaluator "calls" SubmitVerdict.
"""
import asyncio
import json
import os
import re
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import agent as agentmod
from tool_base import AgentContext
from agent_tools import WriteTool, ReadTool
from capabilities.shared import artifacts
from capabilities.shared.config import RoleConfig, CapabilityConfig, Criterion
from capabilities.shared.policies import PlanExecutePolicy
from capabilities.shared.todo import TODO_TOOLS
from capabilities.shared.verdict import (
    Verdict, SubmitVerdict, load_verdict, render_feedback, _parse_verdict_from_text,
)
from capabilities.shared.engine import run_pipeline, run_evaluate, should_verify, _looks_nontrivial


def NS(**kw):
    o = type("NS", (), {})()
    for k, v in kw.items():
        setattr(o, k, v)
    return o


CRITERIA = [Criterion("a", 1.0, 0.6, "crit a"), Criterion("b", 1.0, 0.6, "crit b")]


# ---------------- 4.2 Verdict logic ----------------
def test_verdict_logic():
    v = Verdict(scores={"a": 0.8, "b": 0.9})
    assert v.passed(CRITERIA) and v.failures(CRITERIA) == []
    # a below threshold => whole verdict fails (hard threshold)
    v2 = Verdict(scores={"a": 0.2, "b": 0.9})
    assert not v2.passed(CRITERIA) and v2.failures(CRITERIA) == ["a"]
    # missing score counts as 0 (skeptical)
    v3 = Verdict(scores={"a": 0.9})
    assert v3.score_for("b") == 0.0 and not v3.passed(CRITERIA)
    # out-of-range clamps; weighted score is the criteria-weighted mean
    v4 = Verdict(scores={"a": 1.5, "b": 0.5})
    assert v4.score_for("a") == 1.0
    assert abs(v4.weighted_score(CRITERIA) - 0.75) < 1e-9
    print("4.2 Verdict threshold/missing/clamp/weighted: OK")


async def test_submit_and_load():
    ws = artifacts.make_run_workspace()
    ctx = AgentContext(workspace_dir=ws)
    res = await SubmitVerdict(
        scores={"a": 0.7, "b": 0.65}, rationale={"a": "ok", "b": "ok"},
        feedback="tighten synthesis",
    ).execute(ctx)
    assert not res.error and res.result["recorded"]
    loaded = load_verdict(ctx)
    assert loaded is not None and loaded.scores["a"] == 0.7 and loaded.feedback == "tighten synthesis"
    # absent verdict => None
    assert load_verdict(AgentContext(workspace_dir=artifacts.make_run_workspace())) is None
    print("4.1 SubmitVerdict writes verdict.json + load_verdict round-trips: OK")


def test_text_fallback_and_feedback():
    prose = (
        "Here is my assessment.\n```json\n"
        '{"scores": {"a": 0.3, "b": 0.8}, "rationale": {"a": "thin"}, '
        '"feedback": "answer sub-q 2"}\n```\nHope this helps!'
    )
    v = _parse_verdict_from_text(prose)
    assert v is not None and v.scores["a"] == 0.3 and "sub-q 2" in v.feedback
    # garbage / no json => None
    assert _parse_verdict_from_text("VERDICT: FAIL, no json here") is None
    # render_feedback names the failing criterion + its score
    fb = render_feedback(v, CRITERIA)
    assert "a:" in fb and "0.30" in fb and "answer sub-q 2" in fb
    print("4.1 B-fallback parser + render_feedback: OK")


# ---------------- 4.3 the gate ----------------
def test_should_verify_gate():
    ws = artifacts.make_run_workspace()
    ctx = AgentContext(workspace_dir=ws)
    off = CapabilityConfig(name="x", planner=None, generator=None, verify="off")
    on = CapabilityConfig(name="x", planner=None, generator=None, verify="on")
    auto = CapabilityConfig(name="x", planner=None, generator=None, verify="auto")
    assert should_verify(off, ctx) is False and should_verify(on, ctx) is True
    # auto: trivial spec (<3 sub-questions) => skip
    artifacts.write_artifact(ctx, "spec.md", "1. only one question")
    assert _looks_nontrivial(ctx) is False and should_verify(auto, ctx) is False
    # auto: >=3 sub-questions => run
    artifacts.write_artifact(ctx, "spec.md", "1. a\n2. b\n3. c")
    assert _looks_nontrivial(ctx) is True and should_verify(auto, ctx) is True
    print("4.3 should_verify off/on/auto + _looks_nontrivial: OK")


# ---------------- full loop: fail -> feedback -> pass ----------------
def _loop_config(evaluator_submits=True):
    return CapabilityConfig(
        name="fake",
        planner=RoleConfig(name="planner", system_prompt="[[PLANNER]] plan it."),
        generator=RoleConfig(
            name="generator", system_prompt="[[GENERATOR]] write report.md.",
            tools=[WriteTool, ReadTool, *TODO_TOOLS],
            policy_factory=PlanExecutePolicy, fresh_todo_list=True,
        ),
        evaluator=RoleConfig(
            name="evaluator", system_prompt="[[EVALUATOR]] grade it.",
            tools=[ReadTool, SubmitVerdict],
        ),
        verify="on", max_rounds=2, criteria=CRITERIA,
    )


_state = {"eval_round": 0}


async def fake_acompletion(model, messages, tools=None, **kw):
    sysp = messages[0]["content"] if messages and messages[0]["role"] == "system" else ""
    user = next((m["content"] for m in messages if m.get("role") == "user"), "")
    has_tool_result = any(m.get("role") == "tool" for m in messages)

    if "[[PLANNER]]" in sysp:
        return NS(choices=[NS(message=NS(content="1. q one\n2. q two\n3. q three", tool_calls=None))])

    if "[[GENERATOR]]" in sysp:
        ws = re.search(r"Your run workspace is ([^\s.]+)", sysp).group(1)
        if not has_tool_result:
            content = "# Report v2\nrevised & fixed" if "Revise" in user else "# Report v1\ninitial draft"
            tc = NS(id="g1", function=NS(name="WriteTool",
                    arguments=json.dumps({"path": f"{ws}/report.md", "content": content})))
            return NS(choices=[NS(message=NS(content="", tool_calls=[tc]))])
        return NS(choices=[NS(message=NS(content="report written", tool_calls=None))])

    if "[[EVALUATOR]]" in sysp:
        if not has_tool_result:
            _state["eval_round"] += 1
            scores = {"a": 0.2, "b": 0.9} if _state["eval_round"] == 1 else {"a": 0.8, "b": 0.9}
            tc = NS(id="e1", function=NS(name="SubmitVerdict", arguments=json.dumps(
                {"scores": scores, "rationale": {"a": "x", "b": "y"}, "feedback": "fix criterion a"})))
            return NS(choices=[NS(message=NS(content="", tool_calls=[tc]))])
        return NS(choices=[NS(message=NS(content="verdict submitted", tool_calls=None))])

    return NS(choices=[NS(message=NS(content="ok", tool_calls=None))])


async def test_full_loop_fail_then_pass():
    _state["eval_round"] = 0
    agentmod.acompletion = fake_acompletion
    ctx = AgentContext()
    result = await run_pipeline("Research X deeply", config=_loop_config(), context=ctx)

    assert "Report v2" in result, repr(result)              # the revised report is returned
    assert _state["eval_round"] == 2                          # graded twice (fail then pass)
    assert "criterion a" in artifacts.read_artifact(ctx, "feedback.md")
    final_verdict = load_verdict(ctx)
    assert final_verdict is not None and final_verdict.passed(CRITERIA)
    print("loop: round1 FAIL -> feedback -> revise -> round2 PASS -> report v2: OK")


async def test_no_verdict_ends_cleanly():
    # Evaluator answers without calling SubmitVerdict => run_evaluate must not loop.
    async def no_submit(model, messages, tools=None, **kw):
        sysp = messages[0]["content"] if messages and messages[0]["role"] == "system" else ""
        if "[[EVALUATOR]]" in sysp:
            return NS(choices=[NS(message=NS(content="Looks fine to me.", tool_calls=None))])
        return NS(choices=[NS(message=NS(content="ok", tool_calls=None))])

    agentmod.acompletion = no_submit
    from tool_base import RunBudget
    ws = artifacts.make_run_workspace()
    ctx = AgentContext(workspace_dir=ws, run_semaphore=asyncio.Semaphore(2), run_budget=RunBudget())
    artifacts.write_artifact(ctx, "spec.md", "1. a\n2. b\n3. c")
    artifacts.write_artifact(ctx, "report.md", "# Report\nbody")
    cfg = _loop_config()
    await run_evaluate("brief", cfg, ctx)
    # no verdict file, no feedback written, did not hang
    assert load_verdict(ctx) is None
    assert artifacts.read_artifact(ctx, "feedback.md") == ""
    print("loop: evaluator submits no verdict -> ends cleanly (no infinite loop): OK")


async def main():
    test_verdict_logic()
    await test_submit_and_load()
    test_text_fallback_and_feedback()
    test_should_verify_gate()
    await test_full_loop_fail_then_pass()
    await test_no_verdict_ends_cleanly()
    print("\nALL PHASE 4 CHECKS PASSED")


if __name__ == "__main__":
    asyncio.run(main())
