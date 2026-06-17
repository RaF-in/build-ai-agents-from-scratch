# litellm
# parallel LLM calling - huggingface, groq, lmstudio
# langsmith
# async functionalities 
# stream
# fallback 

# Create api key of necessary libraries 
# Define reading tool func
# Define reading tool json schema
# Prepare a method askLLM which will take a message and pass the llm and print streaming response 
# Start LM studio 
# Define main method 

from typing import Any, Awaitable, Type, TypeAlias, Literal, Protocol, overload
from context.context import prepare_system_message
from context.memory import MemoryManager
# Phase 7.3: route model calls through the resilience wrapper (retry/backoff +
# optional fallbacks). Kept bound to the name `acompletion` so call sites — and the
# tests that monkeypatch `agent.acompletion` — are unchanged.
from llm_resilience import resilient_acompletion as acompletion
import asyncio
import json
# import os
from dotenv import load_dotenv
from tool_base import AgentContext, AgentTool, ToolResult, RunPolicy, DEFAULT_POLICY
import importlib
from pathlib import Path
from rich import print
import agent_tools
from session import (
    SessionManager, SessionEvent, CompactionEvent,
    TextPart, FunctionCallPart, FunctionResponsePart,
    FunctionCallData, FunctionResponseData, FcMetaData,
    StoredEvent,
)
from slash_commands import CLIOutputHandler, parse_and_execute
from skills import skill_manager

TOOL_CALL_GUARDRAIL_INSTRUCTION = (
    "You have used a large number of tool calls. "
    "Stop calling tools and summarise what you have done so far. "
    "Ask the user for clarification before proceeding."
)

# Loop safety for the Phase 0.7 completion gate: a non-default policy may keep
# the run looping on_idle (e.g. while todos remain). This bounds how many times
# in a row on_idle can re-loop WITHOUT any intervening tool call, so a misbehaving
# policy can never spin forever. DefaultPolicy.on_idle returns False, so the main
# agent never reaches this counter — its behavior is unchanged.
MAX_IDLE_CONTINUATIONS = 3

# Phase 7.2 loop safety: if the model emits the EXACT same tool call(s) this many
# times in a row, the run is stuck (e.g. retrying a failing fetch forever). Abort the
# tool loop and force a no-tools summarizing turn so the run ends with whatever
# partial result exists, instead of spinning until the budget/turn cap. Normal agents
# vary their calls and never hit this.
MAX_REPEATED_TOOL_CALLS = 3

REPEATED_TOOL_CALL_GUARDRAIL = (
    "You have repeated the same tool call several times with no new result. "
    "Stop calling tools now and summarise what you have found so far, noting what "
    "remains unresolved."
)

# os.environ["LANGCHAIN_TRACING_V2"]="true"
# os.environ["LANGCHAIN_PROJECT"]="own_agent_demo"

load_dotenv()

cli_runtime = None

HookType: TypeAlias = Literal["on_model_response", "on_tool_result"]

class ModelResponseHook(Protocol):
    def __call__(self, *, message: dict[str, Any], context: AgentContext) -> Awaitable[None]: ...
class ToolResultHook(Protocol): 
    def __call__(self, *, tool_result: ToolResult, args: dict[str, Any], name: str, context: AgentContext) -> Awaitable[None]: ...

HookAnyType: TypeAlias = ModelResponseHook | ToolResultHook

class AgentRuntime:
    """Minimal runtime that exposes registered tools for the model."""
    def __init__(self, context: AgentContext, session_manager: SessionManager, model_name: str, tools_override: list[Type[AgentTool]] | None = None, max_tool_calls_per_turn: int = 10):
        self.model_name = model_name
        self.session_manager = session_manager
        # Phase 0.3: per-turn anti-runaway guardrail, now configurable per runtime.
        # The main agent keeps the conservative default (10) — after this many
        # tool rounds since the last user message it stops and asks the user. A
        # spawned generator/worker CANNOT ask a user, so the PGE engine (Phase 1)
        # constructs those runtimes with a high cap (e.g. 200) so a long fan-out +
        # synthesis run is never force-stopped mid-turn. This is only the per-turn
        # brake; RunBudget (Phase 0.6) remains the total ceiling for a whole run.
        self.max_tool_calls_per_turn = max_tool_calls_per_turn
        self.context = context
        self._sys_prompt: str | None = None
        # Phase 0.7: consecutive on_idle continuations since the last tool call,
        # bounded by MAX_IDLE_CONTINUATIONS. Reset on a new user turn and whenever a
        # tool actually runs. Stays 0 for the main agent (DefaultPolicy never loops).
        self._idle_continuations = 0
        # Phase 7.2: signature of the previous turn's tool call(s) + how many times in
        # a row it has repeated, and a one-shot flag to force a no-tools summarizing
        # turn once the repeat limit is hit. Reset on every new user turn.
        self._last_tool_sig: str | None = None
        self._repeat_count = 0
        self._force_summary = False
        self.thinking_mode: Literal["off", "on", "stream"] = "off"
        self.agent_tool_module = agent_tools
        self.agent_tool_path = Path(agent_tools.__file__).resolve()
        self.agent_tool_lastModified = self.agent_tool_path.stat().st_mtime
        if tools_override is not None:
            # Child runtime: fixed tool set, hot-reload disabled (structural depth limit).
            self.tools = {tool_cls.__name__: tool_cls for tool_cls in tools_override}
            self._tools_locked = True
        else:
            self.tools = {tool_cls.__name__: tool_cls for tool_cls in self.agent_tool_module.TOOLS}
            self._tools_locked = False
        self._hooks: dict[HookType, list[HookAnyType]] = {
            "on_model_response": [],
            "on_tool_result": []
        }
    
    @overload
    def on(self, event: Literal["on_model_response"], handler: ModelResponseHook) -> "AgentRuntime": ...
    @overload
    def on(self, event: Literal["on_tool_result"], handler: ToolResultHook) -> "AgentRuntime": ...
    def on(self, event: HookType, handler: HookAnyType): 
        self._hooks[event].append(handler)
        return self
    async def emit(self, event: HookType, **kwargs): 
        for handler in self._hooks[event]: 
            try:
                await handler(**kwargs)
            except Exception as e:
                print(f"[Hook Error] Event '{event}' failed: {e}")

    def reload_runtime(self):
        if self._tools_locked:
            return  # children never reload — they can't gain new tools
        currentModifiedTime = self.agent_tool_path.stat().st_mtime
        if currentModifiedTime == self.agent_tool_lastModified:
            return
        try:
            self.agent_tool_module = importlib.reload(self.agent_tool_module)
            self.agent_tool_path = Path(self.agent_tool_module.__file__).resolve()
            self.agent_tool_lastModified = self.agent_tool_path.stat().st_mtime
            self.tools = {tool_cls.__name__: tool_cls for tool_cls in self.agent_tool_module.TOOLS}
            reloaded_tools = ",".join(tool for tool in self.tools.keys())
            print(f"[cyan]reloaded tools are {reloaded_tools}[/cyan]")
            return True
        except Exception as exc:
          print(f"[Tool Reload] Failed, keeping previous runtime: {exc}")
          return False

    def _policy(self) -> RunPolicy:
        """The active per-run policy (Phase 0.7); DefaultPolicy when context.policy is None."""
        return self.context.policy or DEFAULT_POLICY

    def get_tools(self) -> list:
        self.reload_runtime()
        schemas = [tool.to_schema() for tool in self.tools.values()]
        # Phase 0.7: a run policy may hide tools (e.g. plan-mode = read-only).
        # DefaultPolicy returns them unchanged, so the main agent is unaffected.
        return self._policy().active_tools(schemas)
    def register_tool(self, new_tool: Type[AgentTool]):
        self.tools[new_tool.__name__] = new_tool
    async def initialize(self, *, replay_handler=None) -> None:
        await self.session_manager.initialize()
        if replay_handler is None:
            return
        events = await self.session_manager.load_messages()
        for event in events:
            await replay_handler(event=event)

    async def run(self, user_text: str | None, sys_prompt: str | None) -> bool:
        """
        Pass user_text on first call. Pass None when looping after tool calls.
        Returns True if tools were called (keep looping), False when done.
        """
        if user_text is not None:
            self._sys_prompt = sys_prompt
            self._idle_continuations = 0  # fresh user turn — reset the idle gate
            self._last_tool_sig = None    # and the repeated-call detector (7.2)
            self._repeat_count = 0
            self._force_summary = False
            await self.session_manager.add_message(
                SessionEvent(role="user", parts=[TextPart(text=user_text)])
            )

        conversation = await self.session_manager.load_messages()
        if self._sys_prompt:
            # Phase 0.7: the policy may wrap/extend the base prompt. DefaultPolicy
            # returns it unchanged, so the main agent's prompt is identical.
            sys_text = self._policy().system_prompt(self._sys_prompt)
            conversation = [SessionEvent(
                role="system",
                parts=[TextPart(text=sys_text)],
            )] + conversation

        last_user_idx = -1
        for idx in range(len(conversation) - 1, -1, -1):
            event = conversation[idx]
            if isinstance(event, SessionEvent) and event.role == "user":
                last_user_idx = idx
                break
        events_since_user = (
            len(conversation) if last_user_idx < 0
            else len(conversation) - last_user_idx - 1
        )
        has_tool_budget = events_since_user < self.max_tool_calls_per_turn

        messages = SessionManager.events_to_litellm_messages(conversation)
        # Phase 7.2: a forced summary turn (repeat limit hit) suppresses tools exactly
        # like the per-turn budget brake, so the model must answer in prose and the
        # loop terminates. _force_summary is one-shot.
        force_summary = self._force_summary
        self._force_summary = False
        if force_summary:
            messages.append({"role": "user", "content": REPEATED_TOOL_CALL_GUARDRAIL})
        elif not has_tool_budget:
            messages.append({"role": "user", "content": TOOL_CALL_GUARDRAIL_INSTRUCTION})

        tools = self.get_tools() if (has_tool_budget and not force_summary) else None
        response = await acompletion(model=self.model_name, messages=messages, tools=tools)
        msg = response.choices[0].message
        tool_calls = msg.tool_calls

        if tool_calls:
            # Phase 7.2: detect the model repeating the EXACT same call(s). On the Nth
            # consecutive identical turn, drop this turn (don't add or execute it) and
            # force a no-tools summary next turn, so a stuck loop ends cleanly.
            sig = json.dumps(
                [[tc.function.name, tc.function.arguments] for tc in tool_calls],
                sort_keys=True,
            )
            self._repeat_count = self._repeat_count + 1 if sig == self._last_tool_sig else 1
            self._last_tool_sig = sig
            if self._repeat_count >= MAX_REPEATED_TOOL_CALLS:
                self._repeat_count = 0
                self._last_tool_sig = None
                self._force_summary = True
                return True   # next turn summarises with tools suppressed

            await self.session_manager.add_message(SessionEvent(
                role="assistant",
                parts=[
                    FunctionCallPart(
                        data=FunctionCallData(
                            name=tc.function.name,
                            args=json.loads(tc.function.arguments),
                        ),
                        tool_call_id=tc.id,
                    )
                    for tc in tool_calls
                ],
            ))
        else:
            await self.session_manager.add_message(SessionEvent(
                role="assistant",
                parts=[TextPart(text=msg.content or "")],
            ))

        await self.emit("on_model_response", message=msg, context=self.context)

        if not tool_calls:
            # Phase 0.7 completion gate. DefaultPolicy.on_idle => False (done), so the
            # main agent stops exactly as before. A capability policy may return True to
            # keep looping while work remains (e.g. open todos); MAX_IDLE_CONTINUATIONS
            # bounds consecutive idle re-loops so a misbehaving policy can't spin.
            if self._idle_continuations >= MAX_IDLE_CONTINUATIONS:
                self._idle_continuations = 0
                return False
            keep_going = await self._policy().on_idle(self.context)
            self._idle_continuations = self._idle_continuations + 1 if keep_going else 0
            return keep_going

        response_parts = []
        for tc in tool_calls:
            args = json.loads(tc.function.arguments)
            result = await self.run_tool(tc.function.name, args)
            await self.emit("on_tool_result", tool_result=result, args=args, name=tc.function.name, context=self.context)
            response_parts.append(FunctionResponsePart(
                data=FunctionResponseData(name=tc.function.name, response=result.result),
                tool_call_id=tc.id,
                fc_metadata=FcMetaData(name=tc.function.name, args=args),
            ))

        await self.session_manager.add_message(SessionEvent(role="tool", parts=response_parts))
        self._idle_continuations = 0  # real progress this turn — clear the idle gate
        return True

    async def run_tool(self, tool_name: str, args: dict[str, Any]) -> ToolResult:
        self.reload_runtime()
        # Phase 0.6: enforce the total run budget BEFORE dispatch. The budget is shared
        # across the whole run (generator + workers); when exhausted we return a clean
        # error so the run wraps up with partial results instead of crashing. None on
        # the main agent => no total cap => behavior unchanged.
        budget = self.context.run_budget
        if budget is not None:
            if budget.exhausted():
                return ToolResult(name=tool_name, error=True, result={
                    "error": (
                        "Run budget exhausted (tool-call or wall-clock limit reached). "
                        "Stop calling tools and summarise the partial results so far."
                    )
                })
            budget.calls += 1
        tool_cls = self.tools.get(tool_name)
        if not tool_cls:
            return ToolResult(name=tool_name, error=True, result={
                "error": f"Tool {tool_name} is not available"
            })
        try:
            tool_obj = tool_cls.model_validate(args)
            tool_result: ToolResult = await tool_obj.execute(self.context)
            return tool_result
        except Exception as e:
            return ToolResult(name=tool_name, error=True, result={
                "error": f"Error while running tool {tool_name}"
            })
async def print_llm_response(*, message: dict[str, Any], context: AgentContext):
    if message.content:
        print(f"[green]Assistant: {message.content}[/green]")
async def print_tool_result( *, tool_result: ToolResult, args: dict[str, Any], name: str, context: AgentContext):
    print(f"[blue] tool {name} with args {json.dumps(args)} responds with result  {json.dumps(tool_result.model_dump())}[/blue]")

async def render_history_event(*, event: StoredEvent) -> None:
    if isinstance(event, CompactionEvent):
        return
    for part in event.parts:
        if isinstance(part, TextPart):
            if event.role == "user":
                print(f"You: {part.text}")
            elif event.role == "assistant":
                print(f"Assistant: {part.text}")
            continue
        if isinstance(part, FunctionCallPart):
            continue
        call_name = part.fc_metadata.name if part.fc_metadata else part.data.name
        call_args = part.fc_metadata.args if part.fc_metadata else {}
        error = "error" in part.data.response
        status = "[green]✓[/green]" if not error else "[red]✗[/red]"
        print(f"{status} [bold]{call_name}[/bold] {call_args}")

def _create_cli_runtime(cron_service=None):
    context = AgentContext(cron_service=cron_service)
    model_name = "huggingface/zai-org/GLM-5.1"
    session_dir = Path.home() / ".ai_assistant" / "sessions" / "cli"
    session_dir.mkdir(parents=True, exist_ok=True)
    sm = SessionManager(basedir=session_dir, model_name=model_name)
    runtime = AgentRuntime(context=context, session_manager=sm, model_name=model_name)
    runtime.on("on_model_response", print_llm_response)
    runtime.on("on_tool_result", print_tool_result)
    return runtime

async def _cli_loop(runtime):
    output = CLIOutputHandler()

    skill_manager.discover()
    if skill_manager.names():
        print(f"[magenta]Skills: {', '.join(skill_manager.names())}[/magenta]")

    await runtime.initialize(replay_handler=render_history_event)
    while True:
        loop = asyncio.get_event_loop()
        inp = await loop.run_in_executor(None, input, "You: ")

        # Legacy exit shortcuts
        if inp.lower() in {"exit", "bye"}:
            break

        # Slash commands
        result = await parse_and_execute(inp, runtime, output)
        if result == "QUIT":
            break
        if result is True:  # Command handled, skip LLM
            continue

        # A /skill: invocation expands into a full LLM turn.
        user_text = result[1] if isinstance(result, tuple) else inp

        # Normal LLM flow
        sys_prompt = prepare_system_message(MemoryManager(), skill_manager.format_for_prompt())
        has_more = await runtime.run(user_text=user_text, sys_prompt=sys_prompt)
        while has_more:
            has_more = await runtime.run(user_text=None, sys_prompt=None)

async def start_cli(cron_service):
    global cli_runtime
    runtime = _create_cli_runtime(cron_service=cron_service)
    cli_runtime = runtime
    await _cli_loop(runtime)

async def main():
    runtime = _create_cli_runtime()
    await _cli_loop(runtime)

if __name__ == "__main__":
    asyncio.run(main())


