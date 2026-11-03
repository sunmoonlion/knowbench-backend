"""Batching changes transport cost, never numeric provenance or acceptance."""

import copy
import json

import jsonschema
import pytest
from test_knowbot_compute import Reader, fact, world

from app.application.workbench.acceptance import Verdict
from app.application.workbench.acceptance_numbers import judge_numbers
from app.domain.workbench.errors import ComputeRefused
from app.domain.workbench.packs import AcceptanceRule
from app.interfaces.mcp.workbench_mcp import TOOLS, call


def batch():
    return [
        {
            "key": "quarter",
            "name": "difference",
            "expression": "h - q",
            "inputs": {"h": fact(2), "q": fact(1)},
        },
        {
            "key": "ratio",
            "name": "ratio",
            "expression": "x / q",
            "inputs": {"x": "batch:quarter", "q": fact(1)},
            "round": 6,
        },
        {
            "key": "total",
            "name": "sum",
            "expression": "sum(xs)",
            "inputs": {"xs": ["batch:quarter", fact(1)]},
        },
    ]


async def test_batch_reads_facts_once_and_records_real_ids_for_chained_results():
    reader = Reader()
    records, tool = world(reader)
    result = await tool.compute_batch(records.task["id"], batch())
    assert reader.asked == [[fact(2), fact(1)]]
    assert [r["value"] for r in result["results"]] == ["150.15", "1.5", "250.25"]
    first, second, third = result["results"]
    assert second["inputs"][0]["ref"] == first["id"]
    assert third["inputs"][0]["refs"] == [first["id"], fact(1)]
    assert "batch:" not in json.dumps(records.repo.events)
    rows = [{"value": r["value"], "compute_id": r["id"]} for r in result["results"]]
    checks = (AcceptanceRule(kind="computed", path="rows", tolerance=0),)
    judged = await judge_numbers(
        Verdict(ok=True, parsed={"rows": rows}),
        checks,
        task=records.task,
        repo=records.repo,
        reader=reader,
    )
    assert judged.ok, judged.checks
    # Later ordinary calls can use batch receipts; no second evidence format.
    later = await tool.compute(records.task["id"], "reuse", "x", {"x": third["id"]})
    assert later["value"] == "250.25"


@pytest.mark.parametrize(
    "fault",
    ["forward", "self", "duplicate", "raw_number", "unknown", "zero", "large", "extra"],
)
async def test_invalid_batch_writes_no_partial_computation(fault):
    records, tool = world()
    rows = batch()
    if fault == "forward":
        rows[0]["inputs"]["h"] = "batch:ratio"
    elif fault == "self":
        rows[0]["inputs"]["h"] = "batch:quarter"
    elif fault == "duplicate":
        rows[1]["key"] = "quarter"
    elif fault == "raw_number":
        rows[-1]["inputs"]["xs"] = [250.25]
    elif fault == "unknown":
        rows[-1]["inputs"]["xs"] = ["c999"]
    elif fault == "zero":
        rows[-1]["expression"] = "1 / sum(xs)"
        rows[-1]["inputs"]["xs"] = [fact(4)]
    elif fault == "large":
        rows = [dict(rows[0], key=f"r{i}") for i in range(51)]
    else:
        rows[-1]["value"] = 250.25
    with pytest.raises(ComputeRefused):
        await tool.compute_batch(records.task["id"], rows)
    assert records.repo.events == []


async def test_batch_mcp_and_single_call_have_unambiguous_schema():
    records, _ = world()
    args = {"task": records.task["id"], "batch": batch()}
    schema = TOOLS["compute"]["inputSchema"]
    jsonschema.validate(args, schema)
    result = await call(records, "compute", args, Reader())
    assert len(result["results"]) == 3
    single = {
        "task": records.task["id"],
        "name": "one",
        "expression": "x",
        "inputs": {"x": fact(1)},
    }
    jsonschema.validate(single, schema)
    mixed = copy.deepcopy(args) | single
    with pytest.raises(jsonschema.ValidationError):
        jsonschema.validate(mixed, schema)
    before = len(records.repo.events)
    with pytest.raises(ComputeRefused):
        await call(records, "compute", mixed, Reader())
    assert len(records.repo.events) == before


async def test_total_input_bound_is_checked_before_reading_facts():
    reader = Reader()
    records, tool = world(reader)
    rows = [
        {
            "key": "many",
            "name": "many",
            "expression": "sum(xs)",
            "inputs": {"xs": [fact(1)] * 500},
        },
        {"key": "one", "name": "one", "expression": "x", "inputs": {"x": fact(2)}},
    ]
    with pytest.raises(ComputeRefused, match="500 inputs"):
        await tool.compute_batch(records.task["id"], rows)
    assert reader.asked == []
    assert records.repo.events == []


async def test_batch_matches_single_calculations_including_rounded_dependency():
    records, tool = world()
    rows = batch()
    rows[0]["round"] = 1
    rows[1]["expression"] = "x / q - 1"
    batched = await tool.compute_batch(records.task["id"], rows)
    ids = {}
    singles = []
    for row in rows:
        inputs = {
            key: [ids.get(r, r) for r in value]
            if isinstance(value, list)
            else ids.get(value, value)
            for key, value in row["inputs"].items()
        }
        result = await tool.compute(
            records.task["id"], row["name"], row["expression"], inputs, row.get("round")
        )
        ids["batch:" + row["key"]] = result["id"]
        singles.append(result["value"])
    assert singles == [r["value"] for r in batched["results"]]
