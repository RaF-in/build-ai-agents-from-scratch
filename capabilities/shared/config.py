"""The config-plugin dataclasses (Phase 1 shapes; the discovery/registry contract
is Phase 2). A CapabilityConfig is pure data: it tells the one engine how each
role behaves — prompt, tools, model, budgets, policy — and how to grade the
result. The engine reads these and never learns the domain.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Callable, Literal

from tool_base import AgentTool, RunPolicy
from subagent import SUBAGENT_MODEL, SUBAGENT_TIMEOUT_SECONDS


@dataclass
class Criterion:
    """One named, weighted grading dimension with a fail threshold (Phase 4 uses
    these; defined here so CapabilityConfig is complete)."""
    name: str
    weight: float = 1.0
    threshold: float = 0.0
    description: str = ""


@dataclass
class RoleConfig:
    """How one role (planner / generator / evaluator) is spawned."""
    name: str
    system_prompt: str
    tools: list[type[AgentTool]] = field(default_factory=list)
    model: str = SUBAGENT_MODEL
    max_tool_calls_per_turn: int = 10
    timeout_s: float = SUBAGENT_TIMEOUT_SECONDS
    # A factory (not an instance) so each spawn gets a fresh policy object.
    # The generator passes ``lambda: PlanExecutePolicy()``; others leave None.
    policy_factory: Callable[[], RunPolicy] | None = None
    # The generator gets a fresh TodoList to plan into; others don't need one.
    fresh_todo_list: bool = False


@dataclass
class CapabilityConfig:
    """A whole capability: its three roles plus run-wide dials. Domain-blind to
    the engine — research/coding/writing differ ONLY in these values."""
    name: str
    planner: RoleConfig
    generator: RoleConfig
    evaluator: RoleConfig | None = None
    breadth: int = 5            # per-run worker concurrency (the run semaphore)
    max_rounds: int = 2         # max generator<->evaluator iterations
    verify: Literal["off", "on", "auto"] = "off"  # the evaluator gate (Phase 4/5)
    criteria: list[Criterion] = field(default_factory=list)
    # Phase 5 calibration data (per-capability, set by the plugin to absolute paths).
    # The engine ignores these; the generic calibration machinery reads them to load
    # few-shot exemplars and the frozen regression set. None on capabilities that
    # ship no calibration data yet.
    eval_examples_dir: str | None = None
    calibration_set_dir: str | None = None
    # Phase 5 ext B: the raw evaluator instruction file (before slot injection). The
    # generic instruction-refiner reads/rewrites only the fenced region inside it, and
    # the prompt-version log hashes it as the "instructions only" fingerprint. None on
    # capabilities whose evaluator prompt is not file-backed.
    evaluator_prompt_path: str | None = None

    def telemetry_attributes(self) -> dict:
        """Tracing attributes for this capability — one source of truth, dotted
        keys. Stamped onto the ``capability.pipeline`` span (Phase 3.1)."""
        return {
            "capability.name": self.name,
            "capability.breadth": self.breadth,
            "capability.max_rounds": self.max_rounds,
            "capability.verify": self.verify,
        }
