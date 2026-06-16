"""The deep-research entry tool — a thin dispatch handle (Phase 2/3).

Selection of this tool by the main agent's LLM IS the routing decision
("tool selection == capability identification"). The tool contains NO pipeline
logic: it binds the request to RESEARCH_CONFIG and calls the one engine.
"""
from __future__ import annotations

from tool_base import AgentContext, AgentTool, ToolResult


class DeepResearchTool(AgentTool):
    """Research a topic in depth and return a thorough, cited report.

    Use for open-ended questions that need multi-source investigation and
    synthesis — not quick factual lookups. The work runs as a planned,
    multi-step pipeline and returns a written report.

    Args:
        brief: What to research, including any scope, timeframe, or focus.
    """
    brief: str

    async def execute(self, context: AgentContext) -> ToolResult:
        # Lazy imports keep capability DISCOVERY cheap (only this class is needed
        # to register the tool) and avoid the import cycle
        # engine -> subagent_tools -> agent_tools at load time.
        from capabilities.shared.engine import run_pipeline
        from capabilities.deep_research.config import RESEARCH_CONFIG

        # Isolated depth-0 run context: run_pipeline populates workspace/budget/
        # semaphore on it, so the caller's (main agent's) live context is untouched.
        run_ctx = context.new_run_context()
        report = await run_pipeline(self.brief, config=RESEARCH_CONFIG, context=run_ctx)
        return self.tool_result(result={"content": report})
