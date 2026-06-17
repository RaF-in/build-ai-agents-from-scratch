"""Article-writer capability pack (Phase 8 — the "prove reuse" second config).

Exports the one thing the registry contract requires: a module-level ``CAPABILITY``.
Dropping this folder surfaces a working capability with ZERO edits to engine.py or
the kernel — everything else is plain data (prompts, criteria, config) consumed by
the shared engine, web layer, and generic tools.
"""
from capabilities.shared.registry import CapabilitySpec
from capabilities.article_writer.entry import WriteArticleTool

CAPABILITY = CapabilitySpec(
    name="article_writer",
    summary="Draft a long-form article (outline -> write -> self-edit) with voice and sources.",
    entry_tools=[WriteArticleTool],
)
