"""Standalone checks for Phase 2 — the config-plugin contract.
Run: `uv run python testing_files/test_phase2_registry.py`

Covers:
  - CapabilitySpec.validate() rejects malformed packs.
  - CapabilityRegistry.discover() finds deep_research, skips shared, and is
    resilient (a bad spec is skipped, not fatal).
  - agent_tools.TOOLS gains DeepResearchTool via discovery (zero kernel edit).
  - RESEARCH_CONFIG is a complete CapabilityConfig (the contract → data).
  - DeepResearchTool.execute() is a thin handle that routes to run_pipeline on an
    ISOLATED run context (the caller's context is not polluted).
  - Full selection→execute→plan→gen path returns the report (eval off).
"""
import asyncio
import json
import os
import re
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import agent as agentmod
import agent_tools
import capabilities.shared.engine as engine_mod
from tool_base import AgentContext, AgentTool
from capabilities.shared.registry import CapabilityRegistry, CapabilitySpec, capability_registry
from capabilities.deep_research.entry import DeepResearchTool
from capabilities.deep_research.config import RESEARCH_CONFIG


def NS(**kw):
    o = type("NS", (), {})()
    for k, v in kw.items():
        setattr(o, k, v)
    return o


def test_spec_validation():
    class Dummy(AgentTool):
        async def execute(self, context):
            return self.tool_result(result={})
    CapabilitySpec("x", "s", [Dummy]).validate()  # ok
    for bad in (
        CapabilitySpec("", "s", [Dummy]),
        CapabilitySpec("x", "", [Dummy]),
        CapabilitySpec("x", "s", []),
        CapabilitySpec("x", "s", [object]),  # not an AgentTool
    ):
        try:
            bad.validate()
            raise AssertionError(f"expected validation failure for {bad}")
        except (ValueError, TypeError):
            pass
    print("contract: CapabilitySpec.validate rejects malformed packs: OK")


def test_discovery():
    reg = CapabilityRegistry()
    reg.discover()
    assert "deep_research" in reg.names(), reg.names()
    assert "shared" not in reg.names()
    assert DeepResearchTool in reg.entry_tools()
    assert "deep_research:" in reg.catalog().replace(" ", "")
    # idempotent: re-discovery doesn't duplicate
    reg.discover()
    assert reg.entry_tools().count(DeepResearchTool) == 1
    print(f"discovery: found {reg.names()}, entry tools surfaced, idempotent: OK")


def test_tools_merged():
    # agent_tools ran discovery at import; the entry tool is in the main tool set.
    names = [t.__name__ for t in agent_tools.TOOLS]
    assert "DeepResearchTool" in names, names
    # but NOT in a locked child tool set (workers never recurse into research)
    from subagent_tools import child_tools
    assert DeepResearchTool not in child_tools(0, 2)
    print("surfacing: DeepResearchTool in main TOOLS, absent from child tools: OK")


def test_config_is_complete():
    c = RESEARCH_CONFIG
    assert c.name == "deep_research"
    assert c.planner and c.generator and c.evaluator
    assert c.generator.policy_factory is not None and c.generator.fresh_todo_list
    assert len(c.criteria) == 4
    # criteria injected into BOTH generator and evaluator prompts
    assert "coverage" in c.generator.system_prompt
    assert "coverage" in c.evaluator.system_prompt
    assert c.verify == "auto"   # Phase 4 enabled the skeptical evaluator gate
    print("config: RESEARCH_CONFIG complete; criteria injected into gen+eval: OK")


async def test_execute_routes_isolated():
    # Replace the engine the entry tool calls; assert it routes there on an
    # isolated context and returns the engine's output verbatim.
    seen = {}

    async def fake_pipeline(brief, *, config, context):
        seen["brief"] = brief
        seen["config"] = config
        seen["ctx"] = context
        return "ROUTED-REPORT"

    orig = engine_mod.run_pipeline
    engine_mod.run_pipeline = fake_pipeline
    try:
        main_ctx = AgentContext(chat_id=42)
        res = await DeepResearchTool(brief="Research X").execute(main_ctx)
        # Phase 6 contract: a short summary, not the full report. The fake pipeline
        # wrote no report.md to the (unset) workspace, so the summary falls back to the
        # returned text and report_path is None.
        assert res.result["summary"] == "ROUTED-REPORT"
        assert res.result["report_path"] is None
        assert seen["brief"] == "Research X" and seen["config"] is RESEARCH_CONFIG
        # isolation: a NEW depth-0 ctx was used; the caller's is untouched
        assert seen["ctx"] is not main_ctx and seen["ctx"].depth == 0
        assert seen["ctx"].chat_id == 42         # services copied
        assert main_ctx.run_budget is None and main_ctx.workspace_dir is None
    finally:
        engine_mod.run_pipeline = orig
    print("dispatch: execute routes to run_pipeline on an isolated context: OK")


_gen_step = {"n": 0}


async def fake_acompletion(model, messages, tools=None, **kw):
    sys_prompt = messages[0]["content"] if messages and messages[0]["role"] == "system" else ""
    if "You are the PLANNER" in sys_prompt:
        msg = NS(content="1. what is X\n2. why X matters", tool_calls=None)
    elif "You are the GENERATOR" in sys_prompt:
        ws = re.search(r"workspace is (/[^\s]+)", sys_prompt).group(1).rstrip(".")
        if _gen_step["n"] == 0:
            _gen_step["n"] = 1
            tc = NS(id="c1", function=NS(name="WriteTool", arguments=json.dumps(
                {"path": f"{ws}/report.md", "content": "# Report on X\nFindings [src]."})))
            msg = NS(content="", tool_calls=[tc])
        else:
            msg = NS(content="report.md written", tool_calls=None)
    else:
        msg = NS(content="ok", tool_calls=None)
    return NS(choices=[NS(message=msg)])


async def test_end_to_end():
    _gen_step["n"] = 0
    agentmod.acompletion = fake_acompletion
    main_ctx = AgentContext()
    res = await DeepResearchTool(brief="Research X").execute(main_ctx)
    # Phase 6 contract: short summary (the generator's final reply) + on-disk path.
    assert res.result["summary"] == "report.md written", repr(res.result)
    assert res.result["report_path"].endswith("report.md")
    assert os.path.isfile(res.result["report_path"])
    assert main_ctx.run_budget is None  # caller still pristine after a real run
    print("end-to-end: select→execute→plan→gen→report (eval off): OK")


async def main():
    test_spec_validation()
    test_discovery()
    test_tools_merged()
    test_config_is_complete()
    await test_execute_routes_isolated()
    await test_end_to_end()
    print("\nALL PHASE 2 CHECKS PASSED")


if __name__ == "__main__":
    asyncio.run(main())
