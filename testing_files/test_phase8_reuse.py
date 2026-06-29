"""Standalone checks for Phase 8 — prove reuse (a second capability).
Run: `uv run python testing_files/test_phase8_reuse.py`

The acceptance test for "future capabilities reuse this fully": dropping
capabilities/article_writer/ adds a working capability with ZERO edits to engine.py
or the kernel. We assert that structurally and behaviourally:

  8.1  the pack is discovered and its entry tool surfaces to the main agent (and is
       hidden from child tools); the pack contains only config/data — no engine code.
  8.2  WRITING_CONFIG is complete and reuses the GENERIC layer: same engine, web
       delegate tool, and the Phase 6 citation/word tools; prompt slots are filled;
       no shell tool is reachable.
  8.3  the Phase 5 calibration machinery is capability-blind — load_config and the
       instruction-fence reader work on article_writer with no special-casing.
  8.4  a faked end-to-end runs through the SAME run_pipeline and returns the Phase 6
       short contract, and a per-run observability record (Phase 7) is written.

No network: acompletion is faked; calibration/telemetry go to a tmp dir.
"""
import asyncio
import json
import os
import re
import sys
import tempfile

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

_TMP = tempfile.mkdtemp(prefix="p8_")
os.environ["AGENT_CALIBRATION_DB"] = os.path.join(_TMP, "verdicts.db")

import agent as agentmod
from tool_base import AgentContext
import capabilities.shared.engine as engine_mod
from capabilities.shared.registry import capability_registry
from capabilities.shared import calibration as calib
from capabilities.article_writer.entry import WriteArticleTool
from capabilities.article_writer.config import WRITING_CONFIG
from capabilities.article_writer.criteria import WRITING_CRITERIA


def NS(**kw):
    o = type("NS", (), {})()
    for k, v in kw.items():
        setattr(o, k, v)
    return o


# ---------------- 8.1 discovery + pure-config pack ----------------
def test_discovery_and_pure_config():
    capability_registry.discover()
    assert "article_writer" in capability_registry.names()
    assert WriteArticleTool in capability_registry.entry_tools()

    import agent_tools
    from subagent_tools import child_tools
    assert WriteArticleTool in agent_tools.TOOLS           # surfaced to the main agent
    assert WriteArticleTool not in child_tools(0, 2)       # hidden from children

    # The pack is PURE config/data — no engine/kernel/runtime modules live in it.
    pkg_dir = os.path.dirname(
        sys.modules["capabilities.article_writer.config"].__file__)
    allowed = {"__init__.py", "entry.py", "config.py", "criteria.py",
               "prompts", "eval_examples", "calibration_set", "__pycache__"}
    assert set(os.listdir(pkg_dir)) <= allowed, set(os.listdir(pkg_dir))
    print("8.1 article_writer discovered, surfaced, child-hidden, pure config: OK")


# ---------------- 8.2 config completeness + generic reuse ----------------
def test_config_complete_and_reuses_generic():
    assert WRITING_CONFIG.criteria is WRITING_CRITERIA and len(WRITING_CRITERIA) == 4
    sysp = WRITING_CONFIG.evaluator.system_prompt
    assert "{{CRITERIA}}" not in sysp and "{{EVAL_EXAMPLES}}" not in sysp
    for c in WRITING_CRITERIA:
        assert c.name in sysp                              # criteria injected
    assert "generic-filler-fail" in sysp                   # few-shot example injected

    tool_names = {t.tool_name() for t in WRITING_CONFIG.generator.tools}
    assert {"CheckCitationsTool", "CountWordsTool", "DelegateWebSearchTool"} <= tool_names
    assert "BashTool" not in tool_names                    # no shell in the writer either
    print("8.2 WRITING_CONFIG complete; reuses engine/web/Phase-6 tools; no shell: OK")


# ---------------- 8.3 Phase 5 machinery is capability-blind ----------------
def test_calibration_is_capability_blind():
    cfg = calib.load_config("article_writer")              # resolves by convention
    assert cfg is WRITING_CONFIG
    block = calib.read_instructions(cfg)                   # fenced region in evaluator.md
    assert "Scoring guidance" in block
    cases = calib.load_calibration_set(cfg.calibration_set_dir)
    assert {c.label for c in cases} == {"filler-fail", "crafted-pass"}
    print("8.3 calibration load_config + fences + regression set work unchanged: OK")


# ---------------- 8.4 faked end-to-end through the shared engine ----------------
_gen_step = {"n": 0}


async def fake_acompletion(model, messages, tools=None, **kw):
    sysp = messages[0]["content"] if messages and messages[0]["role"] == "system" else ""
    if "PLANNER for an article" in sysp:
        # 2 sections => verify="auto" treats it as trivial => evaluator skipped
        return NS(choices=[NS(message=NS(content="1. Hook\n2. Payoff", tool_calls=None))])
    if "GENERATOR for an article" in sysp:
        ws = re.search(r"Your run workspace is ([^\s.]+)", sysp).group(1)
        if _gen_step["n"] == 0:
            _gen_step["n"] = 1
            article = ("# The Quiet Cost of X\nAdoption looks free at first "
                       "[eng.example/x].\n\n## Sources\n- eng.example/x\n")
            tc = NS(id="c1", function=NS(name="WriteTool",
                    arguments=json.dumps({"path": f"{ws}/report.md", "content": article})))
            return NS(choices=[NS(message=NS(content="", tool_calls=[tc]))])
        return NS(choices=[NS(message=NS(content="article written", tool_calls=None))])
    return NS(choices=[NS(message=NS(content="ok", tool_calls=None))])


async def test_end_to_end_short_contract():
    _gen_step["n"] = 0
    agentmod.acompletion = fake_acompletion
    # the entry uses the SAME engine function deep_research does — no writer engine.
    from capabilities.deep_research.entry import DeepResearchTool  # noqa: F401
    assert engine_mod.run_pipeline.__module__ == "capabilities.shared.engine"

    res = (await WriteArticleTool(topic="The cost of X", audience="engineers")
           .execute(AgentContext())).result
    assert set(res) == {"summary", "report_path", "sources"}, res
    assert res["summary"] == "article written"
    assert res["report_path"].endswith("report.md") and os.path.isfile(res["report_path"])
    assert res["sources"] == ["eng.example/x"]

    from capabilities.shared.run_log import runs_dir
    logs = [json.load(open(os.path.join(runs_dir(), f), encoding="utf-8"))
            for f in os.listdir(runs_dir()) if f.endswith(".json")]
    assert any(r["capability"] == "article_writer" for r in logs), "no run log for writer"
    print("8.4 end-to-end via shared run_pipeline; short contract + run log: OK")


async def main():
    test_discovery_and_pure_config()
    test_config_complete_and_reuses_generic()
    test_calibration_is_capability_blind()
    await test_end_to_end_short_contract()
    print("\nALL PHASE 8 CHECKS PASSED")


if __name__ == "__main__":
    asyncio.run(main())
