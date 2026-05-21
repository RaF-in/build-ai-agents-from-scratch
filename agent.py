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

from typing import Any, Type

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
        self.tools = {tool_cls.__name__: tool_cls for tool_cls in agent_tools.TOOLS}
    def reload_runtime(self):
        currentModifiedTime = self.agent_tool_path.stat().st_mtime
        if currentModifiedTime == self.agent_tool_lastModified:
            return
        self.agent_tool_module = importlib.reload(self.agent_tool_module)
        self.agent_tool_path = Path(self.agent_tool_module.__file__).resolve()
        self.agent_tool_lastModified = self.agent_tool_path.stat().st_mtime
        self.tools = {tool_cls.__name__: tool_cls for tool_cls in agent_tools.TOOLS}
        reloaded_tools = ",".join(tool for tool in self.tools.keys())
        print(f"[cyan]reloaded tools are {reloaded_tools}[/cyan]")
        
    def get_tools(self) -> list:
        self.reload_runtime()
        return [tool.to_schema() for tool in self.tools]
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
            return await tool_obj.execute(self.context)
        except Exception as e:
            return ToolResult(error=True, result={
                "error": f"Error while running tool {tool_name}"
            })
        
async def main():
    context = AgentContext()
    runtime = AgentRuntime(context=context)
    messages = []
    while True:
        loop = asyncio.get_event_loop()
        inp = await loop.run_in_executor(None, input, "You: ")

        if inp.lower() in {"exit", "bye"}:
            break
        messages.append({
            "role": "user", 
            "content": inp
        })
        await askLLM(messages, runtime, "openai/qwen/qwen3.5-9b", api_base="http://127.0.0.1:1234/v1")
        # await askLLM(messages, runtime, "huggingface/MiniMaxAI/MiniMax-M2.7")
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
async def askLLM(messages,  runtime: AgentRuntime, model_name: str, api_base=None): 
    kwargs = dict(model=model_name,  tools=runtime.get_tools(), api_base=api_base, api_key="lm-studio" if api_base else None)
    while True:
        response = await acompletion(**kwargs, messages=messages, stream=False)
        message = response.choices[0].message

        tool_calls = message.tool_calls

        messages.append({
            "role": message.role,
            "content": message.content,
            "tool_calls": [
                {
                    "id": tc.id,
                    "type": tc.type,
                    "function": {
                        "name": tc.function.name,
                        "arguments": tc.function.arguments
                    }
                }
                for tc in (tool_calls or [])
            ] if tool_calls else None
        })
        if tool_calls:
            for tool_call in tool_calls:
                function_name = tool_call.function.name
                arguments = json.loads(tool_call.function.arguments)
                # fn = TOOLS.get(function_name)
                # result = fn(**arguments) if fn else f"Error. No function named {function_name}"
                tool_result: ToolResult = await runtime.run_tool(function_name, arguments)
                messages.append(tool_result.to_tool_message(tool_call.id))
                
        else:
            break
    print(messages[-1]["content"])
if __name__ == "__main__":
    asyncio.run(main())


