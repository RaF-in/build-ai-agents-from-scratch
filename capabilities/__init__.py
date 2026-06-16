"""Capability packs + the shared PGE engine.

The kernel (agent.py, tool_base.py, session.py, subagent*) stays
capability-agnostic. Everything domain-specific — and the one trio-runner that
sequences Planner -> Generator -> Evaluator — lives under here. Adding a
capability is dropping a config; the engine never learns the domain.
"""
