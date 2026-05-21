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

from langsmith import traceable
from litellm import acompletion
import asyncio
import json
# import os
from dotenv import load_dotenv
from agent_tools import  AgentContext, AgentTool, ToolResult
from pydantic import BaseModel
import importlib
from pathlib import Path
from rich import print
import agent_tools

# os.environ["LANGCHAIN_TRACING_V2"]="true"
# os.environ["LANGCHAIN_PROJECT"]="own_agent_demo"

load_dotenv()

HookType: TypeAlias = Literal["on_model_response", "on_tool_result"]

class ModelResponseHook(Protocol):
    def __call__(self, *, message: dict[str, Any], context: AgentContext) -> Awaitable[None]: ...
class ToolResultHook(Protocol): 
    def __call__(self, *, tool_result: ToolResult, args: dict[str, Any], name: str, context: AgentContext) -> Awaitable[None]: ...

HookAnyType: TypeAlias = ModelResponseHook | ToolResultHook

@traceable
def read_file(path: str): 
    try:
        with open(path, "r", encoding="utf-8") as f:
            content = f.read()
    except FileNotFoundError:
        return f"file not found in {path}"
    except Exception as e:
        return f"Error reading file {e}"
    return content

class AgentRuntime:
    """Minimal runtime that exposes registered tools for the model."""
    def __init__(self, context: AgentContext):
        self.context = context
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
    async def run_tool(self, tool_name: str, args: dict[str, Any]) -> ToolResult:
        self.reload_runtime()
        tool_cls = self.tools.get(tool_name)
        if not tool_cls:
            return ToolResult(error=True, result={
                "error": f"Tool {tool_name} is not available"
            })
        try:
            tool_obj = tool_cls.model_validate(args)
            tool_result: ToolResult = await tool_obj.execute(self.context)
            return tool_result
        except Exception as e:
            return ToolResult(error=True, result={
                "error": f"Error while running tool {tool_name}"
            })
async def print_llm_response( *, message: dict[str, Any], context: AgentContext): 
    print(f"[green] LLM has responded with {json.dumps(message.model_dump())}[/green]")
async def print_tool_result( *, tool_result: ToolResult, args: dict[str, Any], name: str, context: AgentContext): 
    print(f"[blue] tool {name} with args {json.dumps(args)} responds with result  {json.dumps(tool_result.model_dump())}[/blue]")
async def main():
    context = AgentContext()
    runtime = AgentRuntime(context=context)
    messages = []
    runtime.on("on_model_response", print_llm_response)
    runtime.on("on_tool_result", print_tool_result)
    while True:
        loop = asyncio.get_event_loop()
        inp = await loop.run_in_executor(None, input, "You: ")

        if inp.lower() in {"exit", "bye"}:
            break
        messages.append({
            "role": "user", 
            "content": inp
        })
        # await askLLM(messages, runtime, "openai/qwen/qwen3.5-9b", context, api_base="http://127.0.0.1:1234/v1")
        await askLLM(messages, runtime, "huggingface/MiniMaxAI/MiniMax-M2.7", context)
    # tasks = []
    # tasks.append(askLLM([{
    #     "role": "user", 
    #     "content": "Could you please tell me what is there in ./README.md"
    # }], "groq/openai/gpt-oss-120b"))
    # tasks.append(askLLM([{
    #     "role": "user", 
    #     "content": "Could you please tell me what is there in ./testing_files/test_file_1.txt"
    # }], "huggingface/MiniMaxAI/MiniMax-M2.7"))
    # tasks.append(askLLM([{
    #     "role": "user", 
    #     "content": "Could you please tell me what is there in ./testing_files/test_file_2.txt"
    # }], "openai/qwen/qwen3.5-9b", api_base="http://127.0.0.1:1234/v1"))

    # # await asyncio.gather(*tasks)
    # for task in tasks:
    #     await task


@traceable
async def askLLM(messages,  runtime: AgentRuntime, model_name: str, context: AgentContext, api_base=None): 
    kwargs = dict(model=model_name, api_base=api_base, api_key="lm-studio" if api_base else None)
    while True:
        response = await acompletion(**kwargs, messages=messages, stream=False, tools=runtime.get_tools())
        message = response.choices[0].message
        await runtime.emit("on_model_response", message=message, context=context)
        tool_calls = message.tool_calls

        msg = {"role": message.role, "content": message.content}
        if tool_calls:
            msg["tool_calls"] = [
                {
                    "id": tc.id,
                    "type": tc.type,
                    "function": {
                        "name": tc.function.name,
                        "arguments": tc.function.arguments
                    }
                }
                for tc in (tool_calls or [])
            ] 

        messages.append(msg)

        if tool_calls:
            for tool_call in tool_calls:
                function_name = tool_call.function.name
                arguments = json.loads(tool_call.function.arguments)
                # fn = TOOLS.get(function_name)
                # result = fn(**arguments) if fn else f"Error. No function named {function_name}"
                tool_result: ToolResult = await runtime.run_tool(function_name, arguments)
                await runtime.emit("on_tool_result", tool_result=tool_result, args=arguments, name=function_name, context=context)
                messages.append(tool_result.to_tool_message(tool_call.id))
                
        else:
            break
    print(messages[-1]["content"])
if __name__ == "__main__":
    asyncio.run(main())


