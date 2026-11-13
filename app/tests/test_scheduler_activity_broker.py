"""Real Beat process and broker; no Worker or business database is used."""

from __future__ import annotations

import json
import os
import signal
import subprocess
import sys
import time
from pathlib import Path
from urllib.parse import urlparse
from uuid import uuid4

import pytest
from kombu import Connection

from app.infrastructure.messaging.scheduler_activity import (
    activity_path,
    process_identity,
    read_activity,
)

ROOT = Path(__file__).resolve().parents[1]


def test_real_beat_publication_pause_restart_and_dead_process_rejection(
    tmp_path, celery_probe_config
):
    broker = os.environ.get("CELERY_PROBE_TEST_BROKER_URL")
    if not broker:
        pytest.skip("requires disposable CELERY_PROBE_TEST_BROKER_URL")
    parsed = urlparse(broker)
    assert parsed.scheme == "amqp"
    assert parsed.hostname in {"localhost", "127.0.0.1"}
    assert parsed.path == "/luna_probe_tests"
    queue = "beat.probe." + uuid4().hex
    schedule = str(tmp_path / "beat-schedule")
    env = {
        **os.environ,
        "CELERY_QUEUE": queue,
        "CELERY_BROKER_URL": broker,
        "CELERY_RESULT_BACKEND": "",
    }
    log_path = tmp_path / "beat.log"
    processes = []

    def diagnostic():
        return log_path.read_text(errors="replace")[-4000:].replace(
            parsed.password or "synthetic-password", "<redacted>"
        )

    def start(log):
        process = subprocess.Popen(
            [
                sys.executable,
                "-m",
                "celery",
                "-A",
                "app.bootstrap.scheduler:celery_app",
                "beat",
                "--loglevel=WARNING",
                "--max-interval=1",
                "--schedule",
                schedule,
            ],
            cwd=ROOT,
            env=env,
            stdin=subprocess.DEVNULL,
            stdout=log,
            stderr=log,
        )
        processes.append(process)
        return process

    def wait_for(process, predicate, seconds=20):
        deadline = time.monotonic() + seconds
        while time.monotonic() < deadline:
            assert process.poll() is None, diagnostic()
            try:
                snapshot = read_activity(schedule, max_age=10)
            except (OSError, ValueError):
                snapshot = None
            if (
                snapshot is not None
                and snapshot["pid"] == process.pid
                and predicate(snapshot)
            ):
                return snapshot
            time.sleep(0.05)
        raise AssertionError("Beat did not reach expected activity\n" + diagnostic())

    def stop(process):
        if process.poll() is not None:
            return
        # A paused process must resume before graceful shutdown can work.
        process.send_signal(signal.SIGCONT)
        process.terminate()
        try:
            process.wait(timeout=8)
        except subprocess.TimeoutExpired:
            process.kill()
            process.wait(timeout=5)

    try:
        with log_path.open("w") as log:
            first = start(log)
            sent = wait_for(first, lambda row: row["publish_returns"] >= 1)
            assert sent["publish_errors"] == 0
            # Actual message bytes in our queue, not merely apply_async's return.
            with Connection(broker, connect_timeout=3) as connection:
                channel = connection.channel()
                message = channel.basic_get(queue)
                assert message is not None, diagnostic()
                task = message.headers["task"]
                assert task.startswith("app.tasks.") or task == "celery.backend_cleanup"
                channel.basic_ack(message.delivery_info["delivery_tag"])
                channel.close()
            first.send_signal(signal.SIGSTOP)
            deadline = time.monotonic() + 2
            while process_identity(first.pid)[0] not in {"T", "t"}:
                assert time.monotonic() < deadline
                time.sleep(0.01)
            with pytest.raises(ValueError, match="inactive"):
                read_activity(schedule, max_age=30)
            before = json.loads(activity_path(schedule).read_bytes())["tick_count"]
            first.send_signal(signal.SIGCONT)
            wait_for(first, lambda row: row["tick_count"] > before)
            stop(first)
            assert activity_path(schedule).exists()  # deliberately retain stale file
            with pytest.raises((OSError, ValueError)):
                read_activity(schedule, max_age=30)
            second = start(log)
            restored = wait_for(second, lambda row: row["publish_returns"] >= 1)
            assert restored["pid"] != first.pid
            result = subprocess.run(
                [
                    sys.executable,
                    "-m",
                    "app.cli.scheduler_activity",
                    "--schedule",
                    schedule,
                    "--max-age",
                    "10",
                ],
                cwd=ROOT,
                env=env,
                capture_output=True,
                text=True,
                timeout=5,
            )
            assert result.returncode == 0, diagnostic()
            assert json.loads(result.stdout)["pid"] == second.pid
            assert result.stderr == ""
            stop(second)
    finally:
        for process in processes:
            stop(process)
        # Exact synthetic queue/exchange; never a configured business name.
        with Connection(broker, connect_timeout=3) as connection:
            channel = connection.channel()
            channel.queue_delete(queue)
            channel.exchange_delete(queue)
            channel.close()
