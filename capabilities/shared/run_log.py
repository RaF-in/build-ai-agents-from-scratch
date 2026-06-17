"""Per-run observability (Phase 7.4): one structured JSON record per pipeline run.

Written beside the calibration data (``~/.agent/calibration/runs/<run_id>.json``,
``AGENT_CALIBRATION_DB``-aware) so it survives the per-run workspace being reaped
(7.5) and feeds Phase 5. Capability-blind and best-effort: ``run_pipeline`` calls
this in its ``finally``, and a logging failure never propagates into the run.
"""
from __future__ import annotations

import json
import os


def runs_dir() -> str:
    """Telemetry dir, honoring AGENT_CALIBRATION_DB so tests redirect to tmp too."""
    db = os.getenv("AGENT_CALIBRATION_DB")
    root = os.path.dirname(db) if db else os.path.join(
        os.path.expanduser("~"), ".agent", "calibration"
    )
    path = os.path.join(root, "runs")
    os.makedirs(path, exist_ok=True)
    return path


def write_run_log(record: dict) -> str | None:
    """Persist one run record as JSON; returns the path, or None on any failure."""
    try:
        run_id = record.get("run_id") or "run"
        path = os.path.join(runs_dir(), f"{run_id}.json")
        with open(path, "w", encoding="utf-8") as f:
            json.dump(record, f, indent=2)
        return path
    except Exception:
        return None
