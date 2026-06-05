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
            "step-by-step procedure.\n\n"
            "**Ways to invoke a skill:**\n"
            "1. User types `/skill:<name>` - skill instructions are injected immediately\n"
            "2. You call `SkillInvokeTool(name=\"skill-name\")` - skill instructions are loaded for you to follow\n\n"
            f"{skills_prompt}"
        )

    return "\n\n".join(sections)