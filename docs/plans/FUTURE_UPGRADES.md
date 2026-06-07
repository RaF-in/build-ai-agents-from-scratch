# Future Upgrades — Deferred Production Hardening

> Items intentionally **deferred**, not gaps in the current plans. They came out of comparing
> our design to the real implementations of **Claude Code** and **OpenClaw** (June 2026
> research). The current plans ship without these; this file is the backlog for when the
> agent moves toward unattended / production coding use.
>
> Scope note: the third research finding — *"don't over-engineer research; prefer the general
> loop + subagents over a bespoke orchestrator"* — is **deliberately not adopted**. The deep
> research plan's orchestrator approach is being kept as-is by choice.

---

## Upgrade 1 — Checkpoints / undo for file edits

**What it is.** Before any file-mutating tool runs, snapshot the file's current contents so a
change can be reverted. Claude Code does exactly this ("Before Claude edits any file, it
snapshots the current contents" → `Esc Esc` to rewind). It's separate from git and covers
only file changes.

**Why it matters (later).** For a coding agent doing multi-step auto-execute, a wrong edit
should be one action to undo — not a manual git dance. This is a safety/UX feature, not a
correctness one, which is why it's deferrable.

**Where it lands in our code.**
- A small `CheckpointStore` keyed by `(session, sequence)` holding pre-edit file contents.
- Hook into the **pre-tool gate** (already planned): for `WriteTool` / `EditTool`, save the
  prior content before `execute()` runs.
- An `undo` path (slash command `/undo` or a tool) that restores the last N snapshots.
- Snapshots are per-session and ephemeral (cleared on session end), like Claude Code's.

**Keep it simple.** Only snapshot files actually touched; cap retention (e.g. last 50 edits);
store diffs or full small files — don't build a VCS. Reversible file edits only; actions with
external side effects (network, DB) are **not** checkpointable — gate those instead.

**Acceptance.** After an edit, a single `/undo` restores the previous file content; an edit
made after a snapshot can be rolled back without touching git.

---

## Upgrade 2 — Container-based sandboxing for shell/code execution

**What it is.** Run `BashTool` (and any code execution) inside an isolated sandbox — a
container or remote backend — instead of directly on the host. Both reference systems do this:
Claude Code offers cloud VMs; OpenClaw runs non-`main` sessions in **Docker / SSH / OpenShell**
backends (typical policy: allow bash/process/read/write, deny browser/canvas).

**Why it matters (later).** Our current stance ("omit bash for research; confirm risky bash
for coding via the pre-tool gate") is fine for **attended, single-user** use. The moment the
agent runs **unattended**, or is exposed to **untrusted input** (web content, other users),
the host blast radius is unacceptable — real isolation becomes mandatory.

**Where it lands in our code.**
- An execution-backend abstraction behind `BashTool`: `LocalBackend` (today) vs
  `DockerBackend` / `SSHBackend`. Selected per **deployment profile** (the same profile
  concept the e-commerce plan uses).
- Sandbox policy mirrors OpenClaw's tiering: **`main`/trusted session → host** (with the
  pre-tool gate); **untrusted / unattended / customer-facing → container**, no host
  filesystem, no network unless allowlisted, no secrets mounted.
- Reuse the SSRF/egress controls (research plan 1.1) for any network the sandbox is granted.

**Keep it simple.** Start with one `DockerBackend` (mount only the project dir read-write,
drop network by default, non-root user, resource/time limits). Don't build a full orchestrator;
a single container per run is enough.

**Acceptance.** In the sandboxed profile, a destructive command (e.g. touching `/etc`,
escaping the project dir, or reaching the network when disallowed) cannot affect the host;
the attended local profile behaves as today.

---

## Sequencing

Neither blocks the current plans. Suggested order when picked up:
1. **Upgrade 1 (checkpoints)** first — small, high day-to-day value for the coding agent,
   purely additive via the pre-tool gate.
2. **Upgrade 2 (sandboxing)** before any **unattended** or **multi-user / customer-facing**
   deployment — it's the precondition for safely lifting the "no bash / confirm bash" limits.

## References
- How Claude Code works (checkpoints, permission modes): https://code.claude.com/docs/en/how-claude-code-works
- OpenClaw (tiered Docker/SSH sandboxing, workspaces): https://github.com/openclaw/openclaw
