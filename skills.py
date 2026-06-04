"""Skill discovery and catalog.

A "skill" is a directory containing a SKILL.md file with YAML frontmatter
(name + description) followed by a markdown instruction body, e.g.:

    skills/git-commit/SKILL.md

        ---
        name: git-commit
        description: Stage and commit changes with a conventional commit message
        ---

        # full step-by-step instructions the model follows when invoked...

Skills are discovered from three locations, in precedence order
(first-match-wins on duplicate names):

    1. user     ~/.ai_assistant/skills/
    2. project  {cwd}/.ai_assistant/skills/
    3. bundled  {repo}/skills/
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Literal
import os

import yaml
from rich import print


SkillSource = Literal["user", "project", "bundled"]

USER_SKILLS_DIR = Path.home() / ".ai_assistant" / "skills"
PROJECT_SKILLS_DIR = Path.cwd() / ".ai_assistant" / "skills"
BUNDLED_SKILLS_DIR = Path(__file__).resolve().parent / "skills"


@dataclass
class Skill:
    name: str
    description: str
    instructions: str
    path: Path
    source: SkillSource


def parse_skill_file(path: Path, source: SkillSource) -> Skill | None:
    """Parse a SKILL.md file. Returns None (and logs) on any problem so a
    single malformed skill never crashes discovery/startup."""
    try:
        raw = path.read_text(encoding="utf-8")
    except Exception as exc:
        print(f"[yellow][skills] could not read {path}: {exc}[/yellow]")
        return None

    frontmatter, body = _split_frontmatter(raw)
    if frontmatter is None:
        print(f"[yellow][skills] missing frontmatter in {path}, skipping[/yellow]")
        return None

    try:
        meta = yaml.safe_load(frontmatter) or {}
    except yaml.YAMLError as exc:
        print(f"[yellow][skills] bad YAML in {path}: {exc}[/yellow]")
        return None

    name = meta.get("name")
    description = meta.get("description")
    if not name or not description:
        print(f"[yellow][skills] {path} needs both 'name' and 'description', skipping[/yellow]")
        return None

    return Skill(
        name=str(name).strip(),
        description=str(description).strip(),
        instructions=body.strip(),
        path=path,
        source=source,
    )


def _split_frontmatter(raw: str) -> tuple[str | None, str]:
    """Split a `---`-delimited YAML frontmatter block from the body."""
    text = raw.lstrip()
    if not text.startswith("---"):
        return None, raw
    # Drop the opening fence, then split on the closing fence.
    after = text[3:]
    end = after.find("\n---")
    if end == -1:
        return None, raw
    frontmatter = after[:end]
    body = after[end + 4:]
    return frontmatter, body


class SkillManager:
    def __init__(self, search_dirs: list[tuple[Path, SkillSource]] | None = None):
        self.search_dirs: list[tuple[Path, SkillSource]] = search_dirs or [
            (USER_SKILLS_DIR, "user"),
            (PROJECT_SKILLS_DIR, "project"),
            (BUNDLED_SKILLS_DIR, "bundled"),
        ]
        self.skills: dict[str, Skill] = {}

    def discover(self) -> dict[str, Skill]:
        """Scan every search dir for */SKILL.md, dedupe by name
        (first-match-wins following search_dirs order)."""
        found: dict[str, Skill] = {}
        for base, source in self.search_dirs:
            if not base.is_dir():
                continue
            for skill_md in sorted(base.glob("*/SKILL.md")):
                skill = parse_skill_file(skill_md, source)
                if skill is None:
                    continue
                if skill.name in found:
                    continue  # first match wins
                found[skill.name] = skill
        self.skills = found
        return self.skills

    def get(self, name: str) -> Skill | None:
        return self.skills.get(name)

    def names(self) -> list[str]:
        return list(self.skills.keys())

    def format_for_prompt(self) -> str:
        """XML catalog with names + descriptions only (keeps the prompt small)."""
        if not self.skills:
            return ""
        lines = ["<skills>"]
        for skill in self.skills.values():
            desc = skill.description.replace("\n", " ").strip()
            lines.append(f'  <skill name="{skill.name}">{desc}</skill>')
        lines.append("</skills>")
        return "\n".join(lines)


# Module-level singleton shared by the CLI loop, the Telegram server and tools.
skill_manager = SkillManager()
