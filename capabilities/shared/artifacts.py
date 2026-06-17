"""Per-run workspace + artifact helpers (Phase 1).

Phases hand off through FILES in one per-run directory (the article's "structured
artifacts, not chat"): the planner writes spec.md, the generator writes
findings/*.md and the final report.md, the evaluator writes feedback.md. Only
short summaries flow back through tool results; the payload stays on disk.

The workspace is created by run_pipeline() and assigned to context.workspace_dir,
which child_context() shares down the whole role/worker subtree (Phase 0.2).
"""
from __future__ import annotations

import os
import tempfile

from tool_base import AgentContext

ARTIFACT_ROOT = os.path.join(tempfile.gettempdir(), "agent_runs")
FINAL_ARTIFACT_CANDIDATES = ("report.md", "feedback.md", "spec.md")


def make_run_workspace() -> str:
    """Create and return a fresh per-run scratch directory."""
    os.makedirs(ARTIFACT_ROOT, exist_ok=True)
    return tempfile.mkdtemp(prefix="run_", dir=ARTIFACT_ROOT)


def artifact_path(context: AgentContext, name: str) -> str:
    if not context.workspace_dir:
        raise RuntimeError("No run workspace on this context (not inside a pipeline run).")
    return os.path.join(context.workspace_dir, name)


def write_artifact(context: AgentContext, name: str, content: str) -> str:
    path = artifact_path(context, name)
    parent = os.path.dirname(path)
    if parent:
        os.makedirs(parent, exist_ok=True)
    with open(path, "w", encoding="utf-8") as f:
        f.write(content or "")
    return path


def read_artifact(context: AgentContext, name: str) -> str:
    """Read an artifact; returns "" if it does not exist yet (graceful handoff)."""
    try:
        with open(artifact_path(context, name), "r", encoding="utf-8") as f:
            return f.read()
    except FileNotFoundError:
        return ""


def remove_artifact(context: AgentContext, name: str) -> None:
    """Delete an artifact if present (e.g. clear last round's verdict.json before a
    fresh evaluator round so a stale file is never mistaken for this round's)."""
    try:
        os.remove(artifact_path(context, name))
    except FileNotFoundError:
        pass


def read_final_artifact(workspace_dir: str | None) -> str:
    """The run's deliverable: the first of report.md / feedback.md / spec.md that
    exists. Falls back to a note when a (possibly budget-truncated) run produced
    no artifact, so run_pipeline never returns nothing."""
    if not workspace_dir:
        return "(no run workspace)"
    for name in FINAL_ARTIFACT_CANDIDATES:
        path = os.path.join(workspace_dir, name)
        if os.path.isfile(path):
            with open(path, "r", encoding="utf-8") as f:
                return f.read()
    return "(run produced no artifact)"


def schedule_workspace_cleanup(workspace_dir: str | None) -> None:
    """TTL sweep hook — fully built in Phase 7. A no-op today so run_pipeline's
    finally clause is already wired; workspaces live under ARTIFACT_ROOT and are
    cheap to reap later."""
    return None
