"""Generic document tools: native word count + a deterministic citation linter.

Capability-agnostic on purpose — any sourced-writing capability (research today,
article_writer tomorrow) wants to (a) check a draft's length without shelling out
to ``wc`` (the research runtime has no BashTool, Phase 0.1), and (b) find prose
that asserts a fact with no inline citation. A capability opts in by listing these
in a role's tool set; nothing here is added to the main agent automatically.

The citation check is conservative and structural, not semantic: it flags any body
prose line of >= ``MIN_CLAIM_WORDS`` words that carries no citation marker (a URL or
a ``[ref]`` bracket), skipping headings, code blocks, tables, and the Sources
section. It is an advisory signal the generator repairs against — over-citing is
safe; the evaluator (Phase 4/5) judges nuance.
"""
from __future__ import annotations

import os
import re

from tool_base import AgentContext, AgentTool, ToolResult

MIN_CLAIM_WORDS = 6

_URL_RE = re.compile(r"https?://", re.IGNORECASE)
_BRACKET_RE = re.compile(r"\[[^\]]+\]")          # [1], [src], [bench.example/x], [text](url)
_WORD_RE = re.compile(r"\w+")
_SOURCES_HEADING_RE = re.compile(r"^#{1,6}\s*sources\b", re.IGNORECASE)
_HEADING_RE = re.compile(r"^#{1,6}\s")
_BULLET_RE = re.compile(r"^\s*(?:[-*+]|\d+[.)])\s+")


def _has_citation(line: str) -> bool:
    return bool(_URL_RE.search(line) or _BRACKET_RE.search(line))


def _scan(markdown: str) -> tuple[list[str], list[str]]:
    """Single pass over a markdown doc -> (uncited_claim_lines, source_entries).

    ``source_entries`` are the lines under a ``Sources`` heading (bullets/numbering
    stripped, deduplicated, order preserved)."""
    uncited: list[str] = []
    sources: list[str] = []
    seen_sources: set[str] = set()
    in_code = False
    in_sources = False

    for raw in (markdown or "").splitlines():
        line = raw.strip()
        if line.startswith("```"):
            in_code = not in_code
            continue
        if in_code or not line:
            continue
        if _HEADING_RE.match(line):
            in_sources = bool(_SOURCES_HEADING_RE.match(line))
            continue
        if in_sources:
            entry = _BULLET_RE.sub("", line).strip()
            if entry and entry not in seen_sources:
                seen_sources.add(entry)
                sources.append(entry)
            continue
        if line.startswith("|"):                 # table row — not a prose claim
            continue
        body = _BULLET_RE.sub("", line).strip()
        if _URL_RE.fullmatch(body) or not body:  # a bare reference line
            continue
        if len(_WORD_RE.findall(body)) < MIN_CLAIM_WORDS:
            continue                              # too short to be a factual claim
        if not _has_citation(body):
            uncited.append(body)
    return uncited, sources


def extract_sources(markdown: str) -> list[str]:
    """The deduplicated source entries from a doc's ``## Sources`` section."""
    return _scan(markdown)[1]


def find_uncited_claims(markdown: str) -> list[str]:
    """Body prose lines that assert something but carry no inline citation."""
    return _scan(markdown)[0]


def _resolve(context: AgentContext, path: str) -> str:
    if os.path.isabs(path):
        return path
    if not context.workspace_dir:
        raise RuntimeError("relative path needs a run workspace")
    return os.path.join(context.workspace_dir, path)


class CountWordsTool(AgentTool):
    """Count the words in a file (use instead of a shell `wc`).

    Args:
        path: File to count, absolute or relative to your run workspace.
    """
    path: str = "report.md"

    async def execute(self, context: AgentContext) -> ToolResult:
        try:
            full = _resolve(context, self.path)
            with open(full, encoding="utf-8") as f:
                text = f.read()
        except (OSError, RuntimeError) as exc:
            return self.tool_result(error=True, result={"error": str(exc)})
        return self.tool_result(result={
            "path": self.path,
            "words": len(_WORD_RE.findall(text)),
        })


class CheckCitationsTool(AgentTool):
    """Lint a report for factual claims that lack an inline citation.

    Reads the file and returns any prose lines that assert something without a
    citation marker (a URL or a `[ref]`), plus the parsed Sources list. Fix every
    flagged claim (add a real inline citation or remove it) and re-run until
    `uncited` is empty.

    Args:
        path: The report to check, absolute or relative to your run workspace.
    """
    path: str = "report.md"

    async def execute(self, context: AgentContext) -> ToolResult:
        try:
            full = _resolve(context, self.path)
            with open(full, encoding="utf-8") as f:
                text = f.read()
        except (OSError, RuntimeError) as exc:
            return self.tool_result(error=True, result={"error": str(exc)})
        uncited, sources = _scan(text)
        return self.tool_result(result={
            "path": self.path,
            "ok": not uncited,
            "uncited": uncited,
            "sources": sources,
        })
