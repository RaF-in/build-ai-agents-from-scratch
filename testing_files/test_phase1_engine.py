"""Standalone checks for Phase 1.1/1.2/1.3 (run: `uv run python testing_files/test_phase1_engine.py`).

Covers:
  1.1  Todo/TodoList + the four tools + the create-then-complete guardrail.
       PlanExecutePolicy completion gate (open todos -> keep looping).
  1.2  artifacts (workspace + read/final) and run_pipeline wiring.
  1.3  full plan->gen path end-to-end against a fake LLM, evaluator OFF,
       returning the generator's report.md artifact.
"""
import asyncio
import json
import os
import re
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import agent as agentmod
from tool_base import AgentContext
from agent_tools import WriteTool
from capabilities.shared.todo import (
    TodoList, AddTodo, CompleteTodo, AbandonTodo, ListTodos, TODO_TOOLS,
)
from capabilities.shared.policies import PlanExecutePolicy
from capabilities.shared import artifacts
from capabilities.shared.config import RoleConfig, CapabilityConfig
from capabilities.shared.engine import run_pipeline, spawn_role


def NS(**kw):
    o = type("NS", (), {})()
    for k, v in kw.items():
        setattr(o, k, v)
    return o


# ---------------- 1.1 todo primitive + tools + guardrail ----------------
async def test_todo_tools_and_guardrail():
    ctx = AgentContext(todo_list=TodoList())
    # tools refuse when there is no list
    no_list = await AddTodo(text="x").execute(AgentContext())
    assert no_list.error and "No active todo list" in no_list.result["error"]

    r = await AddTodo(text="research the topic").execute(ctx)
    assert not r.error and r.result["added"]["id"] == 1
    await AddTodo(text="write the report").execute(ctx)
    assert len(ctx.todo_list.items) == 2

    # guardrail: cannot complete a freshly-created (unlisted) todo
    blocked = await CompleteTodo(id=1).execute(ctx)
    assert blocked.error and "just created" in blocked.result["error"]

    # observe via ListTodos, then completion is allowed
    await ListTodos().execute(ctx)
    ok = await CompleteTodo(id=1).execute(ctx)
    assert not ok.error and ctx.todo_list.get(1).status == "completed"

    # abandon the other; list is now fully closed
    await AbandonTodo(id=2, reason="not needed").execute(ctx)
    assert ctx.todo_list.is_complete() and not ctx.todo_list.has_open()
    print("1.1 todo tools + create/complete guardrail: OK")


async def test_plan_execute_gate():
    policy = PlanExecutePolicy()
    # empty plan => done (don't force trivial tasks to invent todos)
    assert await policy.on_idle(AgentContext(todo_list=TodoList())) is False
    assert await policy.on_idle(AgentContext()) is False
    # open todo => keep looping; all closed => done
    tl = TodoList(); tl.add("do it")
    ctx = AgentContext(todo_list=tl)
    assert await policy.on_idle(ctx) is True
    tl.mark_listed(); tl.get(1).status = "completed"
    assert await policy.on_idle(ctx) is False
    # prompt augmentation is additive
    assert policy.system_prompt("BASE").startswith("BASE")
    print("1.1 PlanExecutePolicy completion gate: OK")


# ---------------- 1.2 artifacts ----------------
def test_artifacts():
    ws = artifacts.make_run_workspace()
    ctx = AgentContext(workspace_dir=ws)
    assert artifacts.read_artifact(ctx, "missing.md") == ""   # graceful
    artifacts.write_artifact(ctx, "spec.md", "1. a\n2. b")
    assert artifacts.read_artifact(ctx, "spec.md") == "1. a\n2. b"
    # final-artifact preference: spec.md until report.md exists
    assert "1. a" in artifacts.read_final_artifact(ws)
    artifacts.write_artifact(ctx, "report.md", "# Final")
    assert artifacts.read_final_artifact(ws) == "# Final"
    print("1.2 artifacts workspace/read/write/final: OK")


# ---------------- 1.3 full plan->gen via fake LLM, eval OFF ----------------
_gen_step = {"n": 0}


async def fake_acompletion(model, messages, tools=None, **kw):
    sys_prompt = messages[0]["content"] if messages and messages[0]["role"] == "system" else ""
    if "[[PLANNER]]" in sys_prompt:
        msg = NS(content="1. gather facts\n2. write report.md", tool_calls=None)
    elif "[[GENERATOR]]" in sys_prompt:
        ws = re.search(r"Your run workspace is ([^\s.]+)", sys_prompt).group(1)
        if _gen_step["n"] == 0:
            _gen_step["n"] = 1
            tc = NS(id="c1", function=NS(
                name="WriteTool",
                arguments=json.dumps({"path": f"{ws}/report.md",
                                      "content": "# Final Report\nGrounded findings."}),
            ))
            msg = NS(content="", tool_calls=[tc])
        else:
            msg = NS(content="report written", tool_calls=None)
    else:
        msg = NS(content="ok", tool_calls=None)
    return NS(choices=[NS(message=msg)])


async def test_pipeline_plan_gen():
    _gen_step["n"] = 0
    agentmod.acompletion = fake_acompletion

    config = CapabilityConfig(
        name="fake",
        planner=RoleConfig(name="planner", system_prompt="[[PLANNER]] expand the brief."),
        generator=RoleConfig(
            name="generator",
            system_prompt="[[GENERATOR]] do the work; write report.md.",
            tools=[WriteTool, *TODO_TOOLS],
            policy_factory=PlanExecutePolicy,
            fresh_todo_list=True,
        ),
        verify="off",
    )
    ctx = AgentContext()  # depth 0, max_depth 2
    result = await run_pipeline("Research X", config=config, context=ctx)

    assert result == "# Final Report\nGrounded findings.", repr(result)
    # planner spec was handed off via file
    assert "gather facts" in artifacts.read_artifact(ctx, "spec.md")
    # the shared run budget actually counted the generator's tool dispatch
    assert ctx.run_budget is not None and ctx.run_budget.calls >= 1
    # run-scoped sockets were wired by the engine
    assert ctx.workspace_dir and ctx.run_semaphore is not None
    print(f"1.3 plan->gen end-to-end (eval off) -> artifact; budget.calls={ctx.run_budget.calls}: OK")


async def main():
    await test_todo_tools_and_guardrail()
    await test_plan_execute_gate()
    test_artifacts()
    await test_pipeline_plan_gen()
    print("\nALL PHASE 1 CHECKS PASSED")


if __name__ == "__main__":
    asyncio.run(main())
