"""Exercise timer replay against the same ordinary expert that just delivered."""

import asyncio
import hashlib
from datetime import datetime
from types import SimpleNamespace
from uuid import uuid4

import pytest
from test_workbench_ledger_db import OTHER, OWNER

from app.application.workbench.expert_duties import wake
from app.domain.workbench.errors import StaleStateVersion
from app.interfaces.http.web import workbench_expert as routes


async def verify_period(
    db, patch, client, path, result, reference, qualified=None, *, owner=OWNER
):
    class Area:
        live = True
        hold = False

        def __init__(self):
            self.writes = []
            self.pin = None

        async def write_output(self, **args):
            self.writes.append(args)
            if self.hold:
                started.set()
                await resume.wait()
            assert args["content"] == result["delivery"][output].encode()
            self.pin = self.pin or {
                "material_id": str(uuid4()),
                "version_no": 1,
                "sha256": hashlib.sha256(args["content"]).hexdigest(),
            }
            return {
                **self.pin,
                "provenance_basis": {
                    "task_window": {
                        "started_at": args["task_started_at"],
                        "finished_at": args["task_finished_at"],
                    }
                },
            }

        async def find_output(self, **args):
            assert args.pop("require_available")
            assert all(self.writes[0][key] == value for key, value in args.items())
            return self.pin if self.live else {}

    started, resume = asyncio.Event(), asyncio.Event()
    area = Area()
    patch.setattr(routes, "knowbase_v2", lambda: area)
    patch.setattr(
        routes,
        "get_settings",
        lambda: SimpleNamespace(workbench_output_write_enabled=True),
    )
    origin = path.rsplit("/", 1)[1]

    output = next(iter(result["delivery"]))
    body = {
        "instruction": "Please return a receipt",
        "interval_hours": 1,
        "output": output,
    }
    created = await client.put(path + "/duty", json=body)
    assert created.status_code == 200, created.text
    assert created.json()["config"]["reference"] == reference.model_dump(mode="json")
    period = datetime.fromisoformat(created.json()["next_at"])

    async def trigger(actor=owner):
        async with db() as session:
            svc = routes.find_expert_service(session)
            svc.now = lambda: period
            return await wake(svc, actor, origin, period, area)

    assert await trigger() is None
    wrong = await client.put(path + "/duty/output", json={"execution_id": str(uuid4())})
    assert wrong.status_code == 409 and not area.writes
    release = {"execution_id": result["execution"]["task_id"]}
    area.hold = True
    pending = asyncio.create_task(client.put(path + "/duty/output", json=release))
    try:
        async with asyncio.timeout(8):
            await started.wait()
            withdrawn = await client.put(path + "/duty/withdrawal", json=release)
            withdrawn.raise_for_status()
    finally:
        area.hold = False
        resume.set()
        superseded = await pending
    assert superseded.status_code == 409
    area.writes.clear()
    a, b = await asyncio.gather(
        *(client.put(path + "/duty/output", json=release) for _ in range(2))
    )
    assert a.status_code == b.status_code == 200, (a.text, b.text)
    assert a.json() == b.json() and len(area.writes) == 1
    (await client.put(path + "/duty/state", json={"paused": True})).raise_for_status()
    assert await trigger() is None
    (await client.put(path + "/duty/state", json={"paused": False})).raise_for_status()
    (await client.put(path + "/duty/withdrawal", json=release)).raise_for_status()
    assert await trigger() is None
    (await client.put(path + "/duty/output", json=release)).raise_for_status()
    area.live = False
    with pytest.raises(StaleStateVersion):
        await trigger()
    area.live = True
    from app.domain.workbench.errors import NotFound

    with pytest.raises(NotFound):
        await trigger(OTHER if owner == OWNER else OWNER)
    if qualified is not None:
        qualified.remove(reference)
        with pytest.raises(StaleStateVersion):
            await trigger()
        qualified.add(reference)
    async with db() as session:
        repo = routes.find_expert_service(session).repo
        due = await repo.due_duties(period)
        assert [(str(row["origin"]), row["next_at"]) for row in due] == [
            (origin, period)
        ]
    records = await asyncio.gather(trigger(), trigger())
    assert sum(item is not None for item in records) == 1
    new = next(item for item in records if item)
    assert new["task_id"] != origin
    restored = (await client.get(path + "/duty")).json()
    assert restored["current_match"] == new["task_id"] and restored["approved"] is None
    opened = (await client.get("/api/workbench/find-expert/" + new["task_id"])).json()
    assert opened["followup"]["released"]["material"] == area.pin
    async with db() as session:
        repo = routes.find_expert_service(session).repo
        assert not await repo.due_duties(period)
        task = await repo.get_task(new["task_id"], owner_actor_id=owner)
        assert task["original_input"]["input"]["reference"] == reference.model_dump(
            mode="json"
        )
    assert await trigger() is None
