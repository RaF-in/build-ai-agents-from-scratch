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
from litellm import acompletion
import asyncio
import json
# import os
from dotenv import load_dotenv
from tool_base import AgentContext, AgentTool, ToolResult
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
    def __init__(self, context: AgentContext, session_manager: SessionManager, model_name: str):
        self.model_name = model_name
        self.session_manager = session_manager
        self.max_tool_calls_per_turn = 10
        self.context = context
        self._sys_prompt: str | None = None
        self.thinking_mode: Literal["off", "on", "stream"] = "off"
        self.agent_tool_module = agent_tools
        self.agent_tool_path = Path(agent_tools.__file__).resolve()
        self.agent_tool_lastModified = self.agent_tool_path.stat().st_mtime
        self.tools = {tool_cls.__name__: tool_cls for tool_cls in self.agent_tool_module.TOOLS}
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

    def get_tools(self) -> list:
        self.reload_runtime()
        return [tool.to_schema() for tool in self.tools.values()]
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
            await self.session_manager.add_message(
                SessionEvent(role="user", parts=[TextPart(text=user_text)])
            )

        conversation = await self.session_manager.load_messages()
        if self._sys_prompt:
            conversation = [SessionEvent(
                role="system",
                parts=[TextPart(text=self._sys_prompt)],
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
        if not has_tool_budget:
            messages.append({"role": "user", "content": TOOL_CALL_GUARDRAIL_INSTRUCTION})

        tools = self.get_tools() if has_tool_budget else None
        response = await acompletion(model=self.model_name, messages=messages, tools=tools)
        msg = response.choices[0].message
        tool_calls = msg.tool_calls

        if tool_calls:
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
            return False

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
        return True

    async def run_tool(self, tool_name: str, args: dict[str, Any]) -> ToolResult:
        self.reload_runtime()
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


