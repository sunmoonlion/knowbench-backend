"""Provider queries use the normal confirmed demand and task-resume lifecycle.

Synthetic supplier ports only; no live Wind calls or production confirmations.
"""

from datetime import UTC, datetime, timedelta

import pytest
from test_source_query_planning import QUERY
from test_workbench_expert_data_db import (
    DataWaiter,
    asked,
    candidate,
    known,
    rows,
    workbench_stores,
)
from test_workbench_expert_data_db import world as world  # noqa: F401
from test_workbench_ledger_db import db as db  # noqa: F401
from test_workbench_routes import A
from test_workbench_routes import make_client as make_client  # noqa: F401

pytestmark = pytest.mark.asyncio


@pytest.mark.parametrize("arrives", [True, False])
async def test_source_query_is_not_called_before_confirmation_and_resumes_only_when_ready(
    make_client, db, world, monkeypatch, arrives
):
    kb, supply = world["kb"], world["supply"]
    issued = {**QUERY, "request_id": "a" * 32}

    def items(fetched):
        return [
            {
                "need_key": need["key"],
                "key": {"doc_type": "other_text", "source_query": issued},
                "status": "resolved" if fetched and arrives else "to_confirm",
                "chosen": [{**candidate("2024"), "rendition_kind": "text"}]
                if fetched and arrives
                else [],
                "candidates": [],
                "reason": None,
            }
            for need in kb.needs
        ]

    monkeypatch.setattr(kb, "_items", items)
    original_confirm = kb.confirm

    async def confirm(token, **kwargs):
        confirmed = await original_confirm(token, **kwargs)
        for item in confirmed["items"]:
            item["reading"] = "text"
        return confirmed

    monkeypatch.setattr(kb, "confirm", confirm)
    await known(db)
    async with make_client(A) as http:
        tid, _, state = await asked(
            http,
            db,
            world,
            "provider-query-lifecycle",
            propose=[
                {
                    "doc_type": "other_text",
                    "reading": "text",
                    "essential": True,
                    "reason": "Query source evidence for the synthetic test",
                    "source_query": {
                        **QUERY,
                        "request_id": "model-cannot-select-receipt",
                    },
                }
            ],
        )
        assert state == "WAITING" and not supply.submitted
        assert kb.needs[0]["source_query"] == {**QUERY, "request_id": None}
        response = await http.get(f"/api/workbench/tasks/{tid}/data")
        item = response.json()["requests"][0]["items"][0]
        assert item["key"]["source_query"] == issued
        response = await http.post(
            f"/api/workbench/tasks/{tid}/data-requests/confirm", json={}
        )
        assert response.status_code == 200, response.text
        assert response.json()["status"] == "supplying"
    confirmed = [call for kind, call in kb.calls if kind == "confirm"]
    assert len(confirmed) == 1
    assert confirmed[0]["items"][0]["key"]["source_query"] == issued
    assert len(supply.submitted) == 1
    assert supply.submitted[0]["confirmation_id"] == "conf-1"
    assert supply.asked[-1]["items"][0]["readings"] == ["text"]
    supply.status = "ready" if arrives else "failed"
    waiter = DataWaiter(
        workbench_stores(db),
        kb,
        supply,
        now=lambda: datetime.now(UTC) + timedelta(hours=1),
    )
    assert await waiter.sweep() == 1
    [current] = await rows(
        db, "select state, waiting_reason from workbench_tasks where id=:t", t=tid
    )
    assert current["state"] == ("VALIDATING" if arrives else "WAITING")
    if arrives:
        pins = await rows(
            db,
            "select item_id, rendition_kind from workbench_task_data where task_id=:t",
            t=tid,
        )
        assert any(
            p["item_id"] == "m-2024" and p["rendition_kind"] == "text" for p in pins
        )
        assert await waiter.sweep() == 0
    else:
        assert current["waiting_reason"] == "DATA"
        async with make_client(A) as http:
            shown = (await http.get(f"/api/workbench/tasks/{tid}/data")).json()
            assert shown["requests"][0]["status"] == "missing"
            assert shown["requests"][0]["missing"]
        assert await waiter.sweep() == 0
        assert not [call for kind, call in kb.calls if kind == "grant_by_confirmation"]
    assert len(supply.submitted) == 1  # failure never silently buys another query
