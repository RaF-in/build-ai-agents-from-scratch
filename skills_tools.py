"""Tools for self-authoring skills.

The LLM can use these tools to create new skills and list existing ones,
enabling dynamic skill authoring at runtime.

These tools are thin wrappers around SkillManager - the business logic
lives in skills.py to maintain separation of concerns.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

from tool_base import AgentContext, AgentTool, ToolResult
from skills import skill_manager, SkillSource


class SkillCreateTool(AgentTool):
    """Create a new skill that can be invoked with /skill:<name>.

    Args:
        name: The skill name (use kebab-case, e.g., 'code-review')
        description: One-line summary of what the skill does
        instructions: Full markdown instructions the LLM will follow when invoked
        scope: Where to create the skill - 'user' (default, ~/.ai_assistant/skills/),
               'project' (.ai_assistant/skills/), or 'bundled' (repo/skills/)

    Returns:
        Confirmation with the skill path and next steps.
    """

    name: str
    description: str
    instructions: str
    scope: SkillSource = "user"

    async def execute(self, context: AgentContext) -> ToolResult:
        success, message, _ = skill_manager.create(
            name=self.name,
            description=self.description,
            instructions=self.instructions,
            scope=self.scope,
        )

        if success:
            return self.tool_result(result={"content": message})
        else:
            return self.tool_result(error=True, result={"error": message})


class SkillListTool(AgentTool):
    """List all available skills with their names, descriptions, and sources.

    Returns a formatted list showing:
    - Skill name
    - One-line description
    - Source (user/project/bundled)
    - Relative path
    """

    async def execute(self, context: AgentContext) -> ToolResult:
        by_source = skill_manager.list_all()

        if not any(by_source.values()):
            return self.tool_result(result={"content": "No skills found."})

        lines = []
        for source in ["user", "project", "bundled"]:
            source_skills = by_source[source]
            if not source_skills:
                continue
            lines.append(f"## {source.capitalize()} skills")
            for skill in sorted(source_skills, key=lambda s: s.name):
                # Handle paths outside project directory gracefully
                try:
                    rel_path = skill.path.parent.relative_to(Path.cwd())
                except ValueError:
                    # Path is outside project, show absolute path's relevant part
                    if source == "user":
                        rel_path = Path("~/.ai_assistant/skills") / skill.path.parent.name
                    else:
                        rel_path = skill.path.parent
                lines.append(f"- {skill.name}: {skill.description}")
                lines.append(f"  Location: {rel_path}")
            lines.append("")

        return self.tool_result(result={"content": "\n".join(lines).strip()})


class SkillDeleteTool(AgentTool):
    """Delete an existing skill by name.

    Args:
        name: The skill name to delete (e.g., 'code-review')

    Returns:
        Confirmation of deletion or error if not found.
    """

    name: str

    async def execute(self, context: AgentContext) -> ToolResult:
        success, message = skill_manager.delete(self.name)

        if success:
            return self.tool_result(result={"content": message})
        else:
            return self.tool_result(error=True, result={"error": message})


class SkillInvokeTool(AgentTool):
    """Invoke a skill by name.

    Use this when you determine that following a specific skill's instructions
    would help complete the user's request.

    Args:
        name: The skill name to invoke (e.g., 'git-commit', 'code-review')
        args: Optional additional context or arguments for the skill execution

    Returns:
        The skill's instructions. You should follow these instructions step-by-step
        to complete the user's request.
    """

    name: str
    args: str = ""

    async def execute(self, context: AgentContext) -> ToolResult:
        skill = skill_manager.get(self.name)
        if not skill:
            available = ", ".join(skill_manager.names()) or "(none)"
            return self.tool_result(
                error=True,
                result={
                    "error": f"Skill {self.name!r} not found. Available skills: {available}"
                },
            )

        instructions = skill.instructions.strip()
        if self.args.strip():
            instructions = f"{self.args.strip()}\n\n{instructions}"

        # Return as normal tool result - the LLM will read and follow the instructions
        return self.tool_result(
            result={
                "content": f"Skill '{skill.name}' loaded. Follow these instructions:\n\n{instructions}"
            }
        )
