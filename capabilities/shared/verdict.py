"""The evaluator's structured verdict (Phase 4 — the GAN core).

Capability-blind: a Verdict is just per-criterion scores + rationale + actionable
feedback. The *criteria* (names, weights, thresholds) arrive from the config, so
``passed`` is evaluated against ``config.criteria`` rather than any hard-coded set.

How the verdict crosses the subagent boundary (D2): the evaluator subagent calls
the ``SubmitVerdict`` tool (D1 — a typed tool, not free-text JSON), which writes
``verdict.json`` into the shared run workspace. The parent ``run_evaluate`` reads
it back with :func:`load_verdict`. A text fallback parser salvages a verdict from
prose if the model ever answers without calling the tool.
"""
from __future__ import annotations

import json
import os
import re

from pydantic import BaseModel, Field
from rich import print

from tool_base import AgentContext, AgentTool, ToolResult
from .config import Criterion
from .artifacts import write_artifact, read_artifact

VERDICT_FILE = "verdict.json"


class Verdict(BaseModel):
    scores: dict[str, float] = Field(default_factory=dict)     # criterion name -> 0..1
    rationale: dict[str, str] = Field(default_factory=dict)    # why each score (Phase 5 audit)
    feedback: str = ""                                         # concrete fixes for the generator

    def score_for(self, name: str) -> float:
        # A missing or out-of-range score counts as 0 — skeptical by construction.
        try:
            return max(0.0, min(1.0, float(self.scores.get(name, 0.0))))
        except (TypeError, ValueError):
            return 0.0

    def passed(self, criteria: list[Criterion]) -> bool:
        # Hard threshold: ANY criterion below its threshold fails the whole round.
        return all(self.score_for(c.name) >= c.threshold for c in criteria)

    def weighted_score(self, criteria: list[Criterion]) -> float:
        total = sum(c.weight for c in criteria) or 1.0
        return sum(c.weight * self.score_for(c.name) for c in criteria) / total

    def failures(self, criteria: list[Criterion]) -> list[str]:
        return [c.name for c in criteria if self.score_for(c.name) < c.threshold]


class SubmitVerdict(AgentTool):
    """Submit your grading verdict. Call this EXACTLY once, after you have read the
    artifact and scored every criterion.

    Args:
        scores: Map each criterion name to a score in [0, 1].
        rationale: Map each criterion name to a one-line justification.
        feedback: Concrete, actionable fixes the generator should make (most useful
                  when a criterion is below threshold).
    """
    scores: dict[str, float]
    rationale: dict[str, str] = {}
    feedback: str = ""

    async def execute(self, context: AgentContext) -> ToolResult:
        verdict = Verdict(scores=self.scores, rationale=self.rationale, feedback=self.feedback)
        write_artifact(context, VERDICT_FILE, verdict.model_dump_json(indent=2))
        return self.tool_result(result={"recorded": True, "scores": verdict.scores})


# ---- reading the verdict back (parent side) --------------------------------

def load_verdict(context: AgentContext) -> Verdict | None:
    """Read the verdict the evaluator wrote this round; None if it produced none."""
    raw = read_artifact(context, VERDICT_FILE)
    if not raw.strip():
        return None
    try:
        return Verdict.model_validate_json(raw)
    except Exception:
        return _parse_verdict_from_text(raw)   # B-fallback for non-tool answers


def _parse_verdict_from_text(text: str) -> Verdict | None:
    """Salvage a Verdict from prose (the free-text-JSON fallback). Strips markdown
    fences and grabs the largest brace-delimited blob."""
    cleaned = re.sub(r"```(?:json)?", "", text)
    match = re.search(r"\{.*\}", cleaned, re.DOTALL)
    if not match:
        return None
    try:
        data = json.loads(match.group(0))
    except Exception:
        return None
    if not isinstance(data, dict) or "scores" not in data:
        return None
    try:
        return Verdict.model_validate(data)
    except Exception:
        return None


def render_feedback(verdict: Verdict, criteria: list[Criterion]) -> str:
    """A focused feedback artifact for the generator's next round: which criteria
    failed, their scores, and the evaluator's concrete fixes."""
    lines = ["# Evaluator feedback", ""]
    failed = verdict.failures(criteria)
    if failed:
        lines.append("These criteria are below threshold and MUST be fixed:")
        for c in criteria:
            if c.name in failed:
                why = verdict.rationale.get(c.name, "")
                lines.append(
                    f"- {c.name}: scored {verdict.score_for(c.name):.2f} "
                    f"(needs >= {c.threshold}) — {why}".rstrip(" —")
                )
        lines.append("")
    if verdict.feedback.strip():
        lines += ["Specific fixes:", verdict.feedback.strip(), ""]
    return "\n".join(lines)


def record_verdict(verdict: Verdict, config, *, context: AgentContext, round_index: int) -> None:
    """Trace hook (Phase 5.1). Logs a one-line structured summary AND persists the
    verdict to the calibration ``verdicts`` table, snapshotting the graded report so
    the run's workspace can be reaped without losing the calibration material."""
    passed = verdict.passed(config.criteria)
    print(
        f"[Evaluator] round {round_index}: "
        f"weighted={verdict.weighted_score(config.criteria):.2f} "
        f"passed={passed} "
        f"failures={verdict.failures(config.criteria)}"
    )
    try:
        from .verdict_store import default_store
        store = default_store()
        run_id = os.path.basename(context.workspace_dir or "") or "unknown"
        artifact_ref = None
        report = read_artifact(context, "report.md")
        if report.strip():
            artifact_ref = store.snapshot_artifact(f"{run_id}-r{round_index}", report)
        store.record(
            capability=config.name,
            run_id=run_id,
            round_index=round_index,
            scores=verdict.scores,
            rationale=verdict.rationale,
            feedback=verdict.feedback,
            passed=passed,
            weighted=verdict.weighted_score(config.criteria),
            artifact_ref=artifact_ref,
        )
    except Exception as e:
        # Trace logging is best-effort: a calibration-DB hiccup must never break a run.
        print(f"[Evaluator] verdict trace not recorded: {e}")


# Convenience: the evaluator role's tool set.
EVALUATOR_TOOLS: list[type[AgentTool]] = [SubmitVerdict]
