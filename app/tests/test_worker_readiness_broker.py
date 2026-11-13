"""Opt-in real prefork worker; ONLY a disposable loopback RabbitMQ test vhost."""

from __future__ import annotations

import os
import subprocess
import sys
import time
from pathlib import Path
from urllib.parse import urlparse
from uuid import uuid4

import pytest
from celery import Celery

ROOT = Path(__file__).resolve().parents[1]


def test_real_worker_queue_loss_recovery_and_unreachable(celery_probe_config):
    broker = os.environ.get("CELERY_PROBE_TEST_BROKER_URL")
    if not broker:
        pytest.skip("requires disposable CELERY_PROBE_TEST_BROKER_URL")
    parsed = urlparse(broker)
    assert parsed.scheme == "amqp"
    assert parsed.hostname in {"127.0.0.1", "localhost"}
    assert parsed.path == "/luna_probe_tests"
    suffix = uuid4().hex
    pod = f"probe-{suffix}"
    node = f"celery@{pod}"
    queue = f"probe.{suffix}"
    env = {
        **os.environ,
        "POD_NAME": pod,
        "CELERY_QUEUE": queue,
        "CELERY_BROKER_URL": broker,
        "CELERY_RESULT_BACKEND": "",
    }
    client = Celery(f"probe-test-{suffix}", broker=broker)
    worker = subprocess.Popen(
        [
            sys.executable,
            "-m",
            "celery",
            "-A",
            "app.bootstrap.worker:celery_app",
            "worker",
            "--pool=prefork",
            "--concurrency=1",
            f"--hostname={node}",
            "--without-gossip",
            "--without-mingle",
            "--without-heartbeat",
            "--loglevel=WARNING",
        ],
        cwd=ROOT,
        env=env,
        stdin=subprocess.DEVNULL,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )

    def run_probe():
        return subprocess.run(
            [sys.executable, "-m", "app.cli.worker_readiness"],
            cwd=ROOT,
            env=env,
            capture_output=True,
            text=True,
            timeout=9,
        )

    try:
        deadline = time.monotonic() + 25
        while True:
            assert worker.poll() is None, "test worker exited before readiness"
            result = run_probe()
            if result.returncode == 0:
                break
            assert time.monotonic() < deadline, "test worker never became ready"
            time.sleep(0.2)
        assert result.stdout == result.stderr == ""
        # Cancel only our randomly named synthetic consumer, never a business queue.
        cancelled = client.control.cancel_consumer(
            queue, destination=[node], reply=True, timeout=2
        )
        assert cancelled and "ok" in cancelled[0][node]
        assert client.control.inspect(destination=[node], timeout=2).ping() == {
            node: {"ok": "pong"}
        }
        result = run_probe()
        assert result.returncode == 1
        assert result.stdout == "" and result.stderr == "worker_not_ready\n"
        wrong_queue = f"wrong.{suffix}"
        wrong = client.control.add_consumer(
            wrong_queue, destination=[node], reply=True, timeout=2
        )
        assert wrong and "ok" in wrong[0][node]
        assert client.control.inspect(destination=[node], timeout=2).ping() == {
            node: {"ok": "pong"}
        }
        assert run_probe().returncode == 1
        removed = client.control.cancel_consumer(
            wrong_queue, destination=[node], reply=True, timeout=2
        )
        assert removed and "ok" in removed[0][node]
        restored = client.control.add_consumer(
            queue,
            exchange=queue,
            exchange_type="direct",
            routing_key=queue,
            destination=[node],
            reply=True,
            timeout=2,
        )
        assert restored and "ok" in restored[0][node]
        assert run_probe().returncode == 0
        # Wrong destination must not be rescued by a healthy sibling's reply.
        env["POD_NAME"] = f"missing-{suffix}"
        assert run_probe().returncode == 1
        env["POD_NAME"] = pod
        worker.terminate()
        worker.wait(timeout=10)
        assert run_probe().returncode == 1
        env["CELERY_BROKER_URL"] = "amqp://invalid:synthetic@127.0.0.1:1/unreachable"
        started = time.monotonic()
        result = run_probe()
        assert result.returncode == 1
        assert result.stdout == "" and result.stderr == "worker_not_ready\n"
        assert time.monotonic() - started < 8
    finally:
        if worker.poll() is None:
            worker.terminate()
            try:
                worker.wait(timeout=10)
            except subprocess.TimeoutExpired:
                worker.kill()
                worker.wait(timeout=5)
        client.close()
