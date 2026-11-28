"""Check business acceptance against simple synthetic artifact and calculation records."""

import json
from unittest.mock import AsyncMock

import pytest

from app.application.workbench.acceptance import judge
from app.application.workbench.acceptance_numbers import judge_numbers
from app.domain.workbench.packs import AcceptanceRule


@pytest.mark.parametrize(
    ("kind", "upstream", "rows"),
    [
        ("identity", [], [{"left_id": "c1", "right_id": "c1"}]),
        ("covers", [{"id": "A"}, {"id": "A"}], [{"id": "A"}]),
        ("covers", [{}], [{}]),
        ("covers", [{"id": 1}], [{"id": True}]),
        ("quoted", [], [{"quotes": {}}]),
    ],
)
async def test_invalid_evidence_fails_acceptance(kind, upstream, rows):
    options = (
        {"field": "id", "source_field": "id", "source": "previous.rows"}
        if kind == "covers"
        else {}
    )
    rules = (AcceptanceRule(kind=kind, path="rows", **options),)
    repo = AsyncMock()
    repo.list_task_events.return_value = [{"cursor": 1, "payload": {"value": "25"}}]
    repo.list_artifacts_with_content.return_value = [
        {"kind": "step", "name": "previous", "content": {"rows": upstream}}
    ]
    verdict = await judge_numbers(
        judge(json.dumps({"rows": rows}), rules),
        rules,
        repo=repo,
        task={"id": "sample"},
    )
    assert not verdict.ok, verdict.checks
