"""Standalone checks for Phase 5 — evaluator calibration.
Run: `uv run python testing_files/test_phase5_calibration.py`

Covers:
  5.1  VerdictStore round-trip (record/recent/label/get) on a temp DB;
       record_verdict persists a row AND snapshots the graded report.
  5.2  load_eval_examples / render_eval_examples; the deep_research evaluator
       prompt has its few-shot examples injected (no leftover {{EVAL_EXAMPLES}}).
  5.3  agreement_rate against a fake evaluator: 2/3 cases match -> rate 0.67,
       the disagreement is flagged as a MISS.
  5.4  label_and_learn writes a corrected few-shot example that the loader picks
       up; pending_review hides already-labeled rows.

No network: acompletion is faked; the evaluator reads report.md then "calls"
SubmitVerdict. The calibration DB lives in a tmp dir via AGENT_CALIBRATION_DB.
"""
import asyncio
import json
import os
import re
import sys
import tempfile

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

# Point the calibration store at a throwaway DB BEFORE anything loads it.
_TMP = tempfile.mkdtemp(prefix="calib_test_")
os.environ["AGENT_CALIBRATION_DB"] = os.path.join(_TMP, "verdicts.db")

import agent as agentmod
from tool_base import AgentContext, RunBudget
from agent_tools import ReadTool
from capabilities.shared import artifacts
from capabilities.shared.config import RoleConfig, CapabilityConfig, Criterion
from capabilities.shared.verdict import Verdict, SubmitVerdict, record_verdict
from capabilities.shared import verdict_store as vstore
from capabilities.shared import calibration as calib


def NS(**kw):
    o = type("NS", (), {})()
    for k, v in kw.items():
        setattr(o, k, v)
    return o


CRITERIA = [Criterion("a", 1.0, 0.6, "crit a"), Criterion("b", 1.0, 0.6, "crit b")]


# ---------------- 5.1 VerdictStore + record_verdict ----------------
def test_store_roundtrip():
    vstore.reset_default_store()
    store = vstore.VerdictStore(db_path=os.path.join(_TMP, "rt.db"))
    vid = store.record(
        capability="deep_research", run_id="run_abc", round_index=1,
        scores={"a": 0.8, "b": 0.9}, rationale={"a": "ok"}, feedback="none",
        passed=True, weighted=0.85, artifact_ref=None,
    )
    got = store.get(vid)
    assert got["capability"] == "deep_research" and got["passed"] is True
    assert got["scores"] == {"a": 0.8, "b": 0.9} and got["human_label"] is None
    recent = store.recent("deep_research")
    assert len(recent) == 1 and recent[0]["id"] == vid
    # labeling attaches the human verdict and is visible on re-read
    store.label(vid, {"passed": False, "scores": {"a": 0.2}})
    assert store.get(vid)["human_label"]["passed"] is False
    print("5.1 VerdictStore record/recent/get/label round-trip: OK")


def test_record_verdict_persists_and_snapshots():
    vstore.reset_default_store()
    os.environ["AGENT_CALIBRATION_DB"] = os.path.join(_TMP, "rv.db")
    ws = artifacts.make_run_workspace()
    ctx = AgentContext(workspace_dir=ws)
    artifacts.write_artifact(ctx, "report.md", "# Report\nGOOD body with [src].")
    cfg = CapabilityConfig(name="deep_research", planner=None, generator=None, criteria=CRITERIA)
    v = Verdict(scores={"a": 0.8, "b": 0.9}, rationale={"a": "ok"}, feedback="fine")
    record_verdict(v, cfg, context=ctx, round_index=1)

    store = vstore.default_store()
    rows = store.recent("deep_research")
    assert len(rows) == 1 and rows[0]["passed"] is True
    ref = rows[0]["artifact_ref"]
    assert ref and os.path.isfile(ref), ref               # report snapshotted out of the workspace
    assert "GOOD body" in open(ref, encoding="utf-8").read()
    print("5.1 record_verdict persists a row + snapshots report.md: OK")


# ---------------- 5.2 few-shot examples + prompt injection ----------------
def test_eval_examples_loader_and_render():
    d = tempfile.mkdtemp(prefix="ex_")
    with open(os.path.join(d, "e1.json"), "w") as f:
        json.dump({"label": "fail-case", "artifact_excerpt": "vague text",
                   "scores": {"a": 0.2}, "rationale": {"a": "thin"}, "verdict": "fail"}, f)
    with open(os.path.join(d, "bad.json"), "w") as f:
        f.write("{not json")                              # malformed -> skipped, not fatal
    examples = calib.load_eval_examples(d)
    assert len(examples) == 1 and examples[0].label == "fail-case"
    block = calib.render_eval_examples(examples)
    assert "fail-case" in block and "a=0.2" in block and "thin" in block
    assert calib.render_eval_examples([]) == ""           # empty slot disappears cleanly
    print("5.2 load/render eval examples (skips malformed): OK")


def test_deep_research_prompt_injection():
    from capabilities.deep_research.config import RESEARCH_CONFIG
    sysp = RESEARCH_CONFIG.evaluator.system_prompt
    assert "{{EVAL_EXAMPLES}}" not in sysp                 # slot was filled
    assert "thin-coverage-fail" in sysp or "well-cited-pass" in sysp
    assert RESEARCH_CONFIG.eval_examples_dir and RESEARCH_CONFIG.calibration_set_dir
    print("5.2 deep_research evaluator prompt has few-shot examples injected: OK")


# ---------------- 5.3 agreement rate ----------------
async def fake_eval_acompletion(model, messages, tools=None, **kw):
    sysp = messages[0]["content"] if messages and messages[0]["role"] == "system" else ""
    if "[[EVALUATOR]]" not in sysp:
        return NS(choices=[NS(message=NS(content="ok", tool_calls=None))])
    tool_contents = [str(m.get("content", "")) for m in messages if m.get("role") == "tool"]
    if not tool_contents:
        ws = re.search(r"workspace is (/[^\s]+)", sysp).group(1).rstrip(".")
        tc = NS(id="r1", function=NS(name="ReadTool",
                arguments=json.dumps({"path": f"{ws}/report.md"})))
        return NS(choices=[NS(message=NS(content="", tool_calls=[tc]))])
    if not any("recorded" in c for c in tool_contents):
        good = "GOOD" in " ".join(tool_contents)
        scores = {"a": 0.8, "b": 0.9} if good else {"a": 0.2, "b": 0.9}
        tc = NS(id="e1", function=NS(name="SubmitVerdict", arguments=json.dumps(
            {"scores": scores, "rationale": {"a": "x", "b": "y"}, "feedback": "f"})))
        return NS(choices=[NS(message=NS(content="", tool_calls=[tc]))])
    return NS(choices=[NS(message=NS(content="done", tool_calls=None))])


def _agreement_config(calset_dir):
    return CapabilityConfig(
        name="fake",
        planner=None, generator=None,
        evaluator=RoleConfig(name="evaluator", system_prompt="[[EVALUATOR]] grade it.",
                             tools=[ReadTool, SubmitVerdict]),
        criteria=CRITERIA, calibration_set_dir=calset_dir,
    )


async def test_agreement_rate():
    agentmod.acompletion = fake_eval_acompletion
    d = tempfile.mkdtemp(prefix="calset_")
    # case 1: GOOD -> fake passes, expected pass  => match
    # case 2: bad  -> fake fails, expected fail   => match
    # case 3: GOOD -> fake passes, expected FAIL  => MISS (the evaluator over-scored)
    cases = [
        ("01.json", "GOOD well-cited report", True),
        ("02.json", "weak vague report", False),
        ("03.json", "GOOD looking but expected fail", False),
    ]
    for fname, artifact, passed in cases:
        with open(os.path.join(d, fname), "w") as f:
            json.dump({"label": fname, "artifact": artifact,
                       "expected": {"passed": passed, "scores": {"a": 0.8 if passed else 0.2, "b": 0.9}}}, f)

    report = await calib.agreement_rate(_agreement_config(d))
    assert abs(report.rate - 2 / 3) < 1e-9, report.rate
    misses = [c.label for c in report.cases if not c.match]
    assert misses == ["03.json"], misses
    print(f"5.3 agreement_rate = {report.rate:.0%}, MISS flagged on {misses}: OK")


async def test_agreement_tolerance():
    # With a tolerance, a case matches only if expected scores are within it. The
    # fake gives a=0.8; an expected a=0.2 is outside 0.2 tolerance => MISS.
    agentmod.acompletion = fake_eval_acompletion
    d = tempfile.mkdtemp(prefix="caltol_")
    with open(os.path.join(d, "01.json"), "w") as f:
        json.dump({"label": "loose", "artifact": "GOOD",
                   "expected": {"passed": True, "scores": {"a": 0.2, "b": 0.9}}}, f)
    report = await calib.agreement_rate(_agreement_config(d), tolerance=0.2)
    assert report.rate == 0.0 and report.cases[0].score_deltas["a"] > 0.2
    print("5.3 tolerance mode scores per-criterion delta: OK")


# ---------------- 5.4 divergence-review write step ----------------
def test_label_and_learn_and_pending():
    vstore.reset_default_store()
    os.environ["AGENT_CALIBRATION_DB"] = os.path.join(_TMP, "ll.db")
    store = vstore.default_store()
    # a logged verdict with a snapshotted artifact
    ref = store.snapshot_artifact("run_xyz-r1", "# Report\nsome GOOD text to excerpt")
    vid = store.record(capability="fake", run_id="run_xyz", round_index=1,
                       scores={"a": 0.9}, rationale={}, feedback="", passed=True,
                       weighted=0.9, artifact_ref=ref)
    assert [v["id"] for v in calib.pending_review("fake")] == [vid]   # unlabeled => in queue

    examples_dir = tempfile.mkdtemp(prefix="learn_")
    cfg = CapabilityConfig(name="fake", planner=None, generator=None,
                           criteria=CRITERIA, eval_examples_dir=examples_dir)
    path = calib.label_and_learn(cfg, vid, expected_passed=False,
                                 expected_scores={"a": 0.2, "b": 0.4},
                                 rationale={"a": "missed uncited claims"},
                                 label="over-scored")
    assert os.path.isfile(path)
    learned = calib.load_eval_examples(examples_dir)
    assert len(learned) == 1 and learned[0].verdict == "fail"
    assert "GOOD text" in learned[0].artifact_excerpt        # excerpt pulled from the snapshot
    assert calib.pending_review("fake") == []                # now labeled => out of queue
    print("5.4 label_and_learn writes a corrected exemplar + clears review queue: OK")


async def main():
    test_store_roundtrip()
    test_record_verdict_persists_and_snapshots()
    test_eval_examples_loader_and_render()
    test_deep_research_prompt_injection()
    await test_agreement_rate()
    await test_agreement_tolerance()
    test_label_and_learn_and_pending()
    print("\nALL PHASE 5 CHECKS PASSED")


if __name__ == "__main__":
    asyncio.run(main())
