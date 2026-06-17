"""Persistent verdict trace store (Phase 5.1 — capability-blind).

Every Verdict the evaluator submits is logged to a small SQLite table so the
calibration loop has raw material to review. This is deliberately NOT the async
chat-session DB (``session.py``): calibration data outlives chats, must survive
the per-run workspace being reaped (Phase 7 TTL sweep), and is written by a
synchronous, fast append from inside ``record_verdict``.

Layout (override the root with the ``AGENT_CALIBRATION_DB`` env var):
    ~/.agent/calibration/verdicts.db        # the table below
    ~/.agent/calibration/artifacts/<run>.md # snapshot of the graded report.md

The store is capability-blind: every row is tagged with ``capability`` so one DB
serves deep_research, coding, writing, etc.
"""
from __future__ import annotations

import json
import os
import sqlite3
from datetime import datetime, timezone

_DEFAULT_DIR = os.path.join(os.path.expanduser("~"), ".agent", "calibration")

_SCHEMA = """
CREATE TABLE IF NOT EXISTS verdicts (
    id            INTEGER PRIMARY KEY AUTOINCREMENT,
    capability    TEXT NOT NULL,
    run_id        TEXT NOT NULL,
    round_index   INTEGER NOT NULL,
    created_at    TEXT NOT NULL,
    scores_json   TEXT NOT NULL,
    rationale_json TEXT NOT NULL,
    feedback      TEXT,
    passed        INTEGER NOT NULL,
    weighted      REAL NOT NULL,
    artifact_ref  TEXT,
    human_label_json TEXT
);
CREATE INDEX IF NOT EXISTS idx_verdicts_cap_id ON verdicts (capability, id);

-- Phase 5 ext A: one row per `agreement` run, fingerprinting the exact evaluator
-- prompt that produced the rate so a before/after delta is recorded, not eyeballed.
CREATE TABLE IF NOT EXISTS prompt_runs (
    id                INTEGER PRIMARY KEY AUTOINCREMENT,
    capability        TEXT NOT NULL,
    created_at        TEXT NOT NULL,
    prompt_hash       TEXT NOT NULL,   -- sha256 of the assembled evaluator system prompt
    instructions_hash TEXT NOT NULL,   -- sha256 of evaluator.md ONLY (few-shot excluded)
    n_examples        INTEGER NOT NULL,-- few-shot exemplars present in the prompt
    agreement_rate    REAL NOT NULL,
    mean_abs_delta    REAL NOT NULL,
    n_cases           INTEGER NOT NULL,
    git_rev           TEXT
);
CREATE INDEX IF NOT EXISTS idx_prompt_runs_cap_id ON prompt_runs (capability, id);
"""


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


class VerdictStore:
    """A thin synchronous SQLite log of evaluator verdicts + human labels."""

    def __init__(self, db_path: str | None = None):
        # Precedence: explicit arg > env var > default. The env var names the DB
        # FILE; the artifacts dir is its sibling so a test can point both at tmp.
        self.db_path = db_path or os.getenv("AGENT_CALIBRATION_DB") or os.path.join(
            _DEFAULT_DIR, "verdicts.db"
        )
        self.root = os.path.dirname(self.db_path) or "."
        self.artifacts_dir = os.path.join(self.root, "artifacts")
        os.makedirs(self.artifacts_dir, exist_ok=True)
        self._init_db()

    def _connect(self) -> sqlite3.Connection:
        conn = sqlite3.connect(self.db_path)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA journal_mode=WAL")
        return conn

    def _init_db(self) -> None:
        with self._connect() as conn:
            conn.executescript(_SCHEMA)

    # ---- writing -----------------------------------------------------------

    def snapshot_artifact(self, run_id: str, text: str) -> str:
        """Persist a copy of the graded artifact (the run workspace is reaped)."""
        path = os.path.join(self.artifacts_dir, f"{run_id}.md")
        with open(path, "w", encoding="utf-8") as f:
            f.write(text or "")
        return path

    def record(
        self,
        *,
        capability: str,
        run_id: str,
        round_index: int,
        scores: dict,
        rationale: dict,
        feedback: str,
        passed: bool,
        weighted: float,
        artifact_ref: str | None = None,
    ) -> int:
        with self._connect() as conn:
            cur = conn.execute(
                "INSERT INTO verdicts (capability, run_id, round_index, created_at, "
                "scores_json, rationale_json, feedback, passed, weighted, artifact_ref, "
                "human_label_json) VALUES (?,?,?,?,?,?,?,?,?,?,NULL)",
                (
                    capability, run_id, round_index, _now_iso(),
                    json.dumps(scores), json.dumps(rationale), feedback or "",
                    int(bool(passed)), float(weighted), artifact_ref,
                ),
            )
            return int(cur.lastrowid)

    def label(self, verdict_id: int, expected: dict) -> None:
        """Attach a human 'I disagree, the correct verdict is X' label to a row."""
        with self._connect() as conn:
            conn.execute(
                "UPDATE verdicts SET human_label_json = ? WHERE id = ?",
                (json.dumps(expected), verdict_id),
            )

    def record_prompt_run(
        self,
        *,
        capability: str,
        prompt_hash: str,
        instructions_hash: str,
        n_examples: int,
        agreement_rate: float,
        mean_abs_delta: float,
        n_cases: int,
        git_rev: str | None = None,
    ) -> int:
        """Log an agreement-rate measurement against the prompt that produced it."""
        with self._connect() as conn:
            cur = conn.execute(
                "INSERT INTO prompt_runs (capability, created_at, prompt_hash, "
                "instructions_hash, n_examples, agreement_rate, mean_abs_delta, "
                "n_cases, git_rev) VALUES (?,?,?,?,?,?,?,?,?)",
                (
                    capability, _now_iso(), prompt_hash, instructions_hash,
                    int(n_examples), float(agreement_rate), float(mean_abs_delta),
                    int(n_cases), git_rev,
                ),
            )
            return int(cur.lastrowid)

    # ---- reading -----------------------------------------------------------

    def get(self, verdict_id: int) -> dict | None:
        with self._connect() as conn:
            row = conn.execute(
                "SELECT * FROM verdicts WHERE id = ?", (verdict_id,)
            ).fetchone()
            return _row_to_dict(row) if row else None

    def recent(self, capability: str, limit: int = 20) -> list[dict]:
        with self._connect() as conn:
            rows = conn.execute(
                "SELECT * FROM verdicts WHERE capability = ? ORDER BY id DESC LIMIT ?",
                (capability, limit),
            ).fetchall()
            return [_row_to_dict(r) for r in rows]

    def prompt_run_history(self, capability: str, limit: int = 20) -> list[dict]:
        """Past agreement runs for a capability, newest first (the trend `--history`)."""
        with self._connect() as conn:
            rows = conn.execute(
                "SELECT * FROM prompt_runs WHERE capability = ? ORDER BY id DESC LIMIT ?",
                (capability, limit),
            ).fetchall()
            return [dict(r) for r in rows]


def _row_to_dict(row: sqlite3.Row) -> dict:
    d = dict(row)
    d["scores"] = json.loads(d.pop("scores_json") or "{}")
    d["rationale"] = json.loads(d.pop("rationale_json") or "{}")
    d["passed"] = bool(d["passed"])
    hl = d.pop("human_label_json", None)
    d["human_label"] = json.loads(hl) if hl else None
    return d


_DEFAULT_STORE: VerdictStore | None = None


def default_store() -> VerdictStore:
    """Process-wide singleton (honors AGENT_CALIBRATION_DB at first use)."""
    global _DEFAULT_STORE
    if _DEFAULT_STORE is None:
        _DEFAULT_STORE = VerdictStore()
    return _DEFAULT_STORE


def reset_default_store() -> None:
    """Test seam: drop the cached singleton so a new env var path takes effect."""
    global _DEFAULT_STORE
    _DEFAULT_STORE = None
