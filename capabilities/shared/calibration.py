"""Evaluator calibration machinery (Phase 5.2–5.4 — capability-blind).

The article's calibration loop is *prompt* calibration, not weight fine-tuning:
read traces -> find where the evaluator disagreed with you -> encode the fix as
few-shot examples + prompt rules -> repeat. This module is the reusable engine
for that loop; the DATA (few-shot exemplars, the frozen regression set) lives per
capability under its own package, pointed at by ``CapabilityConfig`` fields.

  - 5.2  load/render the few-shot examples a config injects into its evaluator
         prompt (``eval_examples/``).
  - 5.3  run the evaluator on a frozen ``(artifact, expected verdict)`` set and
         report an objective agreement rate ("eval the evaluator").
  - 5.4  list verdicts awaiting human review and append a newly-labeled case back
         into ``eval_examples/`` (the /calibrate-evaluator skill's backend).
"""
from __future__ import annotations

import asyncio
import glob
import json
import os
import re
from dataclasses import dataclass, field
from datetime import datetime, timezone

from .verdict import Verdict
from .verdict_store import VerdictStore, default_store


# ---- 5.2 few-shot calibration examples (data the config ships in its prompt) --

@dataclass
class EvalExample:
    """One calibrated (artifact, scores, rationale) exemplar. Steers judgment and
    kills score drift; shipped inside the evaluator prompt."""
    label: str
    artifact_excerpt: str
    scores: dict = field(default_factory=dict)
    rationale: dict = field(default_factory=dict)
    verdict: str = ""          # "pass" / "fail" — the headline call


def load_eval_examples(dir_path: str | None) -> list[EvalExample]:
    if not dir_path or not os.path.isdir(dir_path):
        return []
    out: list[EvalExample] = []
    for path in sorted(glob.glob(os.path.join(dir_path, "*.json"))):
        try:
            with open(path, encoding="utf-8") as f:
                d = json.load(f)
            out.append(EvalExample(
                label=d.get("label", os.path.basename(path)),
                artifact_excerpt=d.get("artifact_excerpt", ""),
                scores=d.get("scores", {}),
                rationale=d.get("rationale", {}),
                verdict=d.get("verdict", ""),
            ))
        except (OSError, ValueError):
            continue   # a malformed exemplar is skipped, never fatal
    return out


def render_eval_examples(examples: list[EvalExample]) -> str:
    """Format exemplars into a prompt block. Empty string when there are none, so
    the prompt slot disappears cleanly on a fresh (un-calibrated) capability."""
    if not examples:
        return ""
    blocks = [
        "Here are calibrated examples of how to score. Match this judgment — keep "
        "your scores consistent with these reference gradings:",
        "",
    ]
    for ex in examples:
        scores = ", ".join(f"{k}={v}" for k, v in ex.scores.items())
        blocks.append(f"### Example: {ex.label} ({ex.verdict or 'n/a'})")
        if ex.artifact_excerpt:
            blocks.append(f"Artifact excerpt: {ex.artifact_excerpt.strip()}")
        if scores:
            blocks.append(f"Correct scores: {scores}")
        for name, why in ex.rationale.items():
            blocks.append(f"- {name}: {why}")
        blocks.append("")
    return "\n".join(blocks).rstrip() + "\n"


# ---- 5.3 the frozen regression set + the agreement metric --------------------

@dataclass
class CalibrationCase:
    """A frozen, human-labeled test case: an artifact and the verdict YOU expect.
    Never shipped in the prompt — it is held-out, used only to score the evaluator."""
    label: str
    artifact: str
    expected_passed: bool
    expected_scores: dict = field(default_factory=dict)


def load_calibration_set(dir_path: str | None) -> list[CalibrationCase]:
    if not dir_path or not os.path.isdir(dir_path):
        return []
    out: list[CalibrationCase] = []
    for path in sorted(glob.glob(os.path.join(dir_path, "*.json"))):
        try:
            with open(path, encoding="utf-8") as f:
                d = json.load(f)
            expected = d.get("expected", {})
            out.append(CalibrationCase(
                label=d.get("label", os.path.basename(path)),
                artifact=d.get("artifact", ""),
                expected_passed=bool(expected.get("passed", False)),
                expected_scores=expected.get("scores", {}),
            ))
        except (OSError, ValueError):
            continue
    return out


@dataclass
class CaseResult:
    label: str
    expected_passed: bool
    actual_passed: bool
    match: bool
    score_deltas: dict = field(default_factory=dict)   # criterion -> |expected-actual|


@dataclass
class AgreementReport:
    rate: float                       # fraction of cases the evaluator matched you on
    cases: list[CaseResult]
    mean_abs_delta: float             # avg |Δ| across all scored criteria (richer signal)

    def summary(self) -> str:
        lines = [f"agreement rate: {self.rate:.0%} ({len(self.cases)} cases), "
                 f"mean |Δ| {self.mean_abs_delta:.2f}"]
        for c in self.cases:
            mark = "ok " if c.match else "MISS"
            lines.append(f"  [{mark}] {c.label}: expected passed={c.expected_passed}, "
                         f"got {c.actual_passed}")
        return "\n".join(lines)


async def run_evaluator_once(config, artifact_text: str) -> Verdict:
    """Run only the evaluator role over a given artifact, in an isolated workspace.
    No verdict is persisted (calibration runs must not pollute the trace store)."""
    # Lazy imports avoid an engine<->calibration import cycle at module load.
    import asyncio as _asyncio

    from tool_base import AgentContext, RunBudget
    from .artifacts import make_run_workspace, write_artifact
    from .engine import spawn_role, _eval_task
    from .verdict import load_verdict

    ws = make_run_workspace()
    ctx = AgentContext(
        workspace_dir=ws,
        run_semaphore=_asyncio.Semaphore(max(1, config.breadth)),
        run_budget=RunBudget(),
    )
    write_artifact(ctx, "report.md", artifact_text)
    await spawn_role(config.evaluator, task=_eval_task(ctx), context=ctx)
    return load_verdict(ctx) or Verdict()


async def agreement_rate(config, *, tolerance: float | None = None) -> AgreementReport:
    """Re-run the evaluator on the frozen set and measure agreement with your labels.

    Default match = same pass/fail call. With ``tolerance`` set, a case matches only
    if every expected score is within ``tolerance`` of the evaluator's score. This is
    the literal "evaluator of the evaluator" — run it after any evaluator-prompt edit.
    """
    cases = load_calibration_set(getattr(config, "calibration_set_dir", None))
    if not cases:
        return AgreementReport(rate=0.0, cases=[], mean_abs_delta=0.0)

    results: list[CaseResult] = []
    all_deltas: list[float] = []
    for case in cases:
        v = await run_evaluator_once(config, case.artifact)
        actual_passed = v.passed(config.criteria)
        deltas = {
            name: abs(v.score_for(name) - float(exp))
            for name, exp in case.expected_scores.items()
        }
        all_deltas.extend(deltas.values())
        if tolerance is None:
            match = actual_passed == case.expected_passed
        else:
            match = bool(deltas) and all(d <= tolerance for d in deltas.values())
        results.append(CaseResult(
            label=case.label, expected_passed=case.expected_passed,
            actual_passed=actual_passed, match=match, score_deltas=deltas,
        ))

    rate = sum(1 for r in results if r.match) / len(results)
    mean_abs = (sum(all_deltas) / len(all_deltas)) if all_deltas else 0.0
    return AgreementReport(rate=rate, cases=results, mean_abs_delta=mean_abs)


def agreement_rate_sync(config, *, tolerance: float | None = None) -> AgreementReport:
    """Blocking wrapper for the CLI / skill backend."""
    return asyncio.run(agreement_rate(config, tolerance=tolerance))


# ---- 5.4 divergence-review loop backend (drives /calibrate-evaluator) --------

def pending_review(capability: str, *, store: VerdictStore | None = None,
                   limit: int = 20) -> list[dict]:
    """Recent verdicts not yet human-labeled — the review queue the skill shows."""
    store = store or default_store()
    return [v for v in store.recent(capability, limit=limit) if v.get("human_label") is None]


def _slug(text: str) -> str:
    return re.sub(r"[^a-z0-9]+", "-", text.lower()).strip("-") or "example"


def append_eval_example(config, example: EvalExample) -> str:
    """Write a newly-labeled case into the capability's ``eval_examples/`` so it
    steers future grading (the article's loop, made repeatable). Returns the path."""
    dir_path = getattr(config, "eval_examples_dir", None)
    if not dir_path:
        raise ValueError(f"config {config.name!r} has no eval_examples_dir to append to")
    os.makedirs(dir_path, exist_ok=True)
    stamp = datetime.now(timezone.utc).strftime("%Y%m%d-%H%M%S")
    path = os.path.join(dir_path, f"{stamp}-{_slug(example.label)}.json")
    payload = {
        "label": example.label,
        "artifact_excerpt": example.artifact_excerpt,
        "scores": example.scores,
        "rationale": example.rationale,
        "verdict": example.verdict,
    }
    with open(path, "w", encoding="utf-8") as f:
        json.dump(payload, f, indent=2)
    return path


def load_config(capability: str):
    """Resolve a capability name to its built CapabilityConfig by convention
    (``capabilities/<name>/config.py`` exporting one CapabilityConfig instance).
    Lets the generic skill/CLI work for any capability, not just deep_research."""
    import importlib

    from .config import CapabilityConfig

    mod = importlib.import_module(f"capabilities.{capability}.config")
    for value in vars(mod).values():
        if isinstance(value, CapabilityConfig):
            return value
    raise ValueError(f"no CapabilityConfig found in capabilities.{capability}.config")


def label_and_learn(config, verdict_id: int, *, expected_passed: bool,
                    expected_scores: dict, rationale: dict | None = None,
                    label: str | None = None, store: VerdictStore | None = None) -> str:
    """The divergence-review write step: record the human's corrected verdict on the
    logged row AND append it as a new few-shot exemplar so future grading learns from
    it. Returns the new exemplar path."""
    store = store or default_store()
    row = store.get(verdict_id)
    if row is None:
        raise ValueError(f"no verdict with id {verdict_id}")
    store.label(verdict_id, {"passed": expected_passed, "scores": expected_scores})

    excerpt = ""
    ref = row.get("artifact_ref")
    if ref and os.path.isfile(ref):
        with open(ref, encoding="utf-8") as f:
            excerpt = f.read()[:800]
    example = EvalExample(
        label=label or f"corrected-{verdict_id}",
        artifact_excerpt=excerpt,
        scores=expected_scores,
        rationale=rationale or {},
        verdict="pass" if expected_passed else "fail",
    )
    return append_eval_example(config, example)


# ---- CLI the /calibrate-evaluator skill drives ------------------------------

def _parse_scores(text: str) -> dict:
    out: dict[str, float] = {}
    for pair in (text or "").split(","):
        pair = pair.strip()
        if not pair:
            continue
        k, _, v = pair.partition("=")
        out[k.strip()] = float(v)
    return out


def _main(argv: list[str] | None = None) -> int:
    import argparse

    p = argparse.ArgumentParser(prog="calibration", description="Evaluator calibration loop")
    sub = p.add_subparsers(dest="cmd", required=True)

    sp = sub.add_parser("pending", help="list recent verdicts awaiting human review")
    sp.add_argument("capability")
    sp.add_argument("--limit", type=int, default=20)

    sa = sub.add_parser("agreement", help="agreement rate vs the frozen calibration set")
    sa.add_argument("capability")
    sa.add_argument("--tolerance", type=float, default=None)

    sl = sub.add_parser("label", help="record a corrected verdict + add a few-shot example")
    sl.add_argument("capability")
    sl.add_argument("verdict_id", type=int)
    sl.add_argument("--passed", required=True, choices=["0", "1"])
    sl.add_argument("--scores", default="", help="comma list, e.g. coverage=0.3,synthesis=0.8")
    sl.add_argument("--label", default=None)

    args = p.parse_args(argv)

    if args.cmd == "pending":
        for v in pending_review(args.capability, limit=args.limit):
            print(f"#{v['id']} run={v['run_id']} r{v['round_index']} "
                  f"passed={v['passed']} weighted={v['weighted']:.2f} scores={v['scores']}")
            if v.get("artifact_ref"):
                print(f"    artifact: {v['artifact_ref']}")
        return 0

    if args.cmd == "agreement":
        report = agreement_rate_sync(load_config(args.capability), tolerance=args.tolerance)
        print(report.summary())
        return 0

    if args.cmd == "label":
        path = label_and_learn(
            load_config(args.capability), args.verdict_id,
            expected_passed=args.passed == "1",
            expected_scores=_parse_scores(args.scores),
            label=args.label,
        )
        print(f"labeled verdict #{args.verdict_id}; new few-shot example -> {path}")
        return 0

    return 1


if __name__ == "__main__":
    raise SystemExit(_main())
