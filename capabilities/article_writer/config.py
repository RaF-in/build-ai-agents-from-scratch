"""WRITING_CONFIG — the article-writer capability as pure data (Phase 8).

This is the acceptance test for "future capabilities reuse this fully": it wires the
SAME three RoleConfigs to the SAME shared engine, web layer, criteria machinery, and
generic tools (CheckCitationsTool / CountWordsTool, Phase 6) — differing from
deep_research ONLY in its prompts + criteria. No engine or kernel edit was needed to
add it. Calibration (Phase 5) and resilience/observability/cleanup (Phase 7) come
along for free because they are all keyed by ``config.name`` + optional fields.

Imported lazily by entry.py at execute time, mirroring deep_research/config.py.
"""
from __future__ import annotations

from pathlib import Path

from capabilities.shared.config import RoleConfig, CapabilityConfig
from capabilities.shared.policies import PlanExecutePolicy
from capabilities.shared.todo import TODO_TOOLS
from capabilities.shared.verdict import SubmitVerdict
from capabilities.shared.calibration import load_eval_examples, render_eval_examples
from capabilities.article_writer.criteria import WRITING_CRITERIA

_PKG = Path(__file__).resolve().parent
_PROMPTS = _PKG / "prompts"
_EVAL_EXAMPLES_DIR = _PKG / "eval_examples"
_CALIBRATION_SET_DIR = _PKG / "calibration_set"


def _prompt(name: str) -> str:
    return (_PROMPTS / name).read_text(encoding="utf-8")


def _criteria_block() -> str:
    return "\n".join(
        f"- {c.name} (weight {c.weight}, min {c.threshold}): {c.description}"
        for c in WRITING_CRITERIA
    )


def build_writing_config() -> CapabilityConfig:
    # Generic tools, resolved at build time — the same web + text tools deep_research
    # uses. This config only references them; it ships none of its own tool code.
    from agent_tools import ReadTool, WriteTool, EditTool
    from web.tools import DelegateWebSearchTool
    from capabilities.shared.text_tools import CheckCitationsTool, CountWordsTool

    criteria = _criteria_block()
    eval_examples = render_eval_examples(load_eval_examples(str(_EVAL_EXAMPLES_DIR)))

    planner = RoleConfig(
        name="planner",
        system_prompt=_prompt("planner.md"),
        tools=[],                          # planner only thinks; its text IS the outline
        max_tool_calls_per_turn=6,
    )
    generator = RoleConfig(
        name="generator",
        system_prompt=_prompt("generator.md").replace("{{CRITERIA}}", criteria),
        tools=[*TODO_TOOLS, DelegateWebSearchTool, ReadTool, WriteTool, EditTool,
               CheckCitationsTool, CountWordsTool],
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
        tools=[ReadTool, SubmitVerdict],
    )

    return CapabilityConfig(
        name="article_writer",
        planner=planner,
        generator=generator,
        evaluator=evaluator,
        breadth=3,
        max_rounds=2,
        verify="auto",
        criteria=WRITING_CRITERIA,
        # Phase 5 calibration data lives here; the generic loop reads these paths.
        eval_examples_dir=str(_EVAL_EXAMPLES_DIR),
        calibration_set_dir=str(_CALIBRATION_SET_DIR),
        evaluator_prompt_path=str(_PROMPTS / "evaluator.md"),
    )


WRITING_CONFIG = build_writing_config()
