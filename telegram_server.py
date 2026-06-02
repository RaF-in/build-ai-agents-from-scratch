from pydantic import BaseModel
from typing import Any, Optional
from pathlib import Path
from agent_tools import AgentContext, ToolResult
from agent import AgentRuntime, render_history_event, print_llm_response, print_tool_result
from session import SessionManager
import os
import asyncio
from fastapi import FastAPI
from telegram import Bot
from context.context import prepare_system_message
from context.memory import MemoryManager
from slash_commands import TelegramOutputHandler, parse_and_execute

BASE_DIR = Path(__file__).resolve().parent
MODEL_NAME = "huggingface/zai-org/GLM-5.1"

bot = Bot(token=os.environ.get("TELEGRAM_BOT_TOKEN"))
cron_service = None

_runtimes: dict[int, AgentRuntime] = {}
_runtime_locks: dict[int, asyncio.Lock] = {}

def setup(cron_svc):
    global cron_service
    cron_service = cron_svc

# Telegram webhook update models
class TelegramChat(BaseModel):
    id: int

class TelegramMessageUpdate(BaseModel):
    chat: TelegramChat
    text: Optional[str] = None

class TelegramUpdate(BaseModel):
    update_id: int
    message: Optional[TelegramMessageUpdate] = None

async def send_llm_response_to_telegram(*, message, context: AgentContext) -> None:
    if message.content:
        await context.send_message(message.content)

async def send_tool_response_to_telegram(*, tool_result: ToolResult, name: str, args: dict[str, Any], context: AgentContext) -> None:
    status = "✓" if not tool_result.error else "✗"
    await context.send_message(f"{status} {name}")


async def get_runtime(chat_id: int):
    lock = _runtime_locks.setdefault(chat_id, asyncio.Lock())
    async with lock:
        if chat_id not in _runtimes:
            session_dir = BASE_DIR / "sessions" / str(chat_id)
            session_dir.mkdir(parents=True, exist_ok=True)
            sm = SessionManager(basedir=session_dir, model_name=MODEL_NAME)
            runtime = AgentRuntime(
                context=AgentContext(cron_service=cron_service),
                session_manager=sm,
                model_name=MODEL_NAME,
            )
            runtime.on("on_model_response", send_llm_response_to_telegram)
            runtime.on("on_model_response", print_llm_response)
            runtime.on("on_tool_result", send_tool_response_to_telegram)
            runtime.on("on_tool_result", print_tool_result)
            await runtime.initialize(replay_handler=render_history_event)
            _runtimes[chat_id] = runtime
    sys_prompt = prepare_system_message(MemoryManager())
    return _runtimes[chat_id], sys_prompt

app = FastAPI()

@app.post("/webhook")
async def webhook(update: TelegramUpdate):
    if not update.message or not update.message.text:
        return {"ok": True}
    chat_id = update.message.chat.id
    runtime, sys_prompt = await get_runtime(chat_id)
    runtime.context = AgentContext(chat_id=chat_id, telegram_client=bot, cron_service=cron_service)

    # Telegram-specific output handler
    output = TelegramOutputHandler(runtime.context)

    # Parse slash commands
    text = update.message.text
    result = await parse_and_execute(text, runtime, output)
    if result is True:  # Command handled, skip LLM
        return {"ok": True}

    # Normal LLM flow
    has_more = await runtime.run(user_text=text, sys_prompt=sys_prompt)
    while has_more:
        has_more = await runtime.run(user_text=None, sys_prompt=None)
    return {"ok": True}

if __name__ == "__main__":
    from cron.cron import CronService
    cron_service = CronService(queue=asyncio.Queue())
    setup(cron_service)
    import uvicorn
    uvicorn.run(app, host="0.0.0.0", port=8000)
