"""Provider queries remain proposals; discovery derives owner from the task."""

from unittest.mock import AsyncMock

import jsonschema

from app.application.workbench.expert_data import _asked_need
from app.domain.workbench.supply_prematch import request_items
from app.interfaces.mcp import workbench_mcp

QUERY = {
    "channel": "wind",
    "service": "financial_docs",
    "tool": "get_company_announcements",
    "arguments": {"query": "synthetic annual report", "top_k": 3},
}


def test_supplier_proposal_survives_request_schema_and_prematch_without_subject():
    asked = {
        "doc_type": "other_text",
        "reason": "需要年报查询证据",
        "reading": "text",
        "source_query": QUERY,
        "essential": True,
    }
    jsonschema.validate(
        {"task": "test-task", "needs": [asked]},
        workbench_mcp.TOOLS["request_data"]["inputSchema"],
    )
    asked["source_query"] = {**QUERY, "request_id": "a" * 32}
    need = _asked_need(asked, 1, 1)
    assert need.source_query.model_dump(exclude_none=True) == QUERY
    assert need.formats == ("text",) and need.sources == ("collected",)
    [item] = request_items(
        [
            {
                "ref": "i1",
                "need_key": need.key,
                "status": "to_confirm",
                "key": {
                    "subject": None,
                    "doc_type": "other_text",
                    "source_query": QUERY,
                },
            }
        ],
        [need],
    )
    assert item["key"]["source_query"] == QUERY
    assert item["readings"] == ["text"]


async def test_discovery_uses_authenticated_task_owner_not_model_input(monkeypatch):
    records = AsyncMock()
    records._working.return_value = {"owner_actor_id": "actor-a"}
    records.repo.subject_of_actor.return_value = "owner-a"
    supply = AsyncMock()
    supply.source_tools.return_value = {"sources": []}
    monkeypatch.setattr(workbench_mcp, "knowfeed_supply", lambda: supply)
    assert await workbench_mcp.call(
        records,
        "discover_source_tools",
        {"task": "task-a", "owner": "attacker", "service": "financial_docs"},
    ) == {"sources": []}
    records._working.assert_awaited_once_with("task-a")
    supply.source_tools.assert_awaited_once_with("owner-a", "financial_docs")
