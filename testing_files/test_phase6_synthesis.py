"""Standalone checks for Phase 6 — synthesis & citations.
Run: `uv run python testing_files/test_phase6_synthesis.py`

Covers:
  6.1  CountWordsTool counts a workspace file natively (no shell `wc`).
  6.2  CheckCitationsTool flags prose claims with no inline citation, passes a fully
       cited doc, parses + dedups the Sources list; extract_sources helper.
  6.3  the deep_research generator prompt carries the mandatory citation post-check
       and the two tools are wired into the generator config.
  6.4  DeepResearchTool returns the Phase 6 short contract — {summary, report_path,
       sources} — not the full report, with the report left on disk.

No network: the entry test fakes run_pipeline (writes report.md + summary.md into
the run workspace) so only the output-contract shaping is exercised.
"""
import asyncio
import os
import sys
import tempfile

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import capabilities.shared.engine as engine_mod
from tool_base import AgentContext
from capabilities.shared import text_tools as tt
from capabilities.shared.text_tools import CountWordsTool, CheckCitationsTool
from capabilities.deep_research.entry import DeepResearchTool


# ---------------- 6.1 word count ----------------
async def test_count_words():
    ws = tempfile.mkdtemp(prefix="p6_wc_")
    with open(os.path.join(ws, "report.md"), "w") as f:
        f.write("# Title\nThe quick brown fox jumps.\n")        # 5 words in the body
    ctx = AgentContext(workspace_dir=ws)
    res = await CountWordsTool(path="report.md").execute(ctx)
    assert not res.error and res.result["words"] == 6, res.result  # Title + 5
    # absolute path also works; missing file is a clean error, not a crash
    res2 = await CountWordsTool(path=os.path.join(ws, "nope.md")).execute(ctx)
    assert res2.error
    print("6.1 CountWordsTool counts natively + errors cleanly on a missing file: OK")


# ---------------- 6.2 citation linter ----------------
_UNCITED = """# Findings
GDP grew by 4 percent last year across the region.
Adoption reached 12 million users in 2024 [report.example/adoption].

## Sources
- report.example/adoption
- report.example/adoption
"""

_CITED = """# Findings
GDP grew by 4 percent last year [stats.example/gdp-2024].
Adoption reached 12 million users in 2024 [report.example/adoption].

## Notes
See more.

## Sources
- stats.example/gdp-2024
- report.example/adoption
"""


async def test_check_citations():
    ws = tempfile.mkdtemp(prefix="p6_cite_")
    bad = os.path.join(ws, "bad.md")
    good = os.path.join(ws, "good.md")
    with open(bad, "w") as f:
        f.write(_UNCITED)
    with open(good, "w") as f:
        f.write(_CITED)
    ctx = AgentContext(workspace_dir=ws)

    r_bad = (await CheckCitationsTool(path="bad.md").execute(ctx)).result
    assert r_bad["ok"] is False
    assert any("GDP grew" in c for c in r_bad["uncited"]), r_bad["uncited"]
    assert all("Adoption reached" not in c for c in r_bad["uncited"])   # that line is cited
    assert r_bad["sources"] == ["report.example/adoption"]             # deduped

    r_good = (await CheckCitationsTool(path="good.md").execute(ctx)).result
    assert r_good["ok"] is True and r_good["uncited"] == [], r_good
    assert r_good["sources"] == ["stats.example/gdp-2024", "report.example/adoption"]

    # helper: a heading + short label are never flagged; sources extraction matches
    assert tt.extract_sources(_CITED) == r_good["sources"]
    assert tt.find_uncited_claims("# Heading\nshort label\n") == []
    print("6.2 CheckCitationsTool flags uncited prose, passes cited, dedups sources: OK")


# ---------------- 6.3 generator wiring ----------------
def test_generator_postcheck_wired():
    from capabilities.deep_research.config import RESEARCH_CONFIG
    sysp = RESEARCH_CONFIG.generator.system_prompt
    assert "CheckCitationsTool" in sysp and "MANDATORY" in sysp
    assert "CountWordsTool" in sysp and "never shell out" in sysp
    tool_names = {t.tool_name() for t in RESEARCH_CONFIG.generator.tools}
    assert {"CheckCitationsTool", "CountWordsTool"} <= tool_names, tool_names
    print("6.3 generator prompt carries the citation post-check; tools wired: OK")


# ---------------- 6.4 short output contract ----------------
async def test_entry_short_contract():
    async def fake_pipeline(brief, *, config, context):
        ws = tempfile.mkdtemp(prefix="p6_run_")
        context.workspace_dir = ws                       # what run_pipeline would set
        report = ("# Report\nUsers grew to 5 million in 2024 "
                  "[stats.example/u].\n\n## Sources\n- stats.example/u\n")
        with open(os.path.join(ws, "report.md"), "w") as f:
            f.write(report)
        with open(os.path.join(ws, "summary.md"), "w") as f:
            f.write("Covered adoption and growth; 1 source.")
        return report

    orig = engine_mod.run_pipeline
    engine_mod.run_pipeline = fake_pipeline
    try:
        res = (await DeepResearchTool(brief="Research X").execute(AgentContext())).result
    finally:
        engine_mod.run_pipeline = orig

    assert set(res) == {"summary", "report_path", "sources"}, res
    assert res["summary"] == "Covered adoption and growth; 1 source."   # from summary.md
    assert "# Report" not in res["summary"]                              # NOT the full report
    assert res["report_path"].endswith("report.md") and os.path.isfile(res["report_path"])
    assert res["sources"] == ["stats.example/u"]
    print("6.4 DeepResearchTool returns short {summary, report_path, sources}: OK")


async def main():
    await test_count_words()
    await test_check_citations()
    test_generator_postcheck_wired()
    await test_entry_short_contract()
    print("\nALL PHASE 6 CHECKS PASSED")


if __name__ == "__main__":
    asyncio.run(main())
