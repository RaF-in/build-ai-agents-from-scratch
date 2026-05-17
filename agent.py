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

from langsmith import traceable
from litellm import acompletion
import asyncio
import json
# import os
from dotenv import load_dotenv

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

def get_read_tool_schema():
    return {
        "type": "function", 
        "function": {
            "name": "read_file", 
            "description": "A helper method that reads content of a file from file path specified",
            "parameters": {
                "type": "object", 
                "properties": {
                    "path": {
                        "type": "string", 
                        "description": "file path"
                    }
                },
                "required": ["path"]
            }
        }
    }


TOOLS = {
    "read_file": read_file
}
tools = [
    get_read_tool_schema()
]
async def main():
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
        await askLLM(messages, "openai/qwen/qwen3.5-9b", api_base="http://127.0.0.1:1234/v1")
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
async def askLLM(messages, model_name: str, api_base=None): 
    kwargs = dict(model=model_name,  tools=tools, api_base=api_base, api_key="lm-studio" if api_base else None)
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
                fn = TOOLS.get(function_name)
                result = fn(**arguments) if fn else f"Error. No function named {function_name}"
                messages.append({
                    "role": "tool",
                    "tool_call_id": tool_call.id,
                    "name": function_name, 
                    "content": result
                })
                
        else:
            break
    print(messages[-1]["content"])
if __name__ == "__main__":
    asyncio.run(main())


