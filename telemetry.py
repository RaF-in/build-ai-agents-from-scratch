"""Logfire / OpenTelemetry telemetry layer (Phase 0.3).

ONE module owns all tracing. Three guarantees shape it:

  * **Token-gated, no-op safe.** With no ``LOGFIRE_TOKEN`` everything here is a
    cheap no-op: ``span()`` yields a ``_NullSpan`` whose ``set_attribute`` does
    nothing, the metric recorders short-circuit, and ``configure_telemetry``
    returns False. Tests and offline runs are byte-for-byte unchanged.
  * **Configure once.** ``configure_telemetry`` is idempotent and must be called
    at startup (never at import time, or the test suite would light up).
  * **Centralized, namespaced keys.** Span attributes come from each domain
    object's ``telemetry_attributes()`` (the source of truth); this module only
    composes and emits them.

litellm's native logfire callback auto-instruments every LLM call, so we never
hand-instrument the HTTP request (the token metric below merely reads the usage
litellm already returns).
"""
from __future__ import annotations

import contextlib
import json
import os
from typing import Any

import logfire

# Module state: flipped on by configure_telemetry(), read by every helper.
ENABLED = False
CAPTURE_CONTENT = False
_M: dict[str, Any] = {}


class _NullSpan:
    """Stand-in span used when telemetry is disabled, so call sites can use
    ``set_attribute`` / ``set_attributes`` unconditionally — this is what makes
    "never break the app" structural rather than a discipline."""

    def set_attribute(self, *args: Any, **kwargs: Any) -> None:
        pass

    def set_attributes(self, *args: Any, **kwargs: Any) -> None:
        pass


def configure_telemetry() -> bool:
    """Configure Logfire once. No-op (returns False) without a LOGFIRE_TOKEN.

    Idempotent: safe to call from every entry point. Wires litellm's logfire
    callback and declares the lightweight metric counters.
    """
    global ENABLED, CAPTURE_CONTENT
    if ENABLED or not os.getenv("LOGFIRE_TOKEN"):
        return ENABLED  # already configured, or no token => full no-op
    logfire.configure(
        service_name=os.getenv("OTEL_SERVICE_NAME", "ai-agent"),
        environment=os.getenv("AGENT_ENV", "dev"),
        console=False if os.getenv("TRACE_CONSOLE") == "0" else logfire.ConsoleOptions(verbose=False),
        scrubbing=logfire.ScrubbingOptions(
            extra_patterns=["api_key", "telegram", "tavily", "groq"]
        ),
    )
    # Auto-instrument every LLM call (turn / role / worker / compaction) without
    # touching llm_resilience.py.
    import litellm
    litellm.callbacks = ["logfire"]

    _M["tool_errors"] = logfire.metric_counter("tool.errors")
    _M["subagent_failures"] = logfire.metric_counter("subagent.failures")
    _M["llm_tokens"] = logfire.metric_counter("llm.tokens")

    CAPTURE_CONTENT = os.getenv("TRACE_CAPTURE_CONTENT") == "1"
    ENABLED = True
    return True


def span(name: str, **attributes: Any):
    """A telemetry span when enabled, else a context manager yielding _NullSpan."""
    if ENABLED:
        return logfire.span(name, **attributes)
    return contextlib.nullcontext(_NullSpan())


def payload(key: str, value: Any) -> dict[str, Any]:
    """Summaries by default (Phase 5): emit byte-count + 200-char preview unless
    TRACE_CAPTURE_CONTENT=1, in which case the full serialized value is captured."""
    s = value if isinstance(value, str) else json.dumps(value, default=str)
    if CAPTURE_CONTENT:
        return {key: s}
    return {f"{key}_bytes": len(s), f"{key}_preview": s[:200]}


def record_tool_error(name: str) -> None:
    if ENABLED:
        _M["tool_errors"].add(1, {"tool": name})


def record_subagent_failure(name: str, status: str) -> None:
    if ENABLED:
        _M["subagent_failures"].add(1, {"role": name, "status": status})


def record_tokens(usage: Any) -> None:
    """Token metric from the usage litellm already returned (no re-instrumented
    HTTP call — stays compliant with the "auto-instrument 3rd-party" rule)."""
    if not (ENABLED and usage):
        return
    _M["llm_tokens"].add(getattr(usage, "prompt_tokens", 0), {"kind": "input"})
    _M["llm_tokens"].add(getattr(usage, "completion_tokens", 0), {"kind": "output"})


def instrument_fastapi(app: Any) -> None:
    if ENABLED:
        logfire.instrument_fastapi(app)
