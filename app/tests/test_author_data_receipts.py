from contextlib import asynccontextmanager
from types import SimpleNamespace
from unittest.mock import AsyncMock

from app.application.workbench import test_runs as runs


@asynccontextmanager
async def transaction():
    yield


def material():
    return {
        "material_id": "annual",
        "version_no": 1,
        "sha256": "a" * 64,
        "basis": "public",
        "basis_ref": "record",
        "name": "年报",
    }


async def apply(monkeypatch, answers, existing):
    requests = [{"id": key, "labels": [key], "status": "pending"} for key in answers]

    async def answer(run_id, key):
        return {"answer": {"data": answers[key.split(":")[-1]]}}

    repo = SimpleNamespace(
        get_task_resolution=AsyncMock(return_value={"detail": {"requests": requests}}),
        get_test_answer=AsyncMock(side_effect=answer),
        list_task_data=AsyncMock(return_value=existing),
        transaction=transaction,
        pin_task_data=AsyncMock(),
        update_task_resolution=AsyncMock(),
    )
    from app.application.workbench.ledger import Ledger

    monkeypatch.setattr(Ledger, "resume_data", AsyncMock())
    assert await runs.KnowbotTestConductor(None)._apply_data(
        repo, {"run_id": "r"}, {"id": "t"}
    )
    return repo.update_task_resolution.await_args.kwargs["detail"]


async def test_selected_material_already_pinned_stays_accepted(monkeypatch):
    detail = await apply(
        monkeypatch,
        {"年报": {"grant_id": "g", "items": [material()]}},
        [{"item_id": "annual", "version_no": 1}],
    )
    assert not detail.get("incomplete")
    assert detail["requests"][0]["status"] == "done"


async def test_mixed_answers_retain_only_the_declined_item(monkeypatch):
    detail = await apply(
        monkeypatch,
        {"年报": {"grant_id": "g", "items": [material()]}, "分部表": {"items": []}},
        [],
    )
    assert detail["incomplete"] == ["分部表：你没要"]
    assert all(r["status"] == "done" for r in detail["requests"])
