"""分身的计算工具 `compute` 与计算记录（SDD 0030 §3.2）。

委托是这个用户的、方向盘在专家手里（同读项目记录）。输入只能是事实编号（`fact:<行>`、
`raw:<行>`，工作台凭这件委托的运行授权回 knowbase 取，run_reader）或这件委托前面一次
计算的编号（`c17`）；模型不能直接给数。每次算成都记一条会话事件 `knowbot/compute`，编号取事件的游标
（`c<游标>`），数值验收（acceptance_numbers）按编号回查。
"""

from __future__ import annotations

from decimal import Decimal
from typing import Any

from app.application.ports.workbench import WorkbenchStore
from app.application.workbench.records import ProjectRecords
from app.application.workbench.run_reader import RunReader, is_fact_ref
from app.domain.workbench import compute as calc
from app.domain.workbench.errors import ComputeRefused

COMPUTE_EVENT = "knowbot/compute"
MAX_NAMES = 50
MAX_NAME_CHARS = 200


async def computations(repo: WorkbenchStore, task_id: str) -> dict[str, dict[str, Any]]:
    """这件委托的计算记录：编号 → 记录。"""
    events = await repo.list_task_events(task_id, types=(COMPUTE_EVENT,))
    return {f"c{e['cursor']}": e["payload"] for e in events}


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
            raise ComputeRefused(
                f"round must be a whole number from 0 to {calc.MAX_ROUND}"
            )
        refs: dict[str, list[str]] = {}
        lists: set[str] = set()
        for key, value in inputs.items():
            if not isinstance(key, str) or not calc.NAME.match(key):
                raise ComputeRefused(
                    f"input name {str(key)[:40]!r} must be lower-case letters, digits or _"
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
        total = sum(len(v) for v in refs.values())
        if total > calc.MAX_INPUTS:
            raise ComputeRefused(f"at most {calc.MAX_INPUTS} inputs in one computation")

        flat = [i for v in refs.values() for i in v]
        earlier_ids = [i for i in flat if calc.COMPUTE_ID.match(i)]
        fact_refs = [i for i in flat if is_fact_ref(i)]
        for item in flat:
            if not calc.COMPUTE_ID.match(item) and not is_fact_ref(item):
                raise ComputeRefused(
                    f"not a fact ref or an earlier id: {item[:80]!r} "
                    "(write fact:… from query_facts, or c17)"
                )
        earlier = (
            await computations(self.repo, str(working["id"])) if earlier_ids else {}
        )
        if fact_refs and self.reader is None:
            raise ComputeRefused("facts cannot be read here")
        facts = (
            await self.reader.facts(self.repo, working, fact_refs)
            if fact_refs and self.reader is not None
            else {}
        )

        def value_of(item: str) -> Decimal:
            if calc.COMPUTE_ID.match(item):
                record = earlier.get(item)
                if record is None:
                    raise ComputeRefused(f"no earlier computation {item} in this task")
                found = calc.decimal_of(record.get("value"))
                if found is None:
                    raise ComputeRefused(f"computation {item} has no value")
                return found
            return facts[item]

        values: dict[str, calc.Value] = {}
        recorded = []
        for key, items in refs.items():
            numbers = [value_of(i) for i in items]
            values[key] = numbers if key in lists else numbers[0]
            entry: dict[str, Any] = {"name": key}
            if key in lists:
                entry |= {"refs": items, "values": [calc.text_of(n) for n in numbers]}
            else:
                entry |= {"ref": items[0], "value": calc.text_of(numbers[0])}
            recorded.append(entry)
        result = calc.text_of(calc.rounded(calc.evaluate(expression, values), places))

        payload = {
            "name": name.strip(),
            "expression": expression.strip(),
            "inputs": recorded,
            "round": places,
            "value": result,
        }
        async with self.repo.transaction():
            event = await self.repo.append_event(
                session_id=str(working["session_id"]),
                kind="records",
                event_type=COMPUTE_EVENT,
                payload=payload,
                task_id=str(working["id"]),
            )
        return {
            "id": f"c{event['cursor']}",
            "name": payload["name"],
            "value": result,
            "formula": payload["expression"],
            "inputs": recorded,
        }
