"""Standalone checks for Phase 10 — testing & rollout (the genuinely-new gaps).
Run: `uv run python testing_files/test_phase10_rollout.py`

The per-phase suites already cover the unit/security matrix (depth budget, RunBudget
exhaustion, survivor collection, todos, citation post-check, SSRF, Verdict.passed,
injection-no-op, repeated-call abort, 429 forwarding). This suite adds what was
missing:

  10a  the rollout feature flag (AGENT_DISABLED_CAPABILITIES) hides a pack from
       discovery while leaving others surfaced.
  10d  a FULL mocked-search integration run: plan -> generate (delegating to real
       worker subagents that call SearchTool/FetchTool against mocked I/O) ->
       skeptical eval -> passing verdict, on a fixed brief.
  10e  the SSRF guard catches a public-looking host that RESOLVES to a private IP
       (DNS-rebinding shape), not just literal private URLs.

No network: model calls are faked; the search provider and fetch are mocked.
"""
import asyncio
import json
import os
import re
import sys
import tempfile

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

_TMP = tempfile.mkdtemp(prefix="p10_")
os.environ["AGENT_CALIBRATION_DB"] = os.path.join(_TMP, "verdicts.db")

import agent as agentmod
from tool_base import AgentContext
import web.tools as webtools
import web.fetch as fetchmod
import web.providers as providers
from capabilities.shared.registry import capability_registry
from capabilities.shared.engine import run_pipeline
from capabilities.shared.verdict_store import default_store, reset_default_store
from capabilities.article_writer.entry import WriteArticleTool


def NS(**kw):
    o = type("NS", (), {})()
    for k, v in kw.items():
        setattr(o, k, v)
    return o


# ---------------- 10a feature flag ----------------
def test_feature_flag_gating():
    capability_registry.discover()
    assert "article_writer" in capability_registry.names()
    os.environ["AGENT_DISABLED_CAPABILITIES"] = "article_writer"
    try:
        capability_registry.discover()
        assert "article_writer" not in capability_registry.names()
        assert WriteArticleTool not in capability_registry.entry_tools()
        assert "deep_research" in capability_registry.names()   # others unaffected
    finally:
        del os.environ["AGENT_DISABLED_CAPABILITIES"]
        capability_registry.discover()                          # restore for other tests
    assert "article_writer" in capability_registry.names()
    print("10a rollout flag hides a pack from discovery, others stay surfaced: OK")


# ---------------- 10d full mocked-search integration ----------------
_SEARCH_CALLS = {"n": 0}
_FETCH_CALLS = {"n": 0}
_gen = {"n": 0}


class FakeProvider:
    name = "fake"

    async def search(self, query, *, max_results=5):
        _SEARCH_CALLS["n"] += 1
        return [providers.SearchResult(title="On X", url="https://ex.com/a", snippet="x is real")]


async def fake_fetch(url, **kw):
    _FETCH_CALLS["n"] += 1
    return f"Independent analysis: X is real and widely adopted. (from {url})"


async def integ_acompletion(model, messages, tools=None, **kw):
    sysp = messages[0]["content"] if messages and messages[0]["role"] == "system" else ""
    tool_contents = [str(m.get("content", "")) for m in messages if m.get("role") == "tool"]
    joined = " ".join(tool_contents)

    if "PLANNER for a deep-research" in sysp:
        return NS(choices=[NS(message=NS(content="1. is X real\n2. who adopts X\n3. risks of X",
                                         tool_calls=None))])

    if "WORKER investigating ONE focused question" in sysp:
        if not tool_contents:
            tc = NS(id="s", function=NS(name="SearchTool", arguments=json.dumps({"query": "X"})))
            return NS(choices=[NS(message=NS(content="", tool_calls=[tc]))])
        if "UNTRUSTED" not in joined:                       # searched, not yet fetched
            url = "https://ex.com/a"
            for c in tool_contents:
                try:
                    d = json.loads(c)
                    if isinstance(d, dict) and d.get("results"):
                        url = d["results"][0]["url"]; break
                except Exception:
                    pass
            tc = NS(id="f", function=NS(name="FetchTool", arguments=json.dumps({"url": url})))
            return NS(choices=[NS(message=NS(content="", tool_calls=[tc]))])
        return NS(choices=[NS(message=NS(content="X is real and adopted [https://ex.com/a].",
                                         tool_calls=None))])

    if "GENERATOR for a deep-research" in sysp:
        if _gen["n"] == 0:
            _gen["n"] = 1
            tc = NS(id="d", function=NS(name="DelegateWebSearchTool",
                    arguments=json.dumps({"sub_questions": ["is X real", "who adopts X"]})))
            return NS(choices=[NS(message=NS(content="", tool_calls=[tc]))])
        if _gen["n"] == 1:
            _gen["n"] = 2
            ws = re.search(r"workspace is (/[^\s]+)", sysp).group(1).rstrip(".")
            report = ("# Report: Is X real?\nX is real and widely adopted "
                      "[https://ex.com/a].\n\n## Sources\n- https://ex.com/a\n")
            tc = NS(id="w", function=NS(name="WriteTool",
                    arguments=json.dumps({"path": f"{ws}/report.md", "content": report})))
            return NS(choices=[NS(message=NS(content="", tool_calls=[tc]))])
        return NS(choices=[NS(message=NS(content="report written", tool_calls=None))])

    if "SKEPTICAL QA reviewer" in sysp:
        if not tool_contents:
            ws = re.search(r"workspace is (/[^\s]+)", sysp).group(1).rstrip(".")
            tc = NS(id="r", function=NS(name="ReadTool",
                    arguments=json.dumps({"path": f"{ws}/report.md"})))
            return NS(choices=[NS(message=NS(content="", tool_calls=[tc]))])
        if "recorded" not in joined:
            scores = {"coverage": 0.8, "citation_quality": 0.85,
                      "source_quality": 0.7, "synthesis": 0.7}
            tc = NS(id="v", function=NS(name="SubmitVerdict", arguments=json.dumps(
                {"scores": scores, "rationale": {k: "ok" for k in scores}, "feedback": "solid"})))
            return NS(choices=[NS(message=NS(content="", tool_calls=[tc]))])
        return NS(choices=[NS(message=NS(content="done", tool_calls=None))])

    return NS(choices=[NS(message=NS(content="ok", tool_calls=None))])


async def test_mocked_search_integration():
    from capabilities.deep_research.config import RESEARCH_CONFIG

    _SEARCH_CALLS["n"] = _FETCH_CALLS["n"] = _gen["n"] = 0
    reset_default_store()
    agentmod.acompletion = integ_acompletion
    orig_provider = webtools.get_search_provider
    orig_fetch = webtools.fetch_url
    webtools.get_search_provider = lambda *a, **k: FakeProvider()
    webtools.fetch_url = fake_fetch
    try:
        ctx = AgentContext()
        report = await run_pipeline("Is X real?", config=RESEARCH_CONFIG, context=ctx)
    finally:
        webtools.get_search_provider = orig_provider
        webtools.fetch_url = orig_fetch

    # the REAL web path executed against mocked I/O
    assert _SEARCH_CALLS["n"] >= 1 and _FETCH_CALLS["n"] >= 1, (_SEARCH_CALLS, _FETCH_CALLS)
    # synthesis cited the fetched source
    assert "https://ex.com/a" in report and "## Sources" in report
    # the skeptical evaluator actually ran and passed (verdict persisted for this run)
    run_id = os.path.basename(ctx.workspace_dir)
    verdicts = [v for v in default_store().recent("deep_research") if v["run_id"] == run_id]
    assert verdicts and verdicts[0]["passed"] is True, verdicts
    print(f"10d full plan->gen(workers: {_SEARCH_CALLS['n']} search/"
          f"{_FETCH_CALLS['n']} fetch)->eval PASS on mocked I/O: OK")


# ---------------- 10e DNS-rebinding SSRF ----------------
def test_dns_rebind_ssrf():
    orig = fetchmod.socket.getaddrinfo
    # a public-looking host that RESOLVES to a private address
    fetchmod.socket.getaddrinfo = lambda host, port, **kw: [(2, 1, 6, "", ("10.0.0.5", 0))]
    try:
        raised = False
        try:
            fetchmod.assert_safe_url("http://totally-public.example/")
        except fetchmod.SSRFError:
            raised = True
        assert raised, "guard let a host resolving to a private IP through"
    finally:
        fetchmod.socket.getaddrinfo = orig
    print("10e SSRF guard blocks a public host resolving to a private IP: OK")


async def main():
    test_feature_flag_gating()
    await test_mocked_search_integration()
    test_dns_rebind_ssrf()
    print("\nALL PHASE 10 CHECKS PASSED")


if __name__ == "__main__":
    asyncio.run(main())
