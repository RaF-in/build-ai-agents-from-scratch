"""Subagent registry — bounds concurrency and records run history.

This is the 'process pool' for child agents. Three constraints live here
or are enforced by callers:
  - Depth: structural (children don't get SpawnSubagentTool) — see subagent_tools.py
  - Concurrency: this registry caps simultaneous active runs
  - Timeout: callers wrap child execution in asyncio.wait_for()

Retry-with-backoff (synchronous) is layered on top by the spawn tool:
genuine child failures are retried with exponential delay; timeouts fail
fast by default so the parent's turn is never frozen for minutes.
"""
from __future__ import annotations

import time
import uuid
from dataclasses import dataclass, field
from typing import Literal

MAX_CONCURRENT_SUBAGENTS = 3
SUBAGENT_TIMEOUT_SECONDS = 300  # 5 minutes
SUBAGENT_MODEL = "huggingface/zai-org/GLM-5.1"
MAX_HISTORY = 50

# Retry-with-backoff
MAX_RETRIES = 2            # total attempts = 1 + MAX_RETRIES
RETRY_BASE_DELAY = 2.0     # seconds; delay = base * 2**(attempt-1)
RETRY_ON_TIMEOUT = False   # timeouts fail fast by default

RunStatus = Literal["running", "completed", "failed", "expired", "rejected"]

CHILD_SYSTEM_PROMPT = (
    "You are a subagent: a focused worker spawned to complete ONE specific task. "
    "You have only file and shell tools (read, write, edit, bash). "
    "You cannot spawn further subagents. Work the task to completion, then end your "
    "turn with a concise plain-text summary of what you found or did — that summary "
    "is the ONLY thing returned to the agent that spawned you, so make it self-contained. "
    "Do not ask questions; you cannot receive a reply. Always use absolute paths."
)


@dataclass
class RunInfo:
    id: str
    task: str
    status: RunStatus = "running"
    started_at: float = field(default_factory=time.time)
    finished_at: float | None = None
    result_preview: str = ""
    attempts: int = 0  # how many child runs were spawned for this logical run

    @property
    def duration(self) -> float:
        return (self.finished_at or time.time()) - self.started_at


class SubagentRegistry:
    def __init__(self, max_concurrent: int = MAX_CONCURRENT_SUBAGENTS):
        self.max_concurrent = max_concurrent
        self.active: dict[str, RunInfo] = {}
        self.history: list[RunInfo] = []

    def try_acquire(self, task: str) -> RunInfo | None:
        """Concurrency cap check. Returns a RunInfo slot, or None if full.

        Synchronous on purpose: there is no ``await`` between the check and the
        insert, so the slot can't be double-allocated on a single-threaded loop.
        """
        if len(self.active) >= self.max_concurrent:
            return None
        run = RunInfo(id=uuid.uuid4().hex[:8], task=task)
        self.active[run.id] = run
        return run

    def complete(self, run: RunInfo, *, status: RunStatus, result: str) -> None:
        run.status = status
        run.finished_at = time.time()
        run.result_preview = (result or "").strip().replace("\n", " ")[:160]
        self.active.pop(run.id, None)
        self.history.append(run)
        if len(self.history) > MAX_HISTORY:
            self.history = self.history[-MAX_HISTORY:]


subagent_registry = SubagentRegistry()
