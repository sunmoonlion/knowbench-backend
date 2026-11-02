"""Record the actual HTTP steps/data contract with nonempty knowbase pins.

Only generated IDs, wall-clock timestamps and elapsed seconds are normalized;
the response shape and data-source values remain exactly what the API returns.
"""

import asyncio
import json
import os
import re
from pathlib import Path

import pytest
from preview_recorder import Recorder
from test_workbench_expert_data_db import asked, known
from test_workbench_expert_data_db import world as world  # noqa: F401
from test_workbench_ledger_db import db as db  # noqa: F401
from test_workbench_routes import A
from test_workbench_routes import make_client as make_client  # noqa: F401

FIXTURE = Path(__file__).parent / "fixtures/knowbase-run.json"
CONSUMER = (
    Path(__file__).resolve().parents[3]
    / "knowbench-web-frontend/app/tests/fixtures/knowbase-run.json"
)


def normalize(value):
    if isinstance(value, dict):
        return {k: 0 if k == "seconds" else normalize(v) for k, v in value.items()}
    if isinstance(value, list):
        return [normalize(v) for v in value]
    if isinstance(value, str) and re.fullmatch(
        r"\d{4}-\d{2}-\d{2}T[0-9:.]+(?:Z|[+-]\d{2}:\d{2})", value
    ):
        return "2026-01-01T00:00:00+00:00"
    return value


def record(payload):
    recorder = Recorder(
        "knowbase-run", title="合成资料的步骤响应", description="非空已钉数据"
    )
    result = json.loads(
        recorder.steady(json.dumps(normalize(payload), ensure_ascii=False))
    )
    encoded = json.dumps(result, ensure_ascii=False, indent=2) + "\n"
    if os.environ.get("UPDATE_RUN_CONTRACT_FIXTURE") == "1":
        FIXTURE.write_text(encoded)
    assert FIXTURE.read_text() == encoded
    if CONSUMER.exists():
        assert CONSUMER.read_text() == encoded


@pytest.mark.asyncio
async def test_actual_http_response_matches_nonempty_data_contract(
    make_client, db, world
):  # noqa: F811
    await known(db)
    async with make_client(A) as http:
        task, _, _ = await asked(http, db, world, "k-knowbase-contract")
        steps = await http.get(f"/api/workbench/tasks/{task}/steps")
        data = await http.get(f"/api/workbench/tasks/{task}/data")
        assert steps.status_code == data.status_code == 200
        payload = steps.json()
        assert payload["task"]["state"] == "WAITING"
        assert payload["task"]["chosen_data"]
        assert {d["source"] for d in payload["task"]["chosen_data"]} == {"knowbase"}
        await asyncio.to_thread(record, {"steps": payload, "data": data.json()})
