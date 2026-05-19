
from typing import Any, Awaitable

from pydantic import BaseModel
from abc import ABC, abstractmethod
import os
import subprocess
import json

class AgentContext:
    pass

class ToolResult(BaseModel):
    error: bool = False
    result: dict[str, Any]
    name: str
    def to_tool_message(self, id: int) -> dict[str, Any]: 
        return {
            "name": self.name, 
            "role": "tool", 
            "content": json.dumps(self.result),
            "tool_call_id": id
        }
    
class AgentTool(ABC, BaseModel):
    @classmethod
    def tool_name(cls) -> str:
        return cls.__name__
    def tool_result(self, *, error: bool = False, result: dict[str, Any]) -> ToolResult:
        return ToolResult(name=self.__class__.tool_name(), error=error, result=result)
    @classmethod
    def to_schema(cls):
        model_schema = cls.model_json_schema()
        name = cls.tool_name()
        return {
            "type": "function", 
            "function": {
                "name": name,
                "description": model_schema.get("description", f"call the {name} tool"),
                "parameters": {
                    "type": "object", 
                    "properties": model_schema.get("properties", {}),
                    "required": model_schema.get("required", [])
                }
            }
        }
    
    @abstractmethod
    def execute(self, context: AgentContext) -> Awaitable[ToolResult]: 
        """Override this in subclasses to define tool logic."""
        raise NotImplementedError

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
    replaceAll: bool
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
        
TOOLS = [ReadTool, WriteTool, BashTool, EditTool]


        
