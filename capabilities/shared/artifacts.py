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
import shutil
import tempfile
import time

from tool_base import AgentContext

ARTIFACT_ROOT = os.path.join(tempfile.gettempdir(), "agent_runs")
FINAL_ARTIFACT_CANDIDATES = ("report.md", "feedback.md", "spec.md")

# Phase 7.5: per-run workspaces are reaped once older than this TTL (override via the
# AGENT_WORKSPACE_TTL_S env var). Default 6h — long enough that the entry tool's
# returned report_path stays valid well past the run, short enough to bound disk use.
WORKSPACE_TTL_S = int(os.getenv("AGENT_WORKSPACE_TTL_S", str(6 * 3600)))


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


def schedule_workspace_cleanup(
    workspace_dir: str | None,
    *,
    ttl_s: int | None = None,
    _now: float | None = None,
) -> int:
    """Phase 7.5 TTL sweep: reap ``run_*`` workspaces under ARTIFACT_ROOT older than
    the TTL. The just-finished ``workspace_dir`` is SPARED — its files are fresh and
    the entry tool still returns its ``report_path``; a later run reaps it once it
    ages out. Best-effort: I/O errors are swallowed so cleanup never breaks a run.
    Returns the number of workspaces removed (for tests/observability)."""
    ttl = WORKSPACE_TTL_S if ttl_s is None else ttl_s
    now = time.time() if _now is None else _now
    removed = 0
    try:
        names = os.listdir(ARTIFACT_ROOT)
    except FileNotFoundError:
        return 0
    for name in names:
        if not name.startswith("run_"):
            continue
        path = os.path.join(ARTIFACT_ROOT, name)
        if path == workspace_dir or not os.path.isdir(path):
            continue   # spare the current run; skip non-dirs
        try:
            if now - os.path.getmtime(path) > ttl:
                shutil.rmtree(path, ignore_errors=True)
                removed += 1
        except OSError:
            continue
    return removed
