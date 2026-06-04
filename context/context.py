from context.memory import MemoryManager
from datetime import datetime, timezone


def prepare_system_message(memory: MemoryManager, skills_prompt: str = ""):
    parts = [
        "You are a helpful assistant",
        f"Current time {datetime.now(timezone.utc)}",
        f"Work Space {memory.workspaceDir}",
        f"Always use absolute path while reading or writing files. Your Work Space is {memory.workspaceDir}"
    ]

    sections = ["\n".join(parts)]

    for file, file_content in memory.read_memory_files().items():
        sections.append(f"<{file}>\n{file_content}\n</{file}>")

    if skills_prompt:
        sections.append(
            "## Skills\n"
            "You have access to the following skills. Each skill is a reusable, "
            "step-by-step procedure. To run one, the user (or you) invokes it with "
            "`/skill:<name>` and its full instructions are injected for you to follow.\n"
            f"{skills_prompt}"
        )

    return "\n\n".join(sections)