"""Operations snapshot of pending script settlement and instruction unit retirement."""

import asyncio
import logging

log = logging.getLogger(__name__)
ALERT_SECONDS = 300


async def snapshot(repo):
    async with asyncio.timeout(3):
        queues = await repo.cleanup_backlog()
    return {
        name: {**values, "alert": values["oldest_seconds"] >= ALERT_SECONDS}
        for name, values in queues.items()
    }


async def watch(stores):
    # A separate read session keeps observation off the lease and cleanup loops.
    active = set()
    unavailable = False
    while True:
        try:
            async with stores() as repo:
                queues = await snapshot(repo)
        except Exception:
            if not unavailable:
                log.warning("workbench_cleanup_observation_unavailable")
            unavailable = True
        else:
            if unavailable:
                log.info("workbench_cleanup_observation_restored")
            unavailable = False
            for name, row in queues.items():
                if row["alert"] and name not in active:
                    active.add(name)
                    log.warning(
                        "workbench_cleanup_overdue queue=%s pending=%s oldest_seconds=%s",
                        name,
                        row["pending"],
                        row["oldest_seconds"],
                    )
                elif not row["alert"] and name in active:
                    active.remove(name)
                    log.info(
                        "workbench_cleanup_recovered queue=%s pending=%s oldest_seconds=%s",
                        name,
                        row["pending"],
                        row["oldest_seconds"],
                    )
        await asyncio.sleep(15)
