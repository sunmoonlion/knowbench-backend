"""Short-lived ownership of an external operation, recorded under the caller's row lock."""

import asyncio
from datetime import UTC, datetime, timedelta
from uuid import uuid4

from app.domain.workbench.errors import StaleStateVersion


class ExternalClaim:
    def __init__(self, event):
        self.event = event
        self.id = str(uuid4())
        self.deadline = asyncio.get_running_loop().time() + 30

    async def acquire(self, repo, task, revision):
        previous = await repo.latest_task_payload(str(task["id"]), self.event)
        now = datetime.now(UTC)
        if (
            previous
            and previous["revision"] == revision
            and datetime.fromisoformat(previous["until"]) > now
        ):
            return False
        await self.record(repo, task, revision, now + timedelta(seconds=30))
        return True

    async def owns(self, repo, task_id):
        latest = await repo.latest_task_payload(task_id, self.event)
        return latest is not None and latest["id"] == self.id

    async def record(self, repo, task, revision, until):
        await repo.append_event(
            session_id=str(task["session_id"]),
            task_id=str(task["id"]),
            kind="task",
            event_type=self.event,
            payload={"id": self.id, "revision": revision, "until": until.isoformat()},
        )

    async def failed(self, repo, owner, task_id, revision):
        async with repo.transaction():
            task = await repo.get_task(task_id, owner_actor_id=owner, for_update=True)
            if await self.owns(repo, task_id):
                await self.record(repo, task, revision, datetime.now(UTC))

    async def wait(self):
        if asyncio.get_running_loop().time() >= self.deadline:
            raise StaleStateVersion("The approved operation is still in progress")
        await asyncio.sleep(0.1)
