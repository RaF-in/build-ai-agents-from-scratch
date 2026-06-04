import asyncio
from cron.cron import CronService
from telegram_server import setup as setup_telegram, get_runtime, bot, app
import agent
from agent_tools import AgentContext
from context.context import prepare_system_message
from context.memory import MemoryManager
import uvicorn


async def process_cron_messages(cron_service):
    while True:
        msg = await cron_service.queue.get()
        try:
            channel = msg["channel"]

            if channel == "telegram":
                chat_id = int(msg["chat_id"])
                runtime, sys_prompt = await get_runtime(chat_id)
                runtime.context = AgentContext(
                    chat_id=chat_id,
                    telegram_client=bot,
                    cron_service=cron_service,
                )

            elif channel == "cli":
                if not agent.cli_runtime:
                    continue
                runtime = agent.cli_runtime
                from skills import skill_manager
                sys_prompt = prepare_system_message(MemoryManager(), skill_manager.format_for_prompt())

            else:
                continue

            has_more = await runtime.run(user_text=msg["text"], sys_prompt=sys_prompt)
            while has_more:
                has_more = await runtime.run(user_text=None, sys_prompt=None)

        except Exception as e:
            print(f"[Cron Error] {e}")


async def main():
    cron_queue = asyncio.Queue()
    cron_service = CronService(queue=cron_queue)

    setup_telegram(cron_service)

    asyncio.create_task(cron_service.run())
    asyncio.create_task(process_cron_messages(cron_service))

    telegram_task = asyncio.create_task(
        uvicorn.Server(uvicorn.Config(app, host="0.0.0.0", port=8000)).serve()
    )
    cli_task = asyncio.create_task(agent.start_cli(cron_service))

    await asyncio.gather(telegram_task, cli_task)


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        print("\nShutting down...")
