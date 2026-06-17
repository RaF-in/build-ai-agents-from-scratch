"""The deep-research entry tool — a thin dispatch handle (Phase 2/3).

Selection of this tool by the main agent's LLM IS the routing decision
("tool selection == capability identification"). The tool contains NO pipeline
logic: it binds the request to RESEARCH_CONFIG and calls the one engine.
"""
from __future__ import annotations

import os

from tool_base import AgentContext, AgentTool, ToolResult

_SUMMARY_MAX_CHARS = 600


def _first_meaningful_line(text: str) -> str:
    """First non-empty, non-heading line of a report — a fallback summary when the
    generator wrote none."""
    for line in (text or "").splitlines():
        line = line.strip()
        if line and not line.startswith("#"):
            return line[:_SUMMARY_MAX_CHARS]
    return (text or "").strip()[:_SUMMARY_MAX_CHARS]


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
        from capabilities.shared.text_tools import extract_sources

        # Isolated depth-0 run context: run_pipeline populates workspace/budget/
        # semaphore on it, so the caller's (main agent's) live context is untouched.
        run_ctx = context.new_run_context()
        report = await run_pipeline(self.brief, config=RESEARCH_CONFIG, context=run_ctx)

        # Phase 6 output contract: return a SHORT result — the generator's own summary
        # plus the on-disk report path and its sources — never the whole report. The
        # full report + findings stay in the run workspace; the main agent reads the
        # file if the user wants the full text.
        ws = run_ctx.workspace_dir
        report_path = os.path.join(ws, "report.md") if ws else None
        if report_path and not os.path.isfile(report_path):
            report_path = None

        summary = ""
        if ws:
            try:
                with open(os.path.join(ws, "summary.md"), encoding="utf-8") as f:
                    summary = f.read().strip()
            except FileNotFoundError:
                pass
        if not summary:
            summary = _first_meaningful_line(report)

        return self.tool_result(result={
            "summary": summary,
            "report_path": report_path,
            "sources": extract_sources(report) if report else [],
        })
