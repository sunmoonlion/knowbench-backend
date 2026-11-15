"""One deferred model run must not starve unrelated retention cleanup."""

from contextlib import asynccontextmanager
from datetime import UTC, datetime, timedelta

import pytest

from app.application.workbench.test_runs import KEEP_OUTPUTS, KnowbotTestConductor
from app.domain.workbench.errors import TestCleanupPending as CleanupPending


class CleanupRepo:
    def __init__(self, failure):
        self.failure = failure
        self.visited = []
        self.cleared = []
        self.transactions = []

    async def expired_test_runs(self, now):
        return [{"run_id": "unknown-model"}, {"run_id": "safe-host"}]

    @asynccontextmanager
    async def transaction(self):
        try:
            yield
        except Exception:
            self.transactions.append("rollback")
            raise
        else:
            self.transactions.append("commit")

    async def purge_test_run(self, run_id):
        self.visited.append(run_id)
        if run_id == "unknown-model":
            raise self.failure

    async def clear_test_outputs(self, before):
        self.cleared.append(before)


def conductor(repo, now):
    @asynccontextmanager
    async def stores():
        yield repo

    return KnowbotTestConductor(stores, now=lambda: now)


async def test_deferred_model_cleanup_continues_safe_runs_and_output_retention():
    now = datetime(2026, 11, 15, tzinfo=UTC)
    repo = CleanupRepo(CleanupPending("Model usage is still unknown"))
    service = conductor(repo, now)

    assert await service.cleanup() == 1
    assert repo.visited == ["unknown-model", "safe-host"]
    assert repo.transactions == ["rollback", "commit", "commit"]
    assert repo.cleared == [now - KEEP_OUTPUTS]

    # The pending run remains retryable, but does not suppress later cleanup.
    assert await service.cleanup() == 1
    assert repo.visited == ["unknown-model", "safe-host"] * 2
    assert repo.cleared == [now - KEEP_OUTPUTS]
    service.now = lambda: now + timedelta(hours=1)
    assert await service.cleanup() == 1
    assert repo.cleared == [now - KEEP_OUTPUTS, now + timedelta(hours=1) - KEEP_OUTPUTS]


@pytest.mark.parametrize(
    "error", [ValueError("invalid state"), RuntimeError("database failed")]
)
async def test_cleanup_does_not_swallow_unexpected_failures(error):
    now = datetime(2026, 11, 15, tzinfo=UTC)
    repo = CleanupRepo(error)
    service = conductor(repo, now)

    with pytest.raises(type(error), match=str(error)):
        await service.cleanup()
    assert repo.visited == ["unknown-model"]
    assert repo.transactions == ["rollback"]
    assert repo.cleared == []
    assert service._outputs_cleared_at is None
