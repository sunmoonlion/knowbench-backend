"""Real prefork execution/Inbox evidence with a test-only storage/handler assembly."""

from __future__ import annotations

import asyncio
import os
import signal
import subprocess
import sys
import time
from pathlib import Path
from urllib.parse import urlparse
from uuid import uuid4

import pytest
from celery import Celery
from kombu import Connection
from test_delivery_progress import progress
from test_durable_delivery_db import db as db
from test_durable_delivery_db import request, sql

from app.infrastructure.messaging.scheduler_activity import process_identity

ROOT = Path(__file__).resolve().parents[1]


async def test_prefork_pause_pong_receipts_recovery_duplicate_and_rollback(
    db, tmp_path, celery_probe_config
):
    broker = os.environ.get("CELERY_PROBE_TEST_BROKER_URL")
    if not broker:
        pytest.skip("requires disposable CELERY_PROBE_TEST_BROKER_URL")
    parsed = urlparse(broker)
    assert parsed.scheme == "amqp" and parsed.hostname in {"127.0.0.1", "localhost"}
    assert parsed.path == "/luna_probe_tests"
    schema = await sql(db, "SELECT current_schema()")
    suffix = uuid4().hex
    queue = "progress." + suffix
    node = "celery@progress-" + suffix
    env = {
        **os.environ,
        "CELERY_BROKER_URL": broker,
        "CELERY_QUEUE": queue,
        "CELERY_RESULT_BACKEND": "",
        "DELIVERY_PROGRESS_TEST_SCHEMA": schema,
        "PYTHONPATH": os.pathsep.join(
            (str(ROOT / "tests"), str(ROOT), os.environ.get("PYTHONPATH", ""))
        ),
    }
    client = Celery("progress-" + suffix, broker=broker)
    log_path = tmp_path / "worker.log"
    log = log_path.open("w")
    worker = await asyncio.to_thread(
        subprocess.Popen,
        [
            sys.executable,
            "-m",
            "celery",
            "-A",
            "worker_progress_fixture:celery_app",
            "worker",
            "--pool=prefork",
            "--concurrency=1",
            "--hostname=" + node,
            "--without-gossip",
            "--without-mingle",
            "--without-heartbeat",
            "--loglevel=WARNING",
        ],
        cwd=ROOT,
        env=env,
        stdin=subprocess.DEVNULL,
        stdout=log,
        stderr=log,
        start_new_session=True,
    )
    paused = None

    def diagnostic():
        return log_path.read_text(errors="replace")[-4000:].replace(
            parsed.password or "synthetic-password", "<redacted>"
        )

    async def inspect(command):
        inspector = client.control.inspect(destination=[node], timeout=1)
        return await asyncio.to_thread(getattr(inspector, command))

    async def send(identifier):
        return await asyncio.to_thread(
            client.send_task,
            "app.tasks.durable_delivery.execute",
            args=[str(identifier)],
            queue=queue,
            exchange=queue,
            routing_key=queue,
        )

    async def wait_receipts(count):
        deadline = time.monotonic() + 15
        while time.monotonic() < deadline:
            assert worker.poll() is None, diagnostic()
            row = await progress(db)
            if row["retained_receipt_messages"] == count:
                return row
            await asyncio.sleep(0.05)
        raise AssertionError(
            "expected committed receipts not observed\n" + diagnostic()
        )

    try:
        deadline = time.monotonic() + 25
        while True:
            assert worker.poll() is None, diagnostic()
            stats = await inspect("stats")
            if stats and node in stats and stats[node]["pool"].get("processes"):
                break
            assert time.monotonic() < deadline, diagnostic()
            await asyncio.sleep(0.1)
        children = stats[node]["pool"]["processes"]
        assert len(children) == 1
        child = children[0]
        # Verify OS parentage before signalling; never trust an unrelated reply PID.
        stat_text = await asyncio.to_thread(Path(f"/proc/{child}/stat").read_text)
        fields = stat_text.rsplit(")", 1)[1].split()
        assert int(fields[1]) == worker.pid
        os.kill(child, signal.SIGSTOP)
        paused = child
        deadline = time.monotonic() + 2
        while process_identity(child)[0] not in {"T", "t"}:
            assert time.monotonic() < deadline
            await asyncio.sleep(0.01)
        first = await request(db)
        sent = await send(first)
        # A stopped child cannot acknowledge pool acceptance; parent has reserved it.
        deadline = time.monotonic() + 10
        while True:
            reserved = await inspect("reserved")
            if reserved and any(
                task["id"] == sent.id for task in reserved.get(node, [])
            ):
                break
            assert time.monotonic() < deadline, diagnostic()
            await asyncio.sleep(0.05)
        assert await inspect("ping") == {node: {"ok": "pong"}}
        assert (await progress(db))["retained_receipt_messages"] == 0
        assert await sql(db, "SELECT value FROM delivery_test_counter") == 0
        os.kill(child, signal.SIGCONT)
        paused = None
        first_row = await wait_receipts(1)
        assert first_row["latest_receipt_recorded_timestamp_seconds"] > 0
        assert await sql(db, "SELECT value FROM delivery_test_counter") == 1

        # Same queue + one process: subsequent committed task is a duplicate barrier.
        await send(first)
        second = await request(db, key="two")
        await send(second)
        await wait_receipts(2)
        assert await sql(db, "SELECT value FROM delivery_test_counter") == 2

        failed = await request(db, key="fail", payload={"fail": True})
        await send(failed)
        third = await request(db, key="three")
        await send(third)
        row = await wait_receipts(3)
        assert row["incomplete_messages"] == 1
        assert (
            await sql(
                db, "SELECT count(*) FROM inbox_message WHERE message_id=:id", id=failed
            )
            == 0
        )
        assert await sql(db, "SELECT value FROM delivery_test_counter") == 3
        assert "synthetic-handler-rollback" in log_path.read_text()
    finally:
        if paused is not None:
            os.kill(paused, signal.SIGCONT)
        if worker.poll() is None:
            worker.terminate()
            try:
                await asyncio.to_thread(worker.wait, timeout=10)
            except subprocess.TimeoutExpired:
                # This process group was created solely for this test.
                os.killpg(worker.pid, signal.SIGKILL)
                await asyncio.to_thread(worker.wait, timeout=5)
        log.close()
        client.close()
        with Connection(broker, connect_timeout=3) as connection:
            channel = connection.channel()
            channel.queue_delete(queue)
            channel.exchange_delete(queue)
            channel.close()
