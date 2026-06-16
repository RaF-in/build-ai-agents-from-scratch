"""Pluggable search providers.

The whole point of this module: callers NEVER name a concrete search backend.
They ask :func:`get_search_provider` for "the provider" and get whatever
``SEARCH_PROVIDER`` (env) selects — DuckDuckGo by default (free, no key, no
signup), Tavily when ``SEARCH_PROVIDER=tavily`` + ``TAVILY_API_KEY`` is set, and
so on. Adding a backend is: write a class with one ``search`` coroutine and call
:func:`register_provider`. Zero edits anywhere else.

Contract every provider honors:
  * returns a list of :class:`SearchResult`, normalized to {title, url, snippet,
    fetched_at}, so callers are backend-agnostic;
  * raises on hard failure (missing key, network error) — the SearchTool turns
    that into a clean tool error rather than crashing the run;
  * imports its SDK LAZILY inside ``search``/``__init__`` so an optional backend's
    missing dependency only bites when that backend is actually selected.
"""
from __future__ import annotations

import asyncio
import os
from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from typing import Callable, Protocol, runtime_checkable


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


@dataclass(frozen=True)
class SearchResult:
    """One normalized hit. Every provider maps its raw payload to this shape so
    nothing downstream depends on a provider's field names."""
    title: str
    url: str
    snippet: str
    fetched_at: str = field(default_factory=_now_iso)

    def as_dict(self) -> dict:
        return asdict(self)


@runtime_checkable
class SearchProvider(Protocol):
    """The entire surface a backend must implement."""
    name: str

    async def search(self, query: str, *, max_results: int = 5) -> list[SearchResult]:
        ...


# ---- registry --------------------------------------------------------------
# name -> zero-arg factory. A factory (not an instance) so construction (reading
# API keys, building clients) is deferred until a provider is actually chosen.
_PROVIDERS: dict[str, Callable[[], SearchProvider]] = {}

DEFAULT_PROVIDER = "ddgs"   # free, no key, no signup — the safe zero-config default


def register_provider(name: str, factory: Callable[[], SearchProvider]) -> None:
    _PROVIDERS[name.lower()] = factory


def available_providers() -> list[str]:
    return sorted(_PROVIDERS)


def get_search_provider(name: str | None = None) -> SearchProvider:
    """Resolve the active provider. Precedence: explicit ``name`` arg >
    ``SEARCH_PROVIDER`` env > :data:`DEFAULT_PROVIDER`. Swapping backends is a
    one-line env change; no code edits anywhere else."""
    key = (name or os.getenv("SEARCH_PROVIDER") or DEFAULT_PROVIDER).strip().lower()
    factory = _PROVIDERS.get(key)
    if factory is None:
        raise ValueError(
            f"Unknown search provider {key!r}. "
            f"Set SEARCH_PROVIDER to one of: {', '.join(available_providers())}."
        )
    return factory()


# ---- built-in providers ----------------------------------------------------

class DDGSProvider:
    """DuckDuckGo via the ``ddgs`` package. Free, no API key, no signup — the
    default so the pipeline runs at zero cost out of the box. ``ddgs`` is a sync
    library, so the blocking call is pushed to a worker thread."""
    name = "ddgs"

    async def search(self, query: str, *, max_results: int = 5) -> list[SearchResult]:
        def _run() -> list[dict]:
            from ddgs import DDGS  # lazy: only needed when this provider is selected
            with DDGS() as ddgs:
                return ddgs.text(query, max_results=max_results)

        raw = await asyncio.to_thread(_run)
        return [
            SearchResult(
                title=r.get("title", ""),
                url=r.get("href") or r.get("url", ""),
                snippet=r.get("body") or r.get("snippet", ""),
            )
            for r in (raw or [])
        ]


class TavilyProvider:
    """Tavily search API — purpose-built for agents, returns ranked clean content.
    Free tier (~1,000 credits/month, no card). Needs ``TAVILY_API_KEY``. Uses the
    already-present ``httpx`` (litellm dependency), so no new package."""
    name = "tavily"

    def __init__(self) -> None:
        self.api_key = os.getenv("TAVILY_API_KEY")

    async def search(self, query: str, *, max_results: int = 5) -> list[SearchResult]:
        if not self.api_key:
            raise RuntimeError(
                "SEARCH_PROVIDER=tavily but TAVILY_API_KEY is not set. "
                "Get a free key at https://tavily.com or use SEARCH_PROVIDER=ddgs."
            )
        import httpx  # lazy
        payload = {
            "api_key": self.api_key,
            "query": query,
            "max_results": max_results,
            "search_depth": "basic",
        }
        async with httpx.AsyncClient(timeout=20.0) as client:
            resp = await client.post("https://api.tavily.com/search", json=payload)
            resp.raise_for_status()
            data = resp.json()
        return [
            SearchResult(
                title=r.get("title", ""),
                url=r.get("url", ""),
                snippet=r.get("content", ""),
            )
            for r in data.get("results", [])
        ]


register_provider("ddgs", DDGSProvider)
register_provider("tavily", TavilyProvider)
