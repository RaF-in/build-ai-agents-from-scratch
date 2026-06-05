---
name: create-skill
description: Guide for creating new skills with proper structure and best practices
---

# Creating New Skills

When a user asks you to create a new skill, follow this guide to ensure it's well-structured and effective.

## Understanding Skills

A "skill" is a reusable instruction package that the LLM can invoke via `/skill:<name>`. Each skill lives in a `skills/<skill-name>/SKILL.md` file with YAML frontmatter and markdown body.

## Skill File Structure

Every `SKILL.md` file must follow this format:

```yaml
---
name: skill-name              # kebab-case, e.g., code-review, git-commit
description: One-line summary # concise description for the LLM
---

# Skill Title

Brief description of what this skill does.

## Steps

1. First specific step...
2. Second specific step...

## Rules

- Important rule 1
- Important rule 2

## Examples

Example scenarios (optional).
```

## Step-by-Step Creation Guide

### 1. Name the Skill
- Use **kebab-case** (lowercase with hyphens)
- Be descriptive but concise
- Examples: `git-commit`, `code-review`, `test-runner`

### 2. Write the Description
- One-line summary of what the skill does
- Focus on the outcome, not the process
- Example: "Stage and commit changes with conventional commit messages"

### 3. Structure the Instructions

**Title Section** - Clear heading matching the skill's purpose

**Overview** - Briefly explain:
- What problem does this skill solve?
- When should it be used?
- What tools does it rely on?

**Steps Section** - Sequential, actionable instructions:
- Number each step (1., 2., 3.)
- Be specific about commands to run
- Include validation/checks
- Reference available tools (ReadTool, WriteTool, BashTool, etc.)

**Rules Section** - Constraints and guidelines:
- Use imperative mood ("do this", not "you should")
- Specify edge cases to handle
- List what NOT to do
- Set clear boundaries

**Examples Section** (Optional) - Sample scenarios or outputs

## Best Practices

✅ **Be Specific** - Tell the LLM exactly what to do
   - Bad: "Clean up the code"
   - Good: "Run `black .` then `isort .` to format code"

✅ **Use Available Tools** - Reference tools by name
   - "Use ReadTool to examine the file"
   - "Run `git status` via BashTool"

✅ **Handle Errors** - Specify failure handling
   - "If the file doesn't exist, report an error"
   - "If tests fail, show the output and ask for guidance"

✅ **Set Boundaries** - Define what NOT to do
   - "Never push to remote unless explicitly asked"
   - "Do not modify configuration files"

✅ **Test Your Skill** - Before considering it complete
   - Test with `/skill:<your-name>`
   - Verify it produces expected results
   - Check edge cases

## Common Patterns

### Multi-Step Workflows
```markdown
## Steps

1. Read the configuration file using ReadTool
2. Validate the settings
3. Apply changes using WriteTool
4. Run tests to verify
```

### Conditional Logic
```markdown
## Rules

- If the file exists, validate before modifying
- If tests fail, stop and report the error
- Only proceed if user confirms
```

### Tool Integration
```markdown
## Steps

1. Use BashTool to run `git status`
2. Use ReadTool to examine changed files
3. Use WriteTool to create commit message
```

## Creating the Skill

When ready to create a skill, use the SkillCreateTool with:

- **name**: kebab-case identifier
- **description**: one-line summary
- **instructions**: the full markdown body (following structure above)
- **scope**: "user" (default), "project", or "bundled"

After creation, test it with `/skill:<name>` to verify it works correctly.

## Example: Git Commit Skill

```yaml
---
name: git-commit
description: Stage and commit changes with conventional commit messages
---

# Git Commit

Stage changes and create a conventional commit.

## Steps

1. Run `git status` to see what changed
2. Run `git diff` to understand the changes
3. Stage relevant files with `git add`
4. Commit with conventional format: `type(scope): summary`
5. Run `git log --oneline -1` to confirm

## Rules

- Types: feat, fix, docs, refactor, test, chore
- Write summary in imperative mood ("add" not "added")
- Keep summary under 72 characters
- Never push unless explicitly asked
```
