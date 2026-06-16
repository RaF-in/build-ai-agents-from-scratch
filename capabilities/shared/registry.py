"""The capability registry (Phase 2) — the config-plugin contract.

A capability is a folder ``capabilities/<name>/`` whose package exports a
module-level ``CAPABILITY`` of type :class:`CapabilitySpec`. That is the entire
contract: dropping such a folder makes the capability available with ZERO engine
or kernel edits. The registry discovers these folders, validates the contract,
and surfaces only each pack's thin ENTRY tools (progressive disclosure) — the
heavy role prompts/tools stay scoped inside the pipeline run.

This mirrors the existing ``skills.py`` discovery pattern (``skill_manager``):
scan a directory, import what's there, expose a small catalog + the invokable
handles, and tolerate a broken pack by skipping it instead of crashing.
"""
from __future__ import annotations

import importlib
from dataclasses import dataclass, field
from pathlib import Path

from rich import print

from tool_base import AgentTool

CAPABILITY_ATTR = "CAPABILITY"
# Subdirectories that are not capabilities themselves.
_SKIP = {"shared", "__pycache__"}


@dataclass
class CapabilitySpec:
    """What every capability package must export as ``CAPABILITY``.

    name         stable id (also the folder name by convention).
    summary      one line — the catalog entry / progressive-disclosure blurb.
    entry_tools  the thin AgentTool handle(s) the main agent may select. Their
                 docstrings are the only capability text that enters the main
                 prompt; everything else is scoped inside the run.
    """
    name: str
    summary: str
    entry_tools: list[type[AgentTool]] = field(default_factory=list)

    def validate(self) -> None:
        if not self.name:
            raise ValueError("CapabilitySpec.name is required")
        if not self.summary:
            raise ValueError(f"capability '{self.name}' is missing a summary")
        if not self.entry_tools:
            raise ValueError(f"capability '{self.name}' exposes no entry_tools")
        for tool in self.entry_tools:
            if not (isinstance(tool, type) and issubclass(tool, AgentTool)):
                raise TypeError(
                    f"capability '{self.name}' entry tool {tool!r} is not an AgentTool subclass"
                )


class CapabilityRegistry:
    """Discovers capability packs and surfaces their entry tools."""

    def __init__(self, package: str = "capabilities", root: Path | None = None):
        self.package = package
        # registry.py lives in capabilities/shared/, so capabilities/ is two up.
        self.root = root or Path(__file__).resolve().parent.parent
        self._specs: dict[str, CapabilitySpec] = {}

    def discover(self) -> None:
        """(Re)scan the capabilities package. Idempotent: rebuilds from scratch,
        so it is safe to call on every hot-reload. A pack that fails to import or
        violates the contract is logged and skipped — it never breaks the kernel."""
        self._specs = {}
        if not self.root.is_dir():
            return
        for entry in sorted(self.root.iterdir()):
            if not entry.is_dir() or entry.name in _SKIP or entry.name.startswith("_"):
                continue
            if not (entry / "__init__.py").exists():
                continue
            modname = f"{self.package}.{entry.name}"
            try:
                module = importlib.import_module(modname)
                spec = getattr(module, CAPABILITY_ATTR, None)
                if spec is None:
                    continue  # a subpackage that simply isn't a capability
                if not isinstance(spec, CapabilitySpec):
                    raise TypeError(f"{modname}.{CAPABILITY_ATTR} must be a CapabilitySpec")
                spec.validate()
                self._specs[spec.name] = spec
            except Exception as exc:
                print(f"[Capability Discovery] skipping '{entry.name}': {exc}")

    def names(self) -> list[str]:
        return list(self._specs)

    def entry_tools(self) -> list[type[AgentTool]]:
        """Flat list of every discovered pack's entry tools — merged into the
        main agent's tool set so the model can select a capability directly."""
        tools: list[type[AgentTool]] = []
        for spec in self._specs.values():
            tools.extend(spec.entry_tools)
        return tools

    def catalog(self) -> str:
        """A short ``- name: summary`` listing for an optional prompt blurb."""
        return "\n".join(f"- {s.name}: {s.summary}" for s in self._specs.values())


# Singleton, mirroring skill_manager. agent_tools calls discover() and merges
# entry_tools() into TOOLS at import time.
capability_registry = CapabilityRegistry()
