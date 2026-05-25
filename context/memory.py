import os
import shutil
from typing import Any
WORKSPACE_DIR = os.path.expanduser("~/.ai_assistant/workspace")
TEMPLATES_DIR = os.path.join(os.path.dirname(__file__), "..", "workspace_templates")
class MemoryManager:
    def __init__(self):
        os.makedirs(WORKSPACE_DIR, exist_ok=True)
        self.workspaceDir = WORKSPACE_DIR
        self.templatesDir = os.path.abspath(TEMPLATES_DIR)
        self._seed_templates()
    
    def _seed_templates(self):
        if not os.path.exists(self.templatesDir):
            return
        is_exist = os.path.exists(os.path.join(self.workspaceDir, "SOUL.md"))
        for fileName in os.listdir(self.templatesDir):
            if is_exist and fileName == "BOOTSTRAP.md":
                continue
            dest = os.path.join(self.workspaceDir, fileName)
            if not os.path.exists(dest):
                shutil.copy2(os.path.join(self.templatesDir, fileName), dest)

    def read_memory_files(self) -> dict[str, Any]:
        memory_files = ["AGENTS.md", "BOOTSTRAP.md", "MEMORY.md", "SOUL.md", "USER.md"]
        mp = {}
        for file in memory_files:
            try:
                with open(os.path.join(self.workspaceDir, file), "r", encoding="utf-8") as f:
                    file_content = f.read().strip()
                    if file_content:
                        mp[file.split(".")[0].lower()] = file_content
            except Exception as e:
                pass
        return mp