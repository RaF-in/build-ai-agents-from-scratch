"""The article-writer entry tool — a thin dispatch handle (Phase 8).

Identical SHAPE to deep_research's entry tool: bind the request to a config and call
the one engine, then return the Phase 6 short contract. No pipeline logic lives here.
"""
from __future__ import annotations

import os

from tool_base import AgentContext, AgentTool, ToolResult

_SUMMARY_MAX_CHARS = 600


def _first_meaningful_line(text: str) -> str:
    for line in (text or "").splitlines():
        line = line.strip()
        if line and not line.startswith("#"):
            return line[:_SUMMARY_MAX_CHARS]
    return (text or "").strip()[:_SUMMARY_MAX_CHARS]


class WriteArticleTool(AgentTool):
    """Draft a long-form article or blog post with structure, voice, and sources.

    Use for open-ended writing that needs an outline, a coherent arc, and a distinct
    voice — not a quick paragraph. The work runs as a planned, multi-step pipeline and
    returns the finished article.

    Args:
        topic: What to write about, including any angle, scope, or constraints.
        audience: Who it's for (shapes tone and depth). Defaults to a general reader.
    """
    topic: str
    audience: str = "general"

    async def execute(self, context: AgentContext) -> ToolResult:
        from capabilities.shared.engine import run_pipeline
        from capabilities.article_writer.config import WRITING_CONFIG
        from capabilities.shared.text_tools import extract_sources

        run_ctx = context.new_run_context()
        report = await run_pipeline(
            f"{self.topic} (for {self.audience})", config=WRITING_CONFIG, context=run_ctx
        )

        # Phase 6 output contract: short result; the full draft stays in the workspace.
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
