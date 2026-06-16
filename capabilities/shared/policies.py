"""Run policies (Phase 1) layered on the Phase 0.7 RunPolicy socket.

PlanExecutePolicy is the generator role's policy: it nudges the model to plan
into todos and — crucially — turns the todo list into a COMPLETION CONTRACT via
on_idle. When the model stops calling tools but open todos remain, on_idle
returns True so the run keeps going instead of wrapping up early. The kernel's
MAX_IDLE_CONTINUATIONS bound still caps how many times this may repeat without
progress, so a stuck generator can never spin forever.
"""
from __future__ import annotations

from tool_base import AgentContext
from .todo import TodoList

PLAN_EXECUTE_GUIDANCE = (
    "\n\nWork in a plan→execute loop:\n"
    "1. Decompose the task into concrete todos with AddTodo (one call per item).\n"
    "2. Do each todo's actual work with your tools, then call ListTodos to review.\n"
    "3. Mark finished items with CompleteTodo (only after their work is done); "
    "drop dead ends with AbandonTodo.\n"
    "Do NOT end your turn while any todo is still open — you will be prompted to "
    "continue. Finish only when every todo is completed or abandoned, then write "
    "your final artifact and give a short summary."
)


class PlanExecutePolicy:
    """Todo-aware generator policy (Phase 1.1 completion contract)."""

    def active_tools(self, all_tools: list) -> list:
        return all_tools

    def system_prompt(self, base: str) -> str:
        return base + PLAN_EXECUTE_GUIDANCE

    async def on_idle(self, context: AgentContext) -> bool:
        todos = getattr(context, "todo_list", None)
        if not isinstance(todos, TodoList) or not todos.items:
            # Nothing was planned (or no list) — treat an empty plan as done so a
            # trivial task isn't forced to invent todos.
            return False
        # Keep looping while any todo is still pending/in_progress.
        return todos.has_open()
