"""分身的计算工具 `compute` 与计算记录（SDD 0030 §3.2）。

委托是这个用户的、方向盘在专家手里（同读项目记录）。输入只能是事实编号（`fact:<行>`、
`raw:<行>`，工作台凭这件委托的运行授权回 knowbase 取，run_reader）或这件委托前面一次
计算的编号（`c17`）；模型不能直接给数。每次算成都记一条会话事件 `knowbot/compute`，编号取事件的游标
（`c<游标>`），数值验收（acceptance_numbers）按编号回查。
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from decimal import Decimal
from typing import Any

from app.application.ports.workbench import WorkbenchStore
from app.application.workbench.records import ProjectRecords
from app.application.workbench.run_reader import RunReader, is_fact_ref
from app.domain.workbench import compute as calc
from app.domain.workbench.errors import ComputeRefused
from app.domain.workbench.projects import TOOL_RESULT_MAX_BYTES

COMPUTE_EVENT = "knowbot/compute"
MAX_NAMES = 50
MAX_NAME_CHARS = 200


async def computations(repo: WorkbenchStore, task_id: str) -> dict[str, dict[str, Any]]:
    """这件委托的计算记录：编号 → 记录。"""
    events = await repo.list_task_events(task_id, types=(COMPUTE_EVENT,))
    return {f"c{e['cursor']}": e["payload"] for e in events}


MAX_BATCH = 50


@dataclass
class _Calculation:
    name: str
    expression: str
    places: int | None
    refs: dict[str, list[str]]
    lists: set[str]
    key: str | None = None


def _prepare(name: Any, expression: Any, inputs: Any, places: Any) -> _Calculation:
    if not isinstance(name, str) or not name.strip() or len(name) > MAX_NAME_CHARS:
        raise ComputeRefused("give the computation a name (at most 200 characters)")
    if not isinstance(expression, str):
        raise ComputeRefused("the expression is empty")
    if not isinstance(inputs, dict) or not inputs or len(inputs) > MAX_NAMES:
        raise ComputeRefused(f"inputs must be an object of 1 to {MAX_NAMES} names")
    if places is not None and (
        isinstance(places, bool)
        or not isinstance(places, int)
        or not 0 <= places <= calc.MAX_ROUND
    ):
        raise ComputeRefused(f"round must be a whole number from 0 to {calc.MAX_ROUND}")
    refs: dict[str, list[str]] = {}
    lists: set[str] = set()
    for key, value in inputs.items():
        if not isinstance(key, str) or not calc.NAME.fullmatch(key):
            raise ComputeRefused(
                f"input name {str(key)[:40]!r} must start with an ASCII letter or _, "
                "followed by ASCII letters, digits or _ (1–32 characters, case-sensitive)"
            )
        if isinstance(value, list):
            lists.add(key)
            items = value
        else:
            items = [value]
        if not items or not all(isinstance(i, str) for i in items):
            raise ComputeRefused(
                f"input {key!r} must be a fact ref like fact:…, "
                "an earlier id like c17, or a list of them"
            )
        refs[key] = items
    return _Calculation(name.strip(), expression.strip(), places, refs, lists)


class KnowbotCompute:
    def __init__(
        self, records: ProjectRecords, reader: RunReader | None = None
    ) -> None:
        self.records = records
        self.repo = records.repo
        self.reader = reader

    async def compute(
        self,
        task: Any,
        name: Any,
        expression: Any,
        inputs: Any,
        places: Any = None,
    ) -> dict[str, Any]:
        working = await self.records._working(task)
        return (
            await self._execute(working, [_prepare(name, expression, inputs, places)])
        )[0]

    async def compute_batch(self, task: Any, batch: Any) -> dict[str, Any]:
        """One fact read, bounded pure evaluation, then atomic ordinary receipts.

        batch:key can only name an earlier row. Stored provenance always replaces
        those request-local names with real c<cursor> ids, so existing acceptance
        and subsequent compute calls consume exactly the same evidence format.
        """
        working = await self.records._working(task)
        if not isinstance(batch, list) or not 1 <= len(batch) <= MAX_BATCH:
            raise ComputeRefused(f"batch must contain 1 to {MAX_BATCH} calculations")
        prepared = []
        seen: set[str] = set()
        for row in batch:
            if not isinstance(row, dict) or set(row) - {
                "key",
                "name",
                "expression",
                "inputs",
                "round",
            }:
                raise ComputeRefused(
                    "batch rows accept only key, name, expression, inputs and round"
                )
            key = row.get("key")
            if not isinstance(key, str) or not calc.NAME.fullmatch(key) or key in seen:
                raise ComputeRefused(
                    "batch keys must be unique ASCII identifiers (1–32 characters)"
                )
            item = _prepare(
                row.get("name"),
                row.get("expression"),
                row.get("inputs"),
                row.get("round"),
            )
            item.key = key
            for refs in item.refs.values():
                for ref in refs:
                    if ref.startswith("batch:") and ref[6:] not in seen:
                        raise ComputeRefused(
                            f"batch {key}: input must name an earlier batch key"
                        )
            prepared.append(item)
            seen.add(key)
        results = await self._execute(working, prepared)
        return {
            "results": [
                dict(result, key=item.key)
                for item, result in zip(prepared, results, strict=True)
            ]
        }

    async def _execute(
        self, working: dict[str, Any], items: list[_Calculation]
    ) -> list[dict[str, Any]]:
        flat = [ref for item in items for refs in item.refs.values() for ref in refs]
        if len(flat) > calc.MAX_INPUTS:
            raise ComputeRefused(
                f"at most {calc.MAX_INPUTS} inputs in one computation or batch"
            )
        batch_mode = items[0].key is not None
        for ref in flat:
            if (
                not calc.COMPUTE_ID.fullmatch(ref)
                and not is_fact_ref(ref)
                and not (batch_mode and ref.startswith("batch:"))
            ):
                raise ComputeRefused(
                    f"not a fact ref or an earlier id: {ref[:80]!r} (write fact:… from query_facts, or c17)"
                )
        earlier = (
            await computations(self.repo, str(working["id"]))
            if any(calc.COMPUTE_ID.fullmatch(ref) for ref in flat)
            else {}
        )
        fact_refs = list(dict.fromkeys(ref for ref in flat if is_fact_ref(ref)))
        if fact_refs and self.reader is None:
            raise ComputeRefused("facts cannot be read here")
        facts = (
            await self.reader.facts(self.repo, working, fact_refs)
            if fact_refs and self.reader is not None
            else {}
        )
        calculated: dict[str, Decimal] = {}

        def value_of(ref: str) -> Decimal:
            if ref.startswith("batch:"):
                return calculated[ref[6:]]
            if calc.COMPUTE_ID.fullmatch(ref):
                record = earlier.get(ref)
                if record is None:
                    raise ComputeRefused(f"no earlier computation {ref} in this task")
                found = calc.decimal_of(record.get("value"))
                if found is None:
                    raise ComputeRefused(f"computation {ref} has no value")
                return found
            return facts[ref]

        payloads = []
        for item in items:
            values: dict[str, calc.Value] = {}
            recorded = []
            for key, refs in item.refs.items():
                numbers = [value_of(ref) for ref in refs]
                values[key] = numbers if key in item.lists else numbers[0]
                entry: dict[str, Any] = {"name": key}
                if key in item.lists:
                    entry |= {
                        "refs": refs,
                        "values": [calc.text_of(n) for n in numbers],
                    }
                else:
                    entry |= {"ref": refs[0], "value": calc.text_of(numbers[0])}
                recorded.append(entry)
            try:
                number = calc.rounded(
                    calc.evaluate(item.expression, values), item.places
                )
            except ComputeRefused as exc:
                if item.key is None:
                    raise
                raise ComputeRefused(f"batch {item.key}: {exc}") from exc
            if item.key is not None:
                calculated[item.key] = number
            payloads.append(
                {
                    "name": item.name,
                    "expression": item.expression,
                    "inputs": recorded,
                    "round": item.places,
                    "value": calc.text_of(number),
                }
            )

        result = []
        ids: dict[str, str] = {}
        async with self.repo.transaction():
            # A provider read may have yielded while the task was stopped.
            await self.records._working(str(working["id"]))
            for item, payload in zip(items, payloads, strict=True):

                def resolve(ref: str) -> str:
                    return ids[ref[6:]] if ref.startswith("batch:") else ref

                for entry in payload["inputs"]:
                    if "refs" in entry:
                        entry["refs"] = [resolve(ref) for ref in entry["refs"]]
                    else:
                        entry["ref"] = resolve(entry["ref"])
                event = await self.repo.append_event(
                    session_id=str(working["session_id"]),
                    kind="records",
                    event_type=COMPUTE_EVENT,
                    payload=payload,
                    task_id=str(working["id"]),
                )
                identifier = f"c{event['cursor']}"
                if item.key is not None:
                    ids[item.key] = identifier
                result.append(
                    {
                        "id": identifier,
                        "name": item.name,
                        "value": payload["value"],
                        "formula": item.expression,
                        "inputs": payload["inputs"],
                    }
                )
            response = (
                {
                    "results": [
                        dict(row, key=item.key)
                        for item, row in zip(items, result, strict=True)
                    ]
                }
                if batch_mode
                else result[0]
            )
            if (
                len(json.dumps(response, ensure_ascii=False).encode("utf-8"))
                > TOOL_RESULT_MAX_BYTES
            ):
                # Refuse inside the transaction: no receipts survive for a result
                # that the MCP transport cannot deliver to the model.
                raise ComputeRefused(
                    "computation result is too large; use a smaller batch or fewer inputs"
                )
        return result
