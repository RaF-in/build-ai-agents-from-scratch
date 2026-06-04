---
name: git-commit
description: Stage and commit the current changes with a well-formed conventional commit message
---

# Git Commit

Stage the user's changes and create a single, well-formed commit using the
Conventional Commits style.

## Steps

1. Run `git status` to see what has changed.
2. Run `git diff` (and `git diff --staged` if anything is already staged) to
   understand the actual changes.
3. Decide the correct conventional commit type based on the diff:
   - `feat` — a new feature
   - `fix` — a bug fix
   - `docs` — documentation only
   - `refactor` — code change that neither fixes a bug nor adds a feature
   - `test` — adding or fixing tests
   - `chore` — build process, tooling, or dependency changes
4. Stage the relevant files with `git add` (use `git add -A` only if the user
   clearly wants everything).
5. Commit with a concise message in the form:
   `type(scope): short imperative summary`
   For example: `feat(auth): add OAuth2 login flow`
6. Run `git log --oneline -1` to confirm the commit landed.

## Rules

- Write the summary in the imperative mood ("add", not "added").
- Keep the summary under ~72 characters.
- Never push unless the user explicitly asks.
- If there are no changes to commit, say so and stop.
