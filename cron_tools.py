from agent_tools import AgentTool, AgentContext, ToolResult


class CronAddTool(AgentTool):
    """Schedule a future message. Use 'in:5m' for one-shot, 'interval:3600' for recurring seconds, or '0 9 * * *' for cron."""
    schedule: str
    message: str
    channel: str
    chat_id: str

    async def execute(self, context: AgentContext) -> ToolResult:
        if not context.cron_service:
            return self.tool_result(error=True, result={"error": "Cron service not available"})
        job = context.cron_service.add(
            schedule=self.schedule,
            message=self.message,
            channel=self.channel,
            chat_id=self.chat_id,
        )
        return self.tool_result(result={
            "content": f"Scheduled job {job.id}: '{job.message}' (next run: {job.next_run})"
        })


class CronListTool(AgentTool):
    """List all scheduled cron jobs."""
    async def execute(self, context: AgentContext) -> ToolResult:
        if not context.cron_service:
            return self.tool_result(error=True, result={"error": "Cron service not available"})
        jobs = context.cron_service.listJobs()
        if not jobs:
            return self.tool_result(result={"content": "No jobs scheduled."})
        lines = []
        for j in jobs:
            status = "enabled" if j.enabled else "disabled"
            lines.append(f"[{j.id[:8]}] {j.schedule!r} -> {j.message!r} ({status}, next: {j.next_run})")
        return self.tool_result(result={"content": "\n".join(lines)})


class CronRemoveTool(AgentTool):
    """Remove a scheduled cron job by ID (supports first 8 chars)."""
    job_id: str

    async def execute(self, context: AgentContext) -> ToolResult:
        if not context.cron_service:
            return self.tool_result(error=True, result={"error": "Cron service not available"})
        job_id = self.job_id
        if len(job_id) < 36:
            match = next((j.id for j in context.cron_service.listJobs() if j.id.startswith(job_id)), None)
            if match:
                job_id = match
        removed = context.cron_service.remove(job_id)
        if removed:
            return self.tool_result(result={"content": f"Removed job {job_id}."})
        return self.tool_result(error=True, result={"error": f"No job found with ID {job_id}."})
