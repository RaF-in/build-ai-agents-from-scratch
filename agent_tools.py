from typing import Any
import os
import subprocess
from telegram import Bot

from tool_base import AgentContext, AgentTool, ToolResult

class ReadTool(AgentTool):
    '''A Read tool which helps to read contents from a file'''
    path: str
    async def execute(self, context: AgentContext) -> ToolResult:
        if not os.path.exists(self.path) or not os.path.isfile(self.path):
            return self.tool_result(result={
                "error": f"path does not exist in {self.path}"
            }, error=True)
        try: 
            with open(self.path, "r", encoding="utf-8") as f:
                content = f'''
                the contents of file in path {self.path} are following 
                <content> 
                {f.read()}
                </content>
                '''
                return self.tool_result(result={
                    "content": content
                })
        except Exception as e: 
            return self.tool_result(error=True, result={
                "error": f"Error opening file at path {self.path}"
            })
    
class WriteTool(AgentTool):
    '''A write tool which writes a file'''
    path: str
    content: str
    async def execute(self, context: AgentContext) -> ToolResult:
        parent = os.path.dirname(self.path)
        if parent: 
            os.makedirs(parent, exist_ok=True)
        try:
            with open(self.path, "w", encoding="utf-8") as f: 
                f.write(self.content)
            return self.tool_result(result={
                "content": f"file written successfully at path {self.path}"
            })
        except Exception as e: 
            return self.tool_result(error=True, result={
                "error": f"Error writing to file at path {self.path}"
            })

    
class EditTool(AgentTool):
    '''An edit file tool which edits an existing file'''
    oldContent: str
    newContent: str
    path: str
    replaceAll: bool = False
    async def execute(self, context: AgentContext) -> ToolResult:
        if not os.path.exists(self.path) or not os.path.isfile(self.path):
            return self.tool_result(error=True, result={
                "error": f"file or path not exist at path {self.path}"
            })
        try:
            with open(self.path, "r", encoding="utf-8") as f:
                fileContent = f.read()
        except Exception as e: 
            return self.tool_result(error=True, result={
                "error": f"Error opening file at path {self.path}"
            })
        if self.oldContent not in fileContent:
            return self.tool_result(error=True, result={
                "error": f"old content does not exist in file {self.path}"
            })
        
        cntOld = fileContent.count(self.oldContent)
        if cntOld > 1 and not self.replaceAll:
            return self.tool_result(error=True, result={
                "error": f"Many occurences of old content {self.oldContent} found in path {self.path}. Please replace all of them"
            })
        elif self.replaceAll:
            updated = fileContent.replace(self.oldContent, self.newContent)
            replacements = cntOld
        else:
            updated = fileContent.replace(self.oldContent, self.newContent, 1)
            replacements = 1
        try: 
            with open(self.path, "w", encoding="utf-8") as f: 
                f.write(updated)
            return self.tool_result(result={
                "content": f"Applied {replacements} replacement(s) and successfully updated file at {self.path}"
            })
        except Exception as e: 
            return self.tool_result(error=True, result = {
                "error": f"Error while writing file in {self.path}"
            })

        
class BashTool(AgentTool):
    '''Runs Any bash command to working directory provided'''
    command: str
    cwd: str = "."
    timeouts: int = 30
    async def execute(self, context: AgentContext) -> ToolResult:
        if self.timeouts <= 0: 
            return self.tool_result(error=True, result={
                "error": "Timeouts can't be less or equal to zero"
            })
        if not os.path.isdir(self.cwd):
            return self.tool_result(error=True, result={
                "error": f"Not a valid working directory provided at {self.cwd}"
            })
        try: 
            completion = subprocess.run(self.command, shell=True, timeout=self.timeouts, cwd=self.cwd, check=False, capture_output=True, text=True)
            return self.tool_result(result={
            "content": f'''
                Executed {self.command} successfully 
                <output>
                {completion.stdout} {completion.stderr}
                </output>
            '''
        })
        except subprocess.TimeoutExpired as e: 
            return self.tool_result(error=True, result={
                "error": f"Time expired while running command {self.command}"
            })
        except Exception as e: 
            return self.tool_result(error=True, result={
                "error": f"Error occurred while running command {self.command}"
            })
        
from cron_tools import CronAddTool, CronListTool, CronRemoveTool
from skills_tools import SkillCreateTool, SkillListTool, SkillDeleteTool, SkillInvokeTool
from subagent_tools import SpawnSubagentTool

TOOLS = [ReadTool, WriteTool, BashTool, EditTool, CronAddTool, CronListTool, CronRemoveTool, SkillCreateTool, SkillListTool, SkillDeleteTool, SkillInvokeTool, SpawnSubagentTool]

# Capability packs (Phase 2): discover capabilities/<name>/ and surface ONLY each
# pack's thin entry tool here, so the model can select a capability with no kernel
# edit. Progressive disclosure — the entry tool carries just a 1-line description;
# the heavy role prompts/tools stay scoped inside the pipeline run. This block re-runs
# on every hot-reload of agent_tools, and discover() rebuilds from scratch, so the
# list never accumulates duplicates.
from capabilities.shared.registry import capability_registry
capability_registry.discover()
TOOLS = TOOLS + capability_registry.entry_tools()


        
