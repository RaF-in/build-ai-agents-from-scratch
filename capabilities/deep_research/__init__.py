"""Deep-research capability pack (Phase 2 reference implementation of the contract).

Exports the one thing the registry's contract requires: a module-level
``CAPABILITY`` describing the pack and its entry tool. Everything else
(config, prompts, criteria) is plain data consumed by the shared engine.
"""
from capabilities.shared.registry import CapabilitySpec
from capabilities.deep_research.entry import DeepResearchTool

CAPABILITY = CapabilitySpec(
    name="deep_research",
    summary="Research a topic in depth (plan → gather → synthesize) and return a cited report.",
    entry_tools=[DeepResearchTool],
)
