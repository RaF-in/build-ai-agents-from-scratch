from context.memory import MemoryManager
from datetime import datetime, timezone


def prepare_system_message(memory: MemoryManager):
    parts = [
        "You are a helpful assistant", 
        f"Current time {datetime.now(timezone.utc)}",
        f"Work Space {memory.workspaceDir}", 
        f"Always use absolute path while reading or writing files. Your Work Space is {memory.workspaceDir}"
    ]

    sections = ["\n".join(parts)]

    for file, file_content in memory.read_memory_files().items():
        sections.append(f"<{file}>\n{file_content}\n</{file}>")
    return "\n\n".join(sections)