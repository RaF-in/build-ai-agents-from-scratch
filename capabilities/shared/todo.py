"""The todo-list primitive (Phase 1.1) — shared, not research-only.

The planner's output is a todo list; it is promoted to a SHARED primitive
because coding (features) and writing (sections) want the identical thing. It is
three things at once:

  * Decomposition artifact — the spec, made executable.
  * Handoff / progress ledger — survives compaction or a context reset.
  * Completion contract — PlanExecutePolicy's on_idle gate (see policies.py)
    refuses to finish while any todo is still open, killing "context anxiety"
    early wrap-up.

The four tools (AddTodo, CompleteTodo, AbandonTodo, ListTodos) mutate
``context.todo_list``. They are plain AgentTools, given to the generator role via
its RoleConfig; the kernel never knows about them.
"""
from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, PrivateAttr

from tool_base import AgentContext, AgentTool, ToolResult

TodoStatus = Literal["pending", "in_progress", "completed", "abandoned"]
_OPEN: tuple[TodoStatus, ...] = ("pending", "in_progress")
_CLOSED: tuple[TodoStatus, ...] = ("completed", "abandoned")


class Todo(BaseModel):
    id: int
    text: str
    status: TodoStatus = "pending"


class TodoList(BaseModel):
    items: list[Todo] = []
    # Guardrail state (Phase 1.1): ids created but not yet "observed" via
    # ListTodos. CompleteTodo refuses to close one of these, which operationalizes
    # the v3 rule "never create AND complete a todo in the same turn" without the
    # kernel exposing a turn boundary — the model must observe state (ListTodos)
    # before it may mark freshly-created work done. Not serialized.
    _created_unlisted: set[int] = PrivateAttr(default_factory=set)

    def is_complete(self) -> bool:
        return all(t.status in _CLOSED for t in self.items)

    def has_open(self) -> bool:
        return any(t.status in _OPEN for t in self.items)

    def count_open(self) -> int:
        """Number of still-open todos — surfaced as an agent.todos.open span attr."""
        return sum(1 for t in self.items if t.status in _OPEN)

    def get(self, todo_id: int) -> Todo | None:
        return next((t for t in self.items if t.id == todo_id), None)

    def add(self, text: str) -> Todo:
        todo = Todo(id=(max((t.id for t in self.items), default=0) + 1), text=text)
        self.items.append(todo)
        self._created_unlisted.add(todo.id)
        return todo

    def mark_listed(self) -> None:
        self._created_unlisted.clear()

    def render(self) -> str:
        if not self.items:
            return "(no todos yet)"
        marks = {"pending": "[ ]", "in_progress": "[~]",
                 "completed": "[x]", "abandoned": "[-]"}
        return "\n".join(f"{marks[t.status]} {t.id}. {t.text}" for t in self.items)


def _require_list(context: AgentContext, tool: AgentTool) -> ToolResult | TodoList:
    todos = getattr(context, "todo_list", None)
    if not isinstance(todos, TodoList):
        return tool.tool_result(error=True, result={
            "error": "No active todo list. Todo tools are only usable inside a "
                     "pipeline run (the generator role)."
        })
    return todos


class AddTodo(AgentTool):
    """Add one item to the plan. Break the work into small, verifiable steps.

    Args:
        text: A single, concrete task. Add several by calling this once per task.
    """
    text: str

    async def execute(self, context: AgentContext) -> ToolResult:
        todos = _require_list(context, self)
        if isinstance(todos, ToolResult):
            return todos
        text = (self.text or "").strip()
        if not text:
            return self.tool_result(error=True, result={"error": "Todo text is empty."})
        todo = todos.add(text)
        return self.tool_result(result={"added": todo.model_dump(), "todos": todos.render()})


class CompleteTodo(AgentTool):
    """Mark a todo done — only after you have actually finished its work.

    You cannot complete a todo you created without first calling ListTodos to
    review the plan (this prevents create-and-immediately-complete busywork).

    Args:
        id: The id of the todo to mark completed.
    """
    id: int

    async def execute(self, context: AgentContext) -> ToolResult:
        todos = _require_list(context, self)
        if isinstance(todos, ToolResult):
            return todos
        todo = todos.get(self.id)
        if todo is None:
            return self.tool_result(error=True, result={"error": f"No todo with id {self.id}."})
        if self.id in todos._created_unlisted:
            return self.tool_result(error=True, result={
                "error": (
                    f"Todo {self.id} was just created. Do its work and call "
                    "ListTodos to review the plan before completing it."
                )
            })
        todo.status = "completed"
        return self.tool_result(result={"completed": todo.model_dump(), "todos": todos.render()})


class AbandonTodo(AgentTool):
    """Drop a todo that turned out to be unnecessary or impossible.

    Args:
        id: The id of the todo to abandon.
        reason: Why it is being dropped (kept on the ledger for the handoff).
    """
    id: int
    reason: str = ""

    async def execute(self, context: AgentContext) -> ToolResult:
        todos = _require_list(context, self)
        if isinstance(todos, ToolResult):
            return todos
        todo = todos.get(self.id)
        if todo is None:
            return self.tool_result(error=True, result={"error": f"No todo with id {self.id}."})
        todo.status = "abandoned"
        todos._created_unlisted.discard(self.id)
        return self.tool_result(result={
            "abandoned": todo.model_dump(), "reason": self.reason, "todos": todos.render(),
        })


class ListTodos(AgentTool):
    """Review the current plan and progress. Call this before completing work."""

    async def execute(self, context: AgentContext) -> ToolResult:
        todos = _require_list(context, self)
        if isinstance(todos, ToolResult):
            return todos
        todos.mark_listed()
        return self.tool_result(result={
            "todos": todos.render(),
            "complete": todos.is_complete(),
            "open": sum(1 for t in todos.items if t.status in _OPEN),
        })


# The set given to a generator role's RoleConfig.tools.
TODO_TOOLS: list[type[AgentTool]] = [AddTodo, CompleteTodo, AbandonTodo, ListTodos]
