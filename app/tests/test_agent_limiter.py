"""Onboarding rate limits must stay inside the runtime Redis command allow-list."""

from pathlib import Path

import pytest

from app.infrastructure.workbench import agent_limiter
from app.infrastructure.workbench.agent_limiter import consume_window

_ALLOWED = {"INCR", "EXPIRE", "MULTI", "EXEC"}
_ACL = (
    Path(__file__).resolve().parents[4]
    / "sunmoonun/gitops/components/app-platform/common/backend/redis/provision.py.j2"
)


class RecordingRedis:
    def __init__(self):
        self.commands = []
        self.values = {}
        self.expiry = {}

    def pipeline(self, transaction=False):
        return RecordingPipeline(self, transaction)


class RecordingPipeline:
    def __init__(self, redis, transaction):
        self.redis = redis
        self.transaction = transaction
        self.ops = []

    async def __aenter__(self):
        return self

    async def __aexit__(self, exc_type, exc, traceback):
        return False

    def incr(self, key):
        self.ops.append(("INCR", key))
        return self

    def expire(self, key, seconds, nx=False):
        self.ops.append(("EXPIRE", key, seconds, nx))
        return self

    async def execute(self):
        if self.transaction:
            self.redis.commands.append("MULTI")
        results = []
        for name, *args in self.ops:
            self.redis.commands.append(name)
            if name == "INCR":
                key = args[0]
                self.redis.values[key] = self.redis.values.get(key, 0) + 1
                results.append(self.redis.values[key])
            else:
                key, seconds, nx = args
                if not (nx and key in self.redis.expiry):
                    self.redis.expiry[key] = seconds
                results.append(1)
        if self.transaction:
            self.redis.commands.append("EXEC")
        return results


@pytest.fixture
def clock(monkeypatch):
    now = {"value": 1_700_000_000}

    def read():
        return now["value"]

    monkeypatch.setattr(agent_limiter.time, "time", read)
    return now


async def test_window_uses_only_allowlisted_commands_and_recovers(clock):
    redis = RecordingRedis()
    allowed = [
        await consume_window(redis, "owner", seconds=60, limit=2) for _ in range(2)
    ]
    blocked = await consume_window(redis, "owner", seconds=60, limit=2)
    clock["value"] += 60
    recovered = await consume_window(redis, "owner", seconds=60, limit=2)

    assert allowed == [True, True]
    assert blocked is False
    assert recovered is True
    assert set(redis.commands) <= _ALLOWED
    assert set(redis.commands) >= {"INCR", "EXPIRE", "MULTI", "EXEC"}
    first_key, later_key = list(redis.values)
    assert first_key != later_key
    assert redis.values[first_key] == 3
    assert redis.values[later_key] == 1
    assert redis.expiry == {first_key: 60, later_key: 60}


def test_runtime_acl_allows_the_recorded_commands():
    if not _ACL.is_file():
        pytest.skip("sunmoonun Redis provisioning source is not checked out")
    text = _ACL.read_text()
    for command in ("+incr", "+expire", "+multi", "+exec"):
        assert command in text
