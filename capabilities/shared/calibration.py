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

Two instruction-level extensions sit on the same machinery (both human-gated, still
prompt calibration — never weight fine-tuning):
  - ext A  record every agreement run against a fingerprint of the exact evaluator
           prompt (``prompt_runs`` table) so a before/after delta is logged, not
           eyeballed; ``--history`` shows the trend.
  - ext B  draft a revised instruction block from the cases the evaluator MISSED and
           show the human a diff; on accept, write it back between fences, re-score,
           and record before/after — a regression is surfaced and revertible.
"""
from __future__ import annotations

import asyncio
import difflib
import glob
import hashlib
import json
import os
import re
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path

from .verdict import Verdict
from .verdict_store import VerdictStore, default_store

# Fences delimiting the only region of a file-backed evaluator prompt the refiner
# (ext B) may rewrite. The {{CRITERIA}}/{{EVAL_EXAMPLES}} slots and the SubmitVerdict
# contract live OUTSIDE them and are out of bounds.
INSTRUCTIONS_BEGIN = "<!-- BEGIN:INSTRUCTIONS -->"
INSTRUCTIONS_END = "<!-- END:INSTRUCTIONS -->"
# Instruction refinement is a low-frequency, reasoning-heavy task → the latest
# capable Claude model, independent of the (smaller) evaluator model.
REFINER_MODEL = "claude-opus-4-8"
_REFINER_PROMPT = Path(__file__).resolve().parent / "prompts" / "refiner.md"


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


# ---- ext A: prompt-version log & agreement history ---------------------------

def _sha256(text: str) -> str:
    return hashlib.sha256((text or "").encode("utf-8")).hexdigest()


def _git_rev() -> str | None:
    """Short HEAD rev, best-effort (None outside a repo / when git is absent)."""
    import subprocess
    try:
        out = subprocess.check_output(
            ["git", "rev-parse", "--short", "HEAD"],
            stderr=subprocess.DEVNULL, text=True,
        ).strip()
        return out or None
    except Exception:
        return None


def prompt_fingerprint(config) -> tuple[str, str, int]:
    """(prompt_hash, instructions_hash, n_examples) for the evaluator config.

    ``prompt_hash`` covers the whole assembled system prompt (so it moves when a
    few-shot example changes); ``instructions_hash`` covers the raw evaluator file
    only (few-shot is a placeholder there, so it moves ONLY on an instruction edit).
    Splitting them is what lets a rate delta be attributed to one cause or the other.
    """
    assembled = (config.evaluator.system_prompt if config.evaluator else "") or ""
    prompt_hash = _sha256(assembled)
    instructions_hash = prompt_hash
    path = getattr(config, "evaluator_prompt_path", None)
    if path and os.path.isfile(path):
        with open(path, encoding="utf-8") as f:
            instructions_hash = _sha256(f.read())
    n_examples = len(load_eval_examples(getattr(config, "eval_examples_dir", None)))
    return prompt_hash, instructions_hash, n_examples


def record_prompt_run(config, report: AgreementReport, *,
                      store: VerdictStore | None = None) -> int:
    """Persist one agreement measurement against the prompt that produced it."""
    store = store or default_store()
    prompt_hash, instructions_hash, n_examples = prompt_fingerprint(config)
    return store.record_prompt_run(
        capability=config.name, prompt_hash=prompt_hash,
        instructions_hash=instructions_hash, n_examples=n_examples,
        agreement_rate=report.rate, mean_abs_delta=report.mean_abs_delta,
        n_cases=len(report.cases), git_rev=_git_rev(),
    )


def prompt_run_history(capability: str, *, store: VerdictStore | None = None,
                       limit: int = 20) -> list[dict]:
    return (store or default_store()).prompt_run_history(capability, limit=limit)


def format_history(rows: list[dict]) -> str:
    """Render the prompt-run trend newest-first, marking instruction-hash changes
    (an evaluator.md edit) vs. few-shot-only changes, with the rate delta."""
    if not rows:
        return "no agreement runs recorded yet"
    lines = ["agreement history (newest first):"]
    for i, r in enumerate(rows):
        prev = rows[i + 1] if i + 1 < len(rows) else None
        if prev is None:
            delta = ""
        else:
            d = r["agreement_rate"] - prev["agreement_rate"]
            delta = f"  Δ{d:+.0%}"
            if r["instructions_hash"] != prev["instructions_hash"]:
                delta += " [instructions edited]"
            elif r["n_examples"] != prev["n_examples"]:
                delta += f" [{r['n_examples'] - prev['n_examples']:+d} examples]"
        rev = f" @{r['git_rev']}" if r.get("git_rev") else ""
        lines.append(
            f"  #{r['id']} {r['created_at']}  rate {r['agreement_rate']:.0%}"
            f"  |Δ| {r['mean_abs_delta']:.2f}  n={r['n_cases']}"
            f"  ex={r['n_examples']}  instr={r['instructions_hash'][:8]}{rev}{delta}"
        )
    return "\n".join(lines)


# ---- ext B: LLM-assisted instruction refinement (human-gated) ----------------

def _instructions_path(config) -> str:
    path = getattr(config, "evaluator_prompt_path", None)
    if not path or not os.path.isfile(path):
        raise ValueError(
            f"config {config.name!r} has no file-backed evaluator_prompt_path; "
            "instruction refinement needs one"
        )
    return path


_FENCE_RE = re.compile(
    re.escape(INSTRUCTIONS_BEGIN) + r"\n?(.*?)\n?" + re.escape(INSTRUCTIONS_END),
    re.DOTALL,
)


def read_instructions(config) -> str:
    """The current text between the INSTRUCTIONS fences of the evaluator prompt."""
    with open(_instructions_path(config), encoding="utf-8") as f:
        text = f.read()
    m = _FENCE_RE.search(text)
    if not m:
        raise ValueError(
            f"{_instructions_path(config)} has no "
            f"{INSTRUCTIONS_BEGIN}…{INSTRUCTIONS_END} region to refine"
        )
    return m.group(1).strip("\n")


def write_instructions(config, new_block: str) -> None:
    """Replace ONLY the fenced region in-place; everything else is untouched."""
    path = _instructions_path(config)
    with open(path, encoding="utf-8") as f:
        text = f.read()
    if not _FENCE_RE.search(text):
        raise ValueError(f"{path} has no INSTRUCTIONS region to write into")
    replacement = f"{INSTRUCTIONS_BEGIN}\n{new_block.strip()}\n{INSTRUCTIONS_END}"
    new_text = _FENCE_RE.sub(lambda _m: replacement, text, count=1)
    with open(path, "w", encoding="utf-8") as f:
        f.write(new_text)


def _calibration_root() -> str:
    return default_store().root


def _prev_instructions_path(capability: str) -> str:
    d = os.path.join(_calibration_root(), "instructions")
    os.makedirs(d, exist_ok=True)
    return os.path.join(d, f"{capability}.prev.md")


def _save_prev_instructions(config, block: str) -> str:
    """Snapshot the pre-edit block so a regressing refinement is one-command revertible."""
    path = _prev_instructions_path(config.name)
    with open(path, "w", encoding="utf-8") as f:
        f.write(block)
    return path


def revert_instructions(config) -> bool:
    """Restore the last snapshotted instruction block. False if none exists."""
    path = _prev_instructions_path(config.name)
    if not os.path.isfile(path):
        return False
    with open(path, encoding="utf-8") as f:
        write_instructions(config, f.read())
    return True


@dataclass
class RefinementProposal:
    current: str                      # the instruction block today
    proposed: str                     # the model's revised block (NOT yet applied)
    diff: str                         # unified diff, current -> proposed
    miss_labels: list[str]            # which calibration cases drove the proposal
    proposal_path: str                # where `proposed` is saved, for --apply
    before: AgreementReport           # agreement at proposal time


@dataclass
class ApplyResult:
    before: AgreementReport
    after: AgreementReport
    prev_path: str                    # snapshot of the replaced block (for revert)
    regressed: bool                   # after.rate < before.rate

    def summary(self) -> str:
        verb = "REGRESSED" if self.regressed else "ok"
        return (f"agreement {self.before.rate:.0%} -> {self.after.rate:.0%} "
                f"[{verb}]  mean |Δ| {self.before.mean_abs_delta:.2f} -> "
                f"{self.after.mean_abs_delta:.2f}")


def _render_miss_brief(config, current: str, before: AgreementReport) -> str:
    """The user message for the refiner: current block + the cases it got wrong,
    each with the human's expected verdict, the evaluator's call, and score deltas."""
    cases = {c.label: c for c in load_calibration_set(
        getattr(config, "calibration_set_dir", None))}
    lines = ["CURRENT INSTRUCTION BLOCK:", current, "",
             "CASES THE EVALUATOR GOT WRONG (it must agree with the human verdict):"]
    for r in before.cases:
        if r.match:
            continue
        deltas = ", ".join(f"{k}=Δ{v:.2f}" for k, v in r.score_deltas.items())
        case = cases.get(r.label)
        excerpt = (case.artifact[:400].strip() + " …") if case and case.artifact else ""
        lines.append(
            f"- {r.label}: human passed={r.expected_passed}, evaluator said "
            f"passed={r.actual_passed}; score deltas: {deltas or 'n/a'}"
        )
        if excerpt:
            lines.append(f"    report excerpt: {excerpt}")
    lines += ["", "Revise the instruction block so these would be graded correctly, "
              "without overfitting to them."]
    return "\n".join(lines)


async def _draft_instructions(config, current: str, before: AgreementReport,
                              *, model: str | None = None) -> str:
    """One refiner LLM call -> the proposed instruction block (text only)."""
    import agent  # call through the module so a test's monkeypatch is honored

    meta = _REFINER_PROMPT.read_text(encoding="utf-8")
    user = _render_miss_brief(config, current, before)
    resp = await agent.acompletion(
        model=model or REFINER_MODEL,
        messages=[{"role": "system", "content": meta},
                  {"role": "user", "content": user}],
    )
    return (resp.choices[0].message.content or "").strip()


def _proposal_path(capability: str) -> str:
    d = os.path.join(_calibration_root(), "proposals")
    os.makedirs(d, exist_ok=True)
    stamp = datetime.now(timezone.utc).strftime("%Y%m%d-%H%M%S")
    return os.path.join(d, f"{capability}-{stamp}.md")


async def propose_refinement(config, *, model: str | None = None) -> RefinementProposal:
    """Score the frozen set, draft an instruction edit from the MISS cases, and
    return a reviewable proposal. NOTHING is written to the prompt — the human
    reviews the diff and applies it separately."""
    before = await agreement_rate(config)
    current = read_instructions(config)
    proposed = await _draft_instructions(config, current, before, model=model)
    diff = "".join(difflib.unified_diff(
        current.splitlines(keepends=True),
        (proposed + "\n").splitlines(keepends=True),
        fromfile="instructions (current)", tofile="instructions (proposed)",
    ))
    miss_labels = [c.label for c in before.cases if not c.match]
    path = _proposal_path(config.name)
    with open(path, "w", encoding="utf-8") as f:
        f.write(proposed)
    return RefinementProposal(current=current, proposed=proposed, diff=diff,
                              miss_labels=miss_labels, proposal_path=path,
                              before=before)


async def apply_refinement(config, proposed_block: str, *,
                           rebuild=None, store: VerdictStore | None = None) -> ApplyResult:
    """Write the (human-approved) block into the fenced region, then re-score and
    record before/after (ext A). The replaced block is snapshotted for revert.

    ``rebuild()`` returns a config rebuilt from disk so the new instructions take
    effect in-process; defaults to reloading the capability's config module."""
    store = store or default_store()
    before = await agreement_rate(config)
    record_prompt_run(config, before, store=store)

    prev_path = _save_prev_instructions(config, read_instructions(config))
    write_instructions(config, proposed_block)

    fresh = rebuild() if rebuild else load_config(config.name, reload=True)
    after = await agreement_rate(fresh)
    record_prompt_run(fresh, after, store=store)
    return ApplyResult(before=before, after=after, prev_path=prev_path,
                       regressed=after.rate < before.rate)


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


def load_config(capability: str, *, reload: bool = False):
    """Resolve a capability name to its built CapabilityConfig by convention
    (``capabilities/<name>/config.py`` exporting one CapabilityConfig instance).
    Lets the generic skill/CLI work for any capability, not just deep_research.

    ``reload=True`` re-imports the module so a just-edited prompt file (ext B) is
    reflected in the freshly-assembled config within the same process."""
    import importlib

    from .config import CapabilityConfig

    mod = importlib.import_module(f"capabilities.{capability}.config")
    if reload:
        mod = importlib.reload(mod)
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
    sa.add_argument("--history", action="store_true",
                    help="show the recorded agreement trend instead of running")

    sr = sub.add_parser("refine", help="LLM-assisted instruction refinement (human-gated)")
    sr.add_argument("capability")
    sr.add_argument("--apply", metavar="PROPOSAL_PATH", default=None,
                    help="apply a previously-proposed instruction block + re-score")
    sr.add_argument("--revert", action="store_true",
                    help="restore the last snapshotted instruction block")
    sr.add_argument("--model", default=None, help=f"refiner model (default {REFINER_MODEL})")

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
        if args.history:
            print(format_history(prompt_run_history(args.capability)))
            return 0
        config = load_config(args.capability)
        report = agreement_rate_sync(config, tolerance=args.tolerance)
        print(report.summary())
        record_prompt_run(config, report)   # ext A: log this run against its prompt
        return 0

    if args.cmd == "refine":
        config = load_config(args.capability, reload=True)
        if args.revert:
            ok = revert_instructions(config)
            print("instructions reverted to the previous block" if ok
                  else "no snapshot to revert to")
            return 0 if ok else 1
        if args.apply:
            with open(args.apply, encoding="utf-8") as f:
                block = f.read()
            result = asyncio.run(apply_refinement(config, block))
            print(result.summary())
            if result.regressed:
                print(f"agreement dropped — revert with:\n"
                      f"  uv run python -m capabilities.shared.calibration "
                      f"refine {args.capability} --revert")
            return 0
        # default: propose only (human reviews the diff, then re-runs with --apply)
        proposal = asyncio.run(propose_refinement(config, model=args.model))
        if not proposal.miss_labels:
            print(f"agreement already {proposal.before.rate:.0%}; no MISS cases to "
                  "refine from. Nothing proposed.")
            return 0
        print(f"MISS cases: {', '.join(proposal.miss_labels)}\n")
        print(proposal.diff or "(model proposed no change)")
        print(f"\nproposed block saved to: {proposal.proposal_path}")
        print("review the diff above, then apply with:\n"
              f"  uv run python -m capabilities.shared.calibration "
              f"refine {args.capability} --apply {proposal.proposal_path}")
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
