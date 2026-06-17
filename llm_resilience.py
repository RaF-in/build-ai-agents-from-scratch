"""Provider resilience (Phase 7.3): a thin wrapper over ``litellm.acompletion``.

A long capability run must not die on one rate limit. This adds two native litellm
features so it stays "config, not new infra":

  * ``num_retries`` — litellm retries transient errors (429 / 5xx / timeout) with
    its own exponential backoff before raising.
  * ``fallbacks`` — an ordered list of backup models litellm tries when the primary
    keeps failing after its retries.

The main agent and every spawned role/worker call the model through
``resilient_acompletion`` (imported as ``acompletion``), so the policy is applied
uniformly without the kernel learning any provider specifics. A genuine, persistent
failure still propagates — the existing per-spawn retry (subagent_tools) is the
outer net, and the run wraps up with partial results rather than crashing.
"""
from __future__ import annotations

import os

from litellm import acompletion as _acompletion

# Retries litellm performs per call on a transient error (with its own backoff).
# 0 disables; the error then propagates to the outer subagent-level retry.
LLM_NUM_RETRIES = int(os.getenv("LLM_NUM_RETRIES", "2"))


def _env_fallbacks() -> list[str]:
    return [m.strip() for m in os.getenv("LLM_FALLBACKS", "").split(",") if m.strip()]


# Ordered backup models, tried when the primary fails after its retries. EMPTY by
# default → pure retry on the primary, no cross-provider fallback. Fill with models
# you hold keys for, here or via the LLM_FALLBACKS env var (comma-separated), e.g.
#   LLM_FALLBACKS = ["claude-sonnet-4-6", "huggingface/zai-org/GLM-5.1"]
LLM_FALLBACKS: list[str] = _env_fallbacks()


async def resilient_acompletion(**kwargs):
    """``litellm.acompletion`` + retry/backoff + optional model fallbacks.

    Caller-supplied ``num_retries`` / ``fallbacks`` win (so a specific call can opt
    out); everything else is forwarded untouched.
    """
    kwargs.setdefault("num_retries", LLM_NUM_RETRIES)
    if LLM_FALLBACKS and "fallbacks" not in kwargs:
        kwargs["fallbacks"] = LLM_FALLBACKS
    return await _acompletion(**kwargs)
