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
from capabilities.deep_research.criteria import RESEARCH_CRITERIA

_PROMPTS = Path(__file__).resolve().parent / "prompts"


def _prompt(name: str) -> str:
    return (_PROMPTS / name).read_text(encoding="utf-8")


def _criteria_block() -> str:
    return "\n".join(
        f"- {c.name} (weight {c.weight}, min {c.threshold}): {c.description}"
        for c in RESEARCH_CRITERIA
    )


def build_research_config() -> CapabilityConfig:
    # Imported here (not at module top) so discovery never pulls these in.
    from agent_tools import ReadTool, WriteTool, EditTool
    from subagent_tools import DelegateTasksTool

    criteria = _criteria_block()

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
        # Domain search tools (SearchTool/FetchTool) are added in Phase 3; for now the
        # generator plans, delegates, and writes with the shared file/todo tools.
        tools=[*TODO_TOOLS, DelegateTasksTool, ReadTool, WriteTool, EditTool],
        policy_factory=PlanExecutePolicy,
        fresh_todo_list=True,
    )
    evaluator = RoleConfig(
        name="evaluator",
        system_prompt=_prompt("evaluator.md").replace("{{CRITERIA}}", criteria),
        tools=[ReadTool],
    )

    return CapabilityConfig(
        name="deep_research",
        planner=planner,
        generator=generator,
        evaluator=evaluator,
        breadth=5,
        max_rounds=2,
        # Evaluator stays OFF until Phase 4 implements real criteria scoring + the
        # `auto` gate; until then plan→gen returns the report directly.
        verify="off",
        criteria=RESEARCH_CRITERIA,
    )


RESEARCH_CONFIG = build_research_config()
