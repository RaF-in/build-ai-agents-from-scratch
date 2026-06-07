# Coding Agent & Plan Mode — Production Implementation Plan

> **Guiding principle:** Coding is **not** a spawned capability pack like research or
> e-commerce. Your **main agent already *is* a coding agent** — its `ReadTool`, `WriteTool`,
> `EditTool`, `BashTool`, `SpawnSubagentTool`, skills, and sessions are the Claude-Code
> toolset. So we **enhance the main agent**, and **reuse the generic sockets** already in
> the other plans (`RunPolicy`, pre-tool gate, subagents, budgets) — not build a new
> subsystem. Coding stays in the main loop because the user reviews every change and it
> mutates the real repo (shared, mutable state).

---

## 0. Architecture at a glance

```
Main agent (the coding agent — same single runtime)
  │
  ├─ Tools: Read, Write, Edit, Bash  + NEW: Grep, Glob, (symbol search)
  │
  ├─ DefaultPolicy (resting)
  │     • simple task  → answer/edit directly, no todos
  │     • complex task → agent OPTS IN to a todo list → auto plan+execute (NO approval gate)
  │
  ├─ /plan  (USER toggle, slash command)
  │     → InteractivePlanPolicy: plan → STOP & wait for approval → execute → revert
  │
  ├─ Pre-tool gate (reused): approve risky bash / writes outside workspace
  │
  └─ Subagents (reused): parallel "explore the codebase" workers (depth budget)
```

**Three behaviors, two policies** (the key model):

| Task | Policy | Todos? | Approval gate? | Decided by |
|---|---|---|---|---|
| Simple ("what does X do?") | `DefaultPolicy` | no | no | agent |
| Complex ("refactor auth") | `DefaultPolicy` | yes (agent opts in) | **no — auto-execute** | **agent** |
| User wants to review first | `InteractivePlanPolicy` | yes | **yes — stop & approve** | **user** (`/plan`) |

**Where logic lives:** raw actions = tools; *how to code well* = `coding/SKILL.md`; *plan/approve flow* = the policies. Never bury coding judgment in `execute()`.

---

## Phase 0 — Kernel reuse (almost nothing new)

> Coding introduces **no new kernel concepts**. It reuses sockets defined in the research &
> e-commerce plans. The only genuinely new kernel line is making the policy swappable.

### 0.1 — Swappable policy + `set_policy` (the one small addition)
- **Why:** the main agent is built once with `DefaultPolicy`; `/plan` must flip it at runtime.
- **Tasks:**
  - Keep `self.default_policy` and `self.policy` on `AgentRuntime`; add `_set_policy(p)` that
    sets `self.policy = p`.
  - Expose it to tools/commands via `context.set_policy` and `context.default_policy`
    callbacks wired in `__init__`.
  - The kernel already reads `self.policy` fresh each turn (`get_tools()`, idle branch), so a
    swap takes effect next turn — no other kernel change.
- **Acceptance:** `/plan` can switch policy mid-session and revert; default behavior unchanged otherwise.

### 0.2 — Reused sockets (already specified elsewhere)
- **`RunPolicy`** + completion gate — from the research plan (0.6).
- **Pre-tool approval gate** — from the e-commerce plan (0.3): allow / deny / require-confirmation *before* a tool runs.
- **Subagents + depth budget** — from the research plan (0.1) for parallel codebase exploration.
- **Per-turn tool-call budget** — already exists (`max_tool_calls_per_turn = 10`, `agent.py:64`).
- **Provider retry + model fallback** — from both plans (litellm `fallbacks=[...]`).
- **Acceptance:** no duplicate machinery; coding wires into existing sockets.

---

## Phase 1 — Code navigation tools (the biggest real gap)

> Today the agent can only `Read` by exact path. Claude Code lives on **search**. Without
> this the agent can't find anything and blows context reading whole files.

### 1.1 — `GrepTool` (content search)
- Wrap `ripgrep` (`rg`) if present, else Python fallback. Args: `pattern`, optional `path`,
  `glob`, `-i`. Return file:line matches, **capped** (e.g. 100 matches) to protect context.
### 1.2 — `GlobTool` (file-name search)
- Find files by pattern (`**/*.py`, `src/**/test_*.py`). Sort by mtime; cap results.
### 1.3 — (Optional) symbol / codebase search
- A "find definition/usages" tool (ctags/LSP or embeddings) — add later; `Grep` covers 80%.
### 1.4 — Read with ranges
- Extend `ReadTool` to accept optional `offset`/`limit` so the agent reads slices, not 5k-line files.
- **Acceptance:** the agent locates code by content/name and reads only the relevant slice;
  register these in the main agent's `TOOLS`.

---

## Phase 2 — Coding playbook (`coding/SKILL.md`)

### 2.1 — Behavior rules (markdown, no code)
- **Search before read**; read slices, not whole files.
- **Minimal, surgical edits**; match existing style/conventions.
- **Verify after editing** (run tests/build/lint; read errors; fix).
- **Don't commit/push unless asked**; never invent APIs — check the codebase first.
- **For complex tasks, make a todo list**; for simple ones, just answer.
### 2.2 — Discovery
- Drop `coding/SKILL.md`; the existing `skill_manager.discover()` loads it (name+description in
  prompt, body on invoke). No code.
- **Acceptance:** the skill is listed; invoking it injects the playbook.

---

## Phase 3 — Auto plan + execute (agent-decided, no gate)

> The default path for complex tasks: the agent **opts into** a todo list and runs it to
> completion autonomously — **no approval stop**. This is Claude Code's `TodoWrite`.

### 3.1 — Optional todo tool
- A `TodoWriteTool` (add/update/complete) available in `DefaultPolicy`. The agent uses it only
  for multi-step work; simple tasks never call it.
### 3.2 — Completion gate (no-op when empty)
- `DefaultPolicy.on_idle`: if `todos` exist and are unfinished → re-inject "finish your todos"
  and continue; if none → finish normally. **No approval gate.**
### 3.3 — Loop safety (gap, simple)
- `max_continuations` cap + repeated-identical-tool-call detector → abort with a partial
  summary (no infinite loops). (Same mechanism as the research plan.)
- **Acceptance:** a complex task auto-plans and finishes all todos; a simple task creates none;
  a stuck loop aborts cleanly.

---

## Phase 4 — Interactive plan mode (user-toggled, with approval)

> Separate from auto-execute. Activated **only by the user**, never by the LLM.

### 4.1 — `/plan` slash command (the toggle)
- In `slash_commands.py`, `/plan` swaps `runtime.policy` between `default_policy` and
  `InteractivePlanPolicy()` (and prints the state). Returns "handled" so it skips the LLM.
### 4.2 — `InteractivePlanPolicy`
- Plan phase: agent researches + proposes a plan, then **ends the turn and waits**
  (`awaiting_approval = True`; `on_idle` returns `False` so control returns to the user).
- Approval: the user's next "approve/go ahead" flips to execute; edits keep it in plan mode.
- On completion: revert to `default_policy`.
### 4.3 — Read-only safety in plan phase
- While planning, restrict to read/search tools (no Write/Edit/Bash-mutations) via
  `active_tools` — so "plan mode" genuinely cannot change the repo before approval.
- **Acceptance:** `/plan` → agent plans and stops; nothing is modified until the user approves;
  `/plan` again returns to auto-execute.

---

## Phase 5 — Guardrails (simple, production-grade)

> All deliberately lightweight; most reuse the pre-tool gate (0.2).

### 5.1 — Bash safety
- **Pre-tool gate** requires confirmation for destructive/irreversible commands (a small
  denylist: `rm -rf`, `git push --force`, `:(){ }`, disk/format, `sudo`).
- Keep the existing **timeout** and run with `cwd` confined to the project; cap output size.
### 5.2 — Write/edit safety
- Confine writes to the project root by default; **require confirmation** to write/edit outside it.
- `EditTool` already checks uniqueness (`agent_tools.py:70-84`) — keep it; prefer edit over full rewrite.
### 5.3 — Secret hygiene
- Refuse to read obvious secret files (`.env`, `*.pem`, `id_rsa`, credentials) unless explicitly
  asked; **redact** secret-looking strings in tool output (`on_tool_result` hook).
### 5.4 — Git safety
- Never commit/push unless asked; if on the default branch, create a branch first; never
  `--force` without confirmation.
### 5.5 — Untrusted content
- If the agent fetches docs/web during coding, treat that text as **data, not instructions**
  (same delimiter+preamble as the research plan).
- **Acceptance:** destructive commands and out-of-workspace writes are gated; secrets aren't
  leaked; git actions are safe by default.

---

## Phase 6 — Budgeting & context management

### 6.1 — Per-turn / per-task budget
- Reuse the existing per-turn tool-call cap (`max_tool_calls_per_turn`); add a soft per-task
  budget (max tool calls / tokens) for long auto-execute runs → graceful "summarize progress" wrap-up.
### 6.2 — Context discipline (the real cost driver)
- **Search before read; read slices, not whole files** (Phase 1.4) — the #1 way to keep
  context (and cost) bounded on large repos.
- **Offload exploration to subagents:** "find everywhere X is used / how Y works" runs in a
  silent subagent that returns a summary, keeping the main context clean (reuses depth budget).
- Trigger the existing `CompactionEvent` path when the main context grows large.
### 6.3 — Provider resilience
- Retry + backoff on `429`/`5xx`; model fallback list (litellm) — a long coding session must
  not die on a rate limit.
- **Acceptance:** a long task stays within budget (or wraps up gracefully); big-repo work
  doesn't explode context; provider rate limits are retried, not fatal.

---

## Phase 7 — Verification loop

### 7.1 — Run-and-fix
- After edits, the playbook drives: run the project's tests/build/lint via `BashTool`, read
  failures, fix, repeat (bounded by the task budget).
### 7.2 — Don't claim success blindly
- Report what was run and the actual result; if tests fail, say so with output (no false "done").
- **Acceptance:** edits are followed by a real verification step; outcomes are reported truthfully.

---

## Phase 8 — Observability & testing

### 8.1 — Observability
- Per-task log: tools used, files changed, commands run, tokens/cost, duration; trace into
  exploration subagents.
### 8.2 — Unit tests
- `Grep`/`Glob` correctness + caps; policy swap via `/plan`; completion gate no-op when no
  todos; `max_continuations` abort; bash denylist + out-of-workspace write gating; secret redaction.
### 8.3 — Integration tests
- A fixed repo fixture: search → edit → run tests → fix loop; plan-mode stop-and-approve flow.
### 8.4 — Rollout
- Ship nav tools + skill first (low risk); enable auto plan+execute; enable `/plan`; turn on
  bash/write gating before any unattended use. Behind a flag; kill switch.
- **Acceptance:** green unit + integration; a real change is made, verified, and reported.

---

## Definition of Done
- [ ] Coding lives in the **main agent** — no spawned "coding capability"; subagents used only for exploration.
- [ ] **Only new kernel line:** swappable `self.policy` + `set_policy`; everything else reuses existing sockets.
- [ ] Navigation tools (`Grep`/`Glob`/ranged `Read`) registered; `coding/SKILL.md` discovered.
- [ ] **Three behaviors, two policies:** simple (no todos), complex (auto todos, no gate),
      `/plan` (user-toggled, approval gate). LLM never self-enters interactive plan mode.
- [ ] **Guardrails:** bash denylist + confirm; writes confined to workspace; secret redaction;
      git-safe defaults; untrusted content quarantined.
- [ ] **Budgeted & resilient:** per-task budget with graceful wrap-up; search-before-read +
      subagent offload for context; provider retry + fallback; `max_continuations` loop cap.
- [ ] Edits are **verified** (tests/build run) and outcomes reported truthfully.
