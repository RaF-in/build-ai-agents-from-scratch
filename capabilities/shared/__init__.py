"""Shared PGE engine & primitives (Phase 1).

  engine.py     run_pipeline() + PHASE_RUNNERS (plan/gen/eval) — the trio-runner
  config.py     RoleConfig, CapabilityConfig, Criterion dataclasses
  todo.py       Todo, TodoList + AddTodo/CompleteTodo/AbandonTodo/ListTodos tools
  policies.py   PlanExecutePolicy (todo-aware completion gate) for the generator
  artifacts.py  per-run workspace read/write helpers (spec.md, findings/, report.md)
"""
