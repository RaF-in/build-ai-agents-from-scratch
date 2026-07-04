from typing import Protocol, Literal, Any, TYPE_CHECKING
import asyncio
import re

if TYPE_CHECKING:
    from agent import AgentRuntime


# A command-shaped first token: a single "/word" with no embedded slashes or spaces
# (so "/clear" matches but a real path like "/etc/hosts" or a sentence starting with
# "/" does NOT). Used to reject mistyped commands without swallowing genuine messages
# that happen to start with a slash.
_COMMAND_TOKEN = re.compile(r"^/[a-zA-Z][\w-]*$")

# Single source of truth for the built-in commands, shown by /help and in the
# unknown-command hint. /skill:<name> is dynamic and listed separately.
_COMMANDS = [
    ("/new (or /clear)", "clear the session and start fresh"),
    ("/think <off|on|stream>", "set extended-thinking mode"),
    ("/model", "show the current model and session info"),
    ("/status", "show full session status"),
    ("/agents", "show subagent run history"),
    ("/skill:<name> [args]", "run a skill as an LLM turn"),
    ("/help", "show this command list"),
    ("/quit", "end the session"),
]


def _help_text() -> str:
    lines = ["Available commands:"]
    lines += [f"  {name}  —  {desc}" for name, desc in _COMMANDS]
    return "\n".join(lines)


# =====================================================================
# Output Protocol — any interface implements this
# =====================================================================

class OutputHandler(Protocol):
    """Abstract output interface — CLI, Telegram, Discord, etc."""

    async def send(self, message: str) -> None:
        """Send a plain-text message to the user."""
        ...

    async def send_rich(self, message: str, format_type: str = "text") -> None:
        """Send a formatted/rich message (status panels, embeds …)."""
        ...

    async def confirm(self, message: str) -> bool:
        """Ask the user for confirmation (y/n)."""
        ...

    def supports_interactive(self) -> bool:
        """Whether this interface can handle interactive prompts."""
        ...


# =====================================================================
# CLI Output Handler
# =====================================================================

class CLIOutputHandler:
    def __init__(self):
        from rich.console import Console
        self.console = Console()

    async def send(self, message: str) -> None:
        print(message)

    async def send_rich(self, message: str, format_type: str = "text") -> None:
        if format_type == "status":
            self.console.print(f"[bold blue]{message}[/bold blue]")
        else:
            self.console.print(message)

    async def confirm(self, message: str) -> bool:
        loop = asyncio.get_event_loop()
        response = await loop.run_in_executor(None, input, f"{message} (y/n): ")
        return response.lower() in {"y", "yes"}

    def supports_interactive(self) -> bool:
        return True


# =====================================================================
# Telegram Output Handler
# =====================================================================

class TelegramOutputHandler:
    def __init__(self, context: Any):
        self.context = context

    async def send(self, message: str) -> None:
        await self.context.send_message(message)

    async def send_rich(self, message: str, format_type: str = "text") -> None:
        formatted = self._format_for_telegram(message, format_type)
        await self.context.send_message(formatted)

    async def confirm(self, message: str) -> bool:
        # Telegram can't do interactive prompts — auto-confirm
        await self.send(f"{message} ✓")
        return True

    def supports_interactive(self) -> bool:
        return False

    def _format_for_telegram(self, message: str, format_type: str) -> str:
        if format_type == "status":
            return f"\U0001f4ca *Session Status*\n\n{message}"
        return message


# =====================================================================
# Slash Command Handler (interface-agnostic business logic)
# =====================================================================

class SlashCommandHandler:
    """Interface-agnostic slash command handler."""

    def __init__(self, runtime: "AgentRuntime", output: OutputHandler):
        self.runtime = runtime
        self.output = output

    # ---- /new --------------------------------------------------------

    async def handle_new(self) -> bool:
        """Start a fresh session."""
        if self.output.supports_interactive():
            confirmed = await self.output.confirm("Clear current session?")
            if not confirmed:
                await self.output.send("Cancelled.")
                return True

        await self.runtime.session_manager.delete()
        from agent import render_history_event
        await self.runtime.initialize(replay_handler=render_history_event)
        await self.output.send("✅ Session cleared. Starting fresh.")
        return True

    # ---- /think ------------------------------------------------------

    async def handle_think(self, mode: str) -> bool:
        """Control extended thinking."""
        if mode not in ("off", "on", "stream"):
            await self.output.send(
                f"❌ Invalid mode: {mode!r}. Use: off, on, stream"
            )
            return True
        self.runtime.thinking_mode = mode
        await self.output.send(f"\U0001f9e0 Thinking mode: {mode}")
        return True

    # ---- /model ------------------------------------------------------

    async def handle_model(self) -> bool:
        """Show current model info."""
        msg_count = await self._get_message_count()
        info = (
            f"\U0001f916 Model: {self.runtime.model_name}\n"
            f"\U0001f4ac Messages: {msg_count}\n"
            f"\U0001f4c1 Session: {self.runtime.session_manager.basedir}"
        )
        await self.output.send(info)
        return True

    # ---- /status -----------------------------------------------------

    async def handle_status(self) -> bool:
        """Display full session status."""
        msg_count = await self._get_message_count()
        context_pct = self._get_context_percentage()

        status = (
            f"Model: {self.runtime.model_name}\n"
            f"Messages: {msg_count}\n"
            f"Context: {context_pct}%\n"
            f"Tools: {len(self.runtime.tools)}\n"
            f"Thinking: {getattr(self.runtime, 'thinking_mode', 'off')}"
        )
        await self.output.send_rich(status, format_type="status")
        return True

    # ---- /agents -----------------------------------------------------

    async def handle_agents(self) -> bool:
        """Display subagent run history and active runs."""
        from subagent import subagent_registry
        active = list(subagent_registry.active.values())
        history = subagent_registry.history[-10:]

        lines = [f"Subagents — {len(active)}/{subagent_registry.max_concurrent} active"]
        if active:
            lines.append("\nRunning:")
            for r in active:
                lines.append(f"  [{r.id}] {r.duration:.0f}s  {r.task[:60]}")
        if history:
            lines.append("\nRecent:")
            icon = {"completed": "✓", "failed": "✗", "expired": "⏱", "rejected": "⊘"}
            for r in reversed(history):
                mark = icon.get(r.status, "?")
                suffix = f" (attempt {r.attempts})" if r.attempts > 1 else ""
                lines.append(f"  {mark} [{r.id}] {r.duration:.0f}s  {r.task[:50]}{suffix}")
                if r.result_preview:
                    lines.append(f"      → {r.result_preview}")
        if not active and not history:
            lines.append("\nNo subagents spawned yet.")
        await self.output.send_rich("\n".join(lines), format_type="status")
        return True

    # ---- /help -------------------------------------------------------

    async def handle_help(self) -> bool:
        """List the available slash commands."""
        await self.output.send(_help_text())
        return True

    # ---- /quit -------------------------------------------------------

    async def handle_quit(self) -> str:
        """Exit gracefully."""
        if self.output.supports_interactive():
            await self.output.send("\U0001f44b Goodbye!")
        else:
            await self.output.send(
                "\U0001f44b Session ended (type /quit again to fully exit)"
            )
        return "QUIT"

    # ---- helpers -----------------------------------------------------

    async def _get_message_count(self) -> int:
        conn = await self.runtime.session_manager.db()
        cursor = await conn.execute("SELECT COUNT(*) FROM messages")
        row = await cursor.fetchone()
        await cursor.close()
        return row[0] if row else 0

    def _get_context_percentage(self) -> int:
        max_events = 40
        # Approximate from the SessionManager.should_summarize logic.
        # We don't have the live event list here, so we estimate from
        # the total message count in the DB.  This is good enough for a
        # status display — the exact number is recalculated every time
        # load_messages() runs.
        return 0  # Will be enriched later if needed


# =====================================================================
# Facade — single entry point for all interfaces
# =====================================================================

async def _expand_skill_command(
    text: str,
    output: OutputHandler,
) -> bool | tuple[str, str]:
    """Expand a `/skill:<name> [args]` invocation into a full LLM turn.

    Returns ("RUN", expanded_text) so the caller feeds it to the LLM, or
    True when the skill is unknown (error already sent, skip the LLM).
    """
    from skills import skill_manager

    skill_manager.discover()  # always read the latest skills from disk

    body = text[len("/skill:"):].strip()
    name, _, args = body.partition(" ")
    name = name.strip()

    skill = skill_manager.get(name)
    if skill is None:
        available = ", ".join(skill_manager.names()) or "(none)"
        await output.send(
            f"❌ Unknown skill: {name!r}. Available skills: {available}"
        )
        return True

    expanded = (
        f"You are now executing the '{skill.name}' skill. "
        f"Follow these instructions exactly:\n\n"
        f"{skill.instructions}"
    )
    if args.strip():
        expanded += f"\n\n## User request\n{args.strip()}"
    return ("RUN", expanded)


async def parse_and_execute(
    text: str,
    runtime: "AgentRuntime",
    output: OutputHandler,
) -> bool | str | tuple[str, str]:
    """
    Parse and execute a slash command.

    Returns
    -------
    True             — command was handled, skip the LLM call
    "QUIT"           — caller should break out of its main loop
    ("RUN", text)    — run the given text through the LLM as a normal turn
    False            — not a slash command, continue to normal LLM flow
    """
    if not text.startswith("/"):
        return False

    handler = SlashCommandHandler(runtime, output)
    parts = text.strip().split()
    command = parts[0].lower()

    if command.startswith("/skill:"):
        return await _expand_skill_command(text.strip(), output)

    if command in ("/new", "/clear"):
        return await handler.handle_new()

    elif command == "/help":
        return await handler.handle_help()

    elif command == "/think":
        mode = parts[1].lower() if len(parts) > 1 else "on"
        return await handler.handle_think(mode)

    elif command == "/model":
        return await handler.handle_model()

    elif command == "/status":
        return await handler.handle_status()

    elif command == "/agents":
        return await handler.handle_agents()

    elif command == "/quit":
        return await handler.handle_quit()

    # A command-shaped but unrecognized token (e.g. "/reset", "/compact", a typo) is a
    # mistyped command, NOT a message — reject it with a hint and skip the LLM so we
    # don't waste a model round-trip interpreting it. Anything that only *starts* with
    # "/" but isn't command-shaped (a path like "/etc/hosts", a sentence) falls through
    # to the LLM as a normal message, unchanged.
    if _COMMAND_TOKEN.match(command):
        await output.send(f"❌ Unknown command: {command}\n{_help_text()}")
        return True

    return False
