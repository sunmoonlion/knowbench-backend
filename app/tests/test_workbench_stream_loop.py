"""事件流核心循环：数据库是顺序来源，Redis 只唤醒；只写库的事件也能实时到，且不会因为先收到后面的事件而漏掉前面的。"""

from __future__ import annotations

import asyncio
import json

from app.interfaces.endpoints.workbench_routes import stream_session_events


def ev(cursor: int, type_: str = "item/completed") -> dict:
    return {
        "id": f"e{cursor}",
        "cursor": cursor,
        "kind": "item",
        "type": type_,
        "payload": {},
    }


class World:
    def __init__(self) -> None:
        self.db: list[dict] = []
        self.wakeups: asyncio.Queue[None] = asyncio.Queue()
        self.closed = False

    async def fetch_after(self, cursor: int) -> list[dict]:
        return [
            e
            for e in sorted(self.db, key=lambda e: e["cursor"])
            if e["cursor"] > cursor
        ]

    async def wait_wakeup(self, wait_seconds: float) -> bool:
        try:
            await asyncio.wait_for(self.wakeups.get(), wait_seconds)
            return True
        except TimeoutError:
            return False

    async def is_disconnected(self) -> bool:
        return self.closed


def cursors(frames: list[str]) -> list[int]:
    out = []
    for f in frames:
        if f.startswith("id: "):
            out.append(json.loads(f.split("\n")[1].removeprefix("data: "))["cursor"])
    return out


async def collect(world: World, stop_after: int, **kw) -> list[str]:
    frames: list[str] = []
    gen = stream_session_events(
        fetch_after=world.fetch_after,
        wait_wakeup=world.wait_wakeup,
        is_disconnected=world.is_disconnected,
        poll_seconds=0.05,
        **kw,
    )
    async for f in gen:
        frames.append(f)
        if len(cursors(frames)) >= stop_after:
            world.closed = True
    return frames


async def test_unpublished_events_arrive_in_order_without_gaps():
    world = World()
    world.db += [ev(1), ev(2)]

    async def writer():
        await asyncio.sleep(0.02)
        world.db.append(ev(3, "task/state"))  # 只写库、不发 Redis（任务状态）
        world.db.append(ev(4))  # 写库并发 Redis
        await world.wakeups.put(None)
        await asyncio.sleep(0.1)
        world.db.append(ev(5, "step/started"))  # 只写库，靠轮询取到

    task = asyncio.create_task(writer())
    frames = await asyncio.wait_for(collect(world, stop_after=5), 3)
    await task
    assert cursors(frames) == [1, 2, 3, 4, 5]


async def test_resume_after_cursor_and_keepalive_when_idle():
    world = World()
    world.db += [ev(1), ev(2), ev(3)]
    frames: list[str] = []
    gen = stream_session_events(
        fetch_after=world.fetch_after,
        wait_wakeup=world.wait_wakeup,
        is_disconnected=world.is_disconnected,
        after=2,
        poll_seconds=0.02,
        keepalive_seconds=0.05,
    )
    async for f in gen:
        frames.append(f)
        if any(x.startswith(":") for x in frames):
            world.closed = True
    assert cursors(frames) == [3]
    assert ": keepalive\n\n" in frames
