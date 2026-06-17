"""RESEARCH_CONFIG — the deep-research capability as pure data.

This wires the three RoleConfigs (planner / generator / evaluator) with their
prompts, tools, and the shared criteria, then hands the result to the one engine.
No control flow lives here; it is the article's "capability = config object."

Imported lazily by entry.py at execute time, so the (heavier) tool imports here
never run during capability discovery.
"""
from __future__ import annotations

from pathlib import Path

from capabilities.shared.config import RoleConfig, CapabilityConfig
from capabilities.shared.policies import PlanExecutePolicy
from capabilities.shared.todo import TODO_TOOLS
from capabilities.shared.verdict import SubmitVerdict
from capabilities.shared.calibration import load_eval_examples, render_eval_examples
from capabilities.deep_research.criteria import RESEARCH_CRITERIA

_PKG = Path(__file__).resolve().parent
_PROMPTS = _PKG / "prompts"
_EVAL_EXAMPLES_DIR = _PKG / "eval_examples"
_CALIBRATION_SET_DIR = _PKG / "calibration_set"


def _prompt(name: str) -> str:
    return (_PROMPTS / name).read_text(encoding="utf-8")


def _criteria_block() -> str:
    return "\n".join(
        f"- {c.name} (weight {c.weight}, min {c.threshold}): {c.description}"
        for c in RESEARCH_CRITERIA
    )


def build_research_config() -> CapabilityConfig:
    # Imported here (not at module top) so discovery never pulls these in. Search
    # reach is the GENERIC web layer — this config only references it.
    from agent_tools import ReadTool, WriteTool, EditTool
    from web.tools import DelegateWebSearchTool

    criteria = _criteria_block()
    # Phase 5.2: the calibrated few-shot exemplars this capability ships are loaded
    # from its own eval_examples/ and injected into the evaluator prompt — the same
    # file-driven pattern as {{CRITERIA}}. The engine never sees this; it is data.
    eval_examples = render_eval_examples(load_eval_examples(str(_EVAL_EXAMPLES_DIR)))

    planner = RoleConfig(
        name="planner",
        system_prompt=_prompt("planner.md"),
        tools=[],                      # planner only thinks; its text IS the spec
        max_tool_calls_per_turn=6,
    )
    generator = RoleConfig(
        name="generator",
        # Same criteria injected here AND into the evaluator — steers before feedback.
        system_prompt=_prompt("generator.md").replace("{{CRITERIA}}", criteria),
        # The generator plans + synthesizes; it fans out actual web search/fetch to
        # workers via DelegateWebSearchTool (Phase 3.3) so only compressed worker
        # summaries — never raw untrusted page content — enter its context.
        tools=[*TODO_TOOLS, DelegateWebSearchTool, ReadTool, WriteTool, EditTool],
        policy_factory=PlanExecutePolicy,
        fresh_todo_list=True,
    )
    evaluator = RoleConfig(
        name="evaluator",
        system_prompt=(
            _prompt("evaluator.md")
            .replace("{{CRITERIA}}", criteria)
            .replace("{{EVAL_EXAMPLES}}", eval_examples)
        ),
        # ReadTool to read report.md; SubmitVerdict to return a structured score.
        tools=[ReadTool, SubmitVerdict],
    )

    return CapabilityConfig(
        name="deep_research",
        planner=planner,
        generator=generator,
        evaluator=evaluator,
        breadth=5,
        max_rounds=2,
        # Phase 4: the skeptical evaluator now scores against RESEARCH_CRITERIA and
        # gates on `auto` — it runs on non-trivial briefs (>=3 sub-questions) and is
        # skipped on trivial ones.
        verify="auto",
        criteria=RESEARCH_CRITERIA,
        # Phase 5: where this capability's calibration data lives. The generic
        # agreement-rate runner and /calibrate-evaluator skill read these paths.
        eval_examples_dir=str(_EVAL_EXAMPLES_DIR),
        calibration_set_dir=str(_CALIBRATION_SET_DIR),
        # Phase 5 ext B: the refiner edits ONLY the fenced INSTRUCTIONS region here.
        evaluator_prompt_path=str(_PROMPTS / "evaluator.md"),
    )


RESEARCH_CONFIG = build_research_config()
