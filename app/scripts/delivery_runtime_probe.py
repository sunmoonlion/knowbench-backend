"""KIND probe using a synthetic handler at the real extension point."""

from __future__ import annotations

import asyncio
import os
import subprocess
import sys
import tempfile
import time
import uuid
from pathlib import Path

from sqlalchemy import text

from app.infrastructure.messaging.durable_tasks import enqueue_task
from app.infrastructure.storage.postgres import get_postgres
from app.worker import celery_app

TOPIC = "gate.increment.v1"


async def handler(session, payload):
    await session.execute(
        text("UPDATE delivery_probe_runs SET value=value+1 WHERE id=:id"),
        {"id": uuid.UUID(payload["probe_id"])},
    )


async def setup(probe_id):
    postgres = get_postgres()
    await postgres.init()
    try:
        async with postgres.session_factory() as session, session.begin():
            await session.execute(
                text(
                    "CREATE TABLE IF NOT EXISTS delivery_probe_runs"
                    "(id uuid PRIMARY KEY,value int NOT NULL,"
                    " replay_verified boolean NOT NULL DEFAULT false)"
                )
            )
            await session.execute(
                text("INSERT INTO delivery_probe_runs(id,value) VALUES (:id,0)"),
                {"id": probe_id},
            )
            return str(
                await enqueue_task(
                    session,
                    topic=TOPIC,
                    key=str(probe_id),
                    payload={"probe_id": str(probe_id)},
                    deduplication_key=f"gate-{probe_id}",
                )
            )
    finally:
        await postgres.shutdown()


@celery_app.task(name="app.tasks.probe.replayed")
def replayed(applied, probe_id):
    if applied is not False:
        raise RuntimeError("duplicate was not acknowledged as already processed")
    asyncio.run(record_replay(uuid.UUID(probe_id)))


async def record_replay(probe_id):
    postgres = get_postgres()
    await postgres.init()
    try:
        async with postgres.session_factory() as session, session.begin():
            await session.execute(
                text(
                    "UPDATE delivery_probe_runs SET replay_verified=true WHERE id=:id"
                ),
                {"id": probe_id},
            )
    finally:
        await postgres.shutdown()


async def verify(probe_id, message_id, replay):
    postgres = get_postgres()
    await postgres.init()
    try:
        async with postgres.session_factory() as session:
            value, replay_verified = (
                await session.execute(
                    text(
                        "SELECT value,replay_verified FROM delivery_probe_runs"
                        " WHERE id=:id"
                    ),
                    {"id": probe_id},
                )
            ).one()
            count = (
                await session.execute(
                    text("SELECT count(*) FROM inbox_message WHERE message_id=:id"),
                    {"id": uuid.UUID(message_id)},
                )
            ).scalar_one()
            return value == 1 and count == 1 and (not replay or replay_verified)
    finally:
        await postgres.shutdown()


def main():
    if os.environ.get("ENV") != "test":
        raise RuntimeError("probe is restricted to disposable test environments")
    if sys.argv[1:2] in (["worker"], ["beat"]):
        from app.tasks import durable_delivery

        durable_delivery.get_delivery_handlers = lambda: {TOPIC: handler}
        from app.worker import configure_celery

        configure_celery(require_broker=True)
        if sys.argv[1] == "worker":
            celery_app.worker_main(
                ["worker", "--pool=solo", "--concurrency=1", "--loglevel=WARNING"]
            )
        else:
            celery_app.start(
                ["beat", "--schedule=" + sys.argv[2], "--loglevel=WARNING"]
            )
        return
    subprocess.run(
        [sys.executable, "-m", "app.bootstrap.migration", "upgrade", "head"], check=True
    )
    probe_id = uuid.uuid4()
    message_id = asyncio.run(setup(probe_id))
    schedule = tempfile.TemporaryDirectory(prefix="delivery-probe-")
    children = [
        subprocess.Popen(
            [sys.executable, __file__, role, str(Path(schedule.name) / "beat")]
        )
        for role in ["worker", "beat"]
    ]
    try:
        from app.tasks.durable_delivery import execute
        from app.worker import configure_celery

        configure_celery(require_broker=True)
        for replay in (False, True):
            if replay:
                execute.apply_async(args=[message_id], link=replayed.s(str(probe_id)))
            deadline = time.monotonic() + 90
            while time.monotonic() < deadline:
                if asyncio.run(verify(probe_id, message_id, replay)):
                    break
                if any(child.poll() is not None for child in children):
                    raise RuntimeError("runtime process exited")
                time.sleep(1)
            else:
                raise RuntimeError(
                    f"delivery acknowledgement timed out (replay={replay})"
                )
        print("KIND_DELIVERY_PROBE_PASSED", flush=True)
    finally:
        for child in children:
            child.terminate()
        for child in children:
            try:
                child.wait(timeout=10)
            except subprocess.TimeoutExpired:
                child.kill()
                child.wait()
        schedule.cleanup()


if __name__ == "__main__":
    main()
