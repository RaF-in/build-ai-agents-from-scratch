import asyncio
from dataclasses import dataclass, asdict
from datetime import datetime, timezone
import os
import json
from uuid import uuid4

CRON_PATH = os.path.expanduser("~/.ai_assistant/cron.json")

@dataclass
class CronJob:
    id: str
    chat_id: str
    schedule: str
    message: str
    channel: str
    next_run: str | None = None
    enabled: bool = True

def _calculate_next_run(schedule: str, now: datetime) -> datetime: 
    unit_map = {
        's': 1, 
        'm': 60, 
        'h': 3600
    }
    if schedule.startswith("in:"):
        spec = int(schedule[3:-1])
        sec = spec * unit_map.get(schedule[-1], -1)
        return datetime.fromtimestamp(now.timestamp() + sec, tz=timezone.utc)
    elif schedule.startswith("interval:"):
        sec = int(schedule.split(":")[1])
        return datetime.fromtimestamp(now.timestamp() + sec, tz=timezone.utc)
    from croniter import croniter
    return croniter(schedule, now).get_next(datetime).replace(tzinfo=timezone.utc)

class CronService: 
    def __init__(self, queue: asyncio.Queue, cron_path: str = CRON_PATH):
        self.queue = queue
        self.path = cron_path
        self._jobs: list[CronJob] = []
        self._load()
    def _load(self):
        if not os.path.exists(self.path):
            return
        now = datetime.now(timezone.utc)
        with open(self.path) as f:
            jobs = json.load(f)
        for job in jobs:
            loaded_job = CronJob(**job)
            if loaded_job.enabled:
                loaded_job.next_run = _calculate_next_run(loaded_job.schedule, now).isoformat()
            self._jobs.append(loaded_job)
        self.save()
    def save(self):
        os.makedirs(os.path.dirname(self.path), exist_ok=True)
        with open(self.path, "w", encoding="utf-8") as f:
            json.dump([asdict(j) for j in self._jobs], f, indent=2)
    def add(self, *, schedule: str, message: str, chat_id: str, channel: str) -> CronJob:
        id = str(uuid4())
        now = datetime.now(timezone.utc)
        job = CronJob(
            id=id, 
            chat_id=chat_id,
            message=message,
            channel=channel,
            schedule=schedule,
            enabled=True
        )
        job.next_run = _calculate_next_run(job.schedule, now).isoformat()
        self._jobs.append(job)
        self.save()
        return job
    def remove(self, job_id: str) -> bool:
        before = len(self._jobs)
        self._jobs = [job for job in self._jobs if job.id != job_id]
        if len(self._jobs) < before:
            self.save()
            return True
        return False
    def listJobs(self) -> list[CronJob]:
        return list(self._jobs)
    async def run(self):
        while True: 
            for job in self._jobs:
                if not job.enabled or not job.next_run:
                    continue
                now = datetime.now(timezone.utc)
                if datetime.fromisoformat(job.next_run).replace(tzinfo=timezone.utc) <= now:
                    await self.queue.put({
                        "text": job.message,
                        "channel": job.channel,
                        "sender_id": "cron", 
                        "chat_id": job.chat_id
                    })
                    if job.schedule.startswith("in:"):
                        job.enabled = False
                    else:
                        job.next_run = _calculate_next_run(job.schedule, now).isoformat()
                    self.save()
            await asyncio.sleep(10)



