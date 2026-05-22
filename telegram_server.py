from pydantic import BaseModel
from typing import Any
from agent_tools import AgentContext, ToolResult
from agent import AgentRuntime, askLLM2, print_llm_response, print_tool_result
import json
from fastapi import FastAPI
import os
from telegram import Bot

class TelegramMessage(BaseModel):
    chat_id: int
    message: str

async def send_llm_response_to_telegram(*, message, context: AgentContext) -> None:
    if message.content:
        await context.send_message(message.content)

async def send_tool_response_to_telegram(*, tool_result: ToolResult, name: str, args: dict[str, Any], context: AgentContext) -> None:
    status = "✓" if not tool_result.error else "✗"
    await context.send_message(f"{status} {name}")


app = FastAPI()

@app.post("/chat")
async def chat(request: TelegramMessage):
    chat_id = request.chat_id
    message = request.message
    messages = []
    messages.append({
        "role": "user", 
        "content": message
    })
    bot_token = os.environ.get("TELEGRAM_BOT_TOKEN")
    bot = Bot(token=bot_token)
    context: AgentContext = AgentContext(chat_id=chat_id, telegram_client=bot)
    runtime = AgentRuntime(context=context)
    runtime.on("on_model_response", send_llm_response_to_telegram)
    runtime.on("on_model_response", print_llm_response)
    runtime.on("on_tool_result", print_tool_result)
    runtime.on("on_tool_result", send_tool_response_to_telegram)
    while True: 
        next_message = await askLLM2(messages, runtime, "zai-org/GLM-5.1", context)
        if not next_message: 
            break
        messages.extend(next_message)
    return {"response": "ok"}

if __name__ == "__main__":
    import uvicorn
    uvicorn.run(app, host="0.0.0.0", port=8000)



