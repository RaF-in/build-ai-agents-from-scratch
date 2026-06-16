"""Standalone checks for Phase 3 (run: `uv run python testing_files/test_phase3_reach.py`).

Covers:
  3.1  pluggable search providers — registry/factory, env-driven selection,
       SearchResult normalization, unknown-provider error. SearchTool wrapper.
  3.2  SSRF guard — scheme reject, loopback/link-local reject, DNS→private reject,
       public IP allowed; html_to_text strips script/style; untrusted wrap.
       FetchTool wrapper (success + refusal).
  3.3  DelegateSearchTool — requires a per-run semaphore; fans out one worker per
       sub-question and collects survivors when one worker fails.

No network and no LLM: providers/fetch/run_subagent are faked.
"""
import asyncio
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import subagent_tools
from tool_base import AgentContext, ToolResult
from web import providers, fetch, tools
from web.providers import (
    SearchResult, get_search_provider, register_provider, available_providers,
)
from web.fetch import (
    assert_safe_url, SSRFError, html_to_text, wrap_untrusted,
)
from web.tools import SearchTool, FetchTool, DelegateWebSearchTool


# ---------------- 3.1 providers ----------------
class _FakeProvider:
    name = "fake"
    async def search(self, query, *, max_results=5):
        return [SearchResult(title=f"r{i}", url=f"https://ex/{i}", snippet=f"about {query}")
                for i in range(max_results)]


def test_provider_registry():
    register_provider("fake", _FakeProvider)
    assert "fake" in available_providers() and "ddgs" in available_providers()
    # explicit name arg wins
    assert get_search_provider("fake").name == "fake"
    # env-driven selection (the one-line swap)
    os.environ["SEARCH_PROVIDER"] = "fake"
    try:
        assert get_search_provider().name == "fake"
    finally:
        os.environ.pop("SEARCH_PROVIDER", None)
    # default is the free, no-key backend
    assert get_search_provider().name == providers.DEFAULT_PROVIDER == "ddgs"
    # unknown backend fails loudly
    try:
        get_search_provider("nope")
        assert False, "expected ValueError"
    except ValueError as e:
        assert "Unknown search provider" in str(e)
    # SearchResult normalizes to the {title,url,snippet,fetched_at} contract
    d = SearchResult("t", "u", "s").as_dict()
    assert set(d) == {"title", "url", "snippet", "fetched_at"}
    print("3.1 provider registry + env selection + normalization: OK")


async def test_search_tool():
    register_provider("fake", _FakeProvider)
    os.environ["SEARCH_PROVIDER"] = "fake"
    try:
        res = await SearchTool(query="quantum batteries", max_results=3).execute(AgentContext())
        assert not res.error
        assert res.result["provider"] == "fake" and len(res.result["results"]) == 3
        assert res.result["results"][0]["url"] == "https://ex/0"
        # empty query -> clean tool error
        empty = await SearchTool(query="  ").execute(AgentContext())
        assert empty.error and "Empty search query" in empty.result["error"]
    finally:
        os.environ.pop("SEARCH_PROVIDER", None)
    print("3.1 SearchTool (results + empty-query error): OK")


# ---------------- 3.2 SSRF + extraction + fetch ----------------
def test_ssrf_guard():
    # scheme reject
    for bad in ("ftp://example.com", "file:///etc/passwd", "gopher://x"):
        try:
            assert_safe_url(bad); assert False, bad
        except SSRFError:
            pass
    # loopback + link-local (cloud metadata) + DNS→private
    for blocked in ("http://127.0.0.1/", "http://169.254.169.254/latest/meta-data/",
                    "http://localhost:8080/", "http://[::1]/"):
        try:
            assert_safe_url(blocked); assert False, blocked
        except SSRFError:
            pass
    # a public IP literal is allowed (no network needed for a numeric host)
    assert_safe_url("http://93.184.216.34/")
    print("3.2 SSRF guard (scheme/loopback/link-local/DNS→private; public allowed): OK")


def test_extraction_and_wrap():
    html = "<html><head><style>.x{}</style></head><body><script>evil()</script>" \
           "<h1>Title</h1><p>Hello world</p></body></html>"
    text = html_to_text(html)
    assert "Title" in text and "Hello world" in text
    assert "evil" not in text and ".x{}" not in text   # script/style dropped
    wrapped = wrap_untrusted("payload", "https://ex/a")
    assert "UNTRUSTED" in wrapped and "<untrusted_content>" in wrapped and "payload" in wrapped
    print("3.2 html_to_text strips script/style + untrusted wrap: OK")


async def test_fetch_tool(monkeypatch_target=fetch):
    async def fake_fetch(url, **kw):
        return "clean page text"
    tools.fetch_url = fake_fetch  # patch the symbol the tool actually calls
    try:
        ok = await FetchTool(url="https://example.com/a").execute(AgentContext())
        assert not ok.error and "clean page text" in ok.result["content"]
        assert "UNTRUSTED" in ok.result["content"]
    finally:
        tools.fetch_url = fetch.fetch_url

    async def boom(url, **kw):
        raise SSRFError("refusing private/loopback address 127.0.0.1")
    tools.fetch_url = boom
    try:
        refused = await FetchTool(url="http://127.0.0.1/").execute(AgentContext())
        assert refused.error and "refused unsafe URL" in refused.result["error"]
    finally:
        tools.fetch_url = fetch.fetch_url
    print("3.2 FetchTool (success wraps content; SSRF refusal surfaces): OK")


# ---------------- 3.3 DelegateSearchTool fan-out ----------------
async def test_delegate_requires_semaphore():
    # no per-run semaphore => refuse loudly (not a silent global-cap fallback)
    res = await DelegateWebSearchTool(sub_questions=["q1"]).execute(AgentContext())
    assert res.error and "per-run semaphore" in res.result["error"]
    # empty list
    empty = await DelegateWebSearchTool(sub_questions=[]).execute(
        AgentContext(run_semaphore=asyncio.Semaphore(2)))
    assert empty.error and "No sub-questions" in empty.result["error"]
    print("3.3 DelegateWebSearchTool guards (semaphore required; empty list): OK")


async def test_delegate_fanout_survivors():
    calls = []
    expected_worker_tools = tools._web_worker_tools()

    async def fake_run_subagent(*, task, context, tools_override, sys_prompt, **kw):
        calls.append(task)
        # workers get the web search/fetch/file set (Phase 3.3), never bash
        assert tools_override == expected_worker_tools
        if "fail" in task:
            return ToolResult(name="DelegateWebSearchTool", error=True,
                              result={"error": "worker blew up"})
        return ToolResult(name="DelegateWebSearchTool",
                          result={"content": f"findings for {task}"})

    # delegate_to_workers calls run_subagent in the subagent_tools namespace.
    orig = subagent_tools.run_subagent
    subagent_tools.run_subagent = fake_run_subagent
    try:
        ctx = AgentContext(run_semaphore=asyncio.Semaphore(5), workspace_dir="/tmp/run_x")
        res = await DelegateWebSearchTool(
            sub_questions=["good one", "please fail", "good two"]).execute(ctx)
        assert not res.error
        s = res.result["summaries"]
        assert len(s) == 3 and [x["sub_question"] for x in s] == ["good one", "please fail", "good two"]
        assert s[0]["ok"] and s[0]["summary"] == "findings for good one"
        assert not s[1]["ok"] and s[1]["error"] == "worker blew up"   # survivor collection
        assert s[2]["ok"]
        assert set(calls) == {"good one", "please fail", "good two"}
    finally:
        subagent_tools.run_subagent = orig
    print("3.3 DelegateWebSearchTool fan-out + survivor collection + worker scoping: OK")


async def main():
    test_provider_registry()
    await test_search_tool()
    test_ssrf_guard()
    test_extraction_and_wrap()
    await test_fetch_tool()
    await test_delegate_requires_semaphore()
    await test_delegate_fanout_survivors()
    print("\nALL PHASE 3 CHECKS PASSED")


if __name__ == "__main__":
    asyncio.run(main())
