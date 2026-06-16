"""Generic web reach — capability-agnostic tools usable by ANY agent or capability.

This package deliberately lives at the repo root (next to ``agent_tools.py``,
``subagent_tools.py``) rather than inside ``capabilities/deep_research/``, because
web search and fetch are not research-specific: research, writing, coding, or even
the main agent can reuse them. A capability opts in by listing these tools in its
config; nothing here is added to the main agent's default tool set automatically.

Contents:
  * providers.py — pluggable search backends behind one interface (DuckDuckGo
    default/free, Tavily, …). Swap with the SEARCH_PROVIDER env var.
  * fetch.py     — SSRF-guarded URL fetch + HTML→text + untrusted-content wrap.
  * tools.py     — SearchTool, FetchTool, and DelegateWebSearchTool (parallel
    web-research workers, bounded by the per-run semaphore).
"""
