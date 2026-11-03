"""Dynamic plan, signed reads, compute receipts and Markdown in one regression.

Real disposable DB and temporary files; scripted model/supplier, no live Wind.
"""

import hashlib
import json
import re
from decimal import Decimal

import pytest
from method_plan_fixtures import proposal
from sqlalchemy import text
from test_method_advisor_db import patch_catalog
from test_method_plans_db import task
from test_workbench_ledger_db import OWNER
from test_workbench_ledger_db import db as db  # noqa: F401

from app.application.workbench.advisor import Advisor
from app.application.workbench.knowbot_compute import COMPUTE_EVENT
from app.application.workbench.records import ProjectRecords
from app.application.workbench.run_files import task_dossier
from app.application.workbench.run_reader import RunReader
from app.application.workbench.tokens import (
    TokenIssuer,
    generate_private_key_pem,
    verify,
)
from app.application.workbench.turn import TurnResult
from app.domain.workbench.method_plan import output_name
from app.domain.workbench.states import TaskState
from app.infrastructure.workbench.repository import (
    SqlWorkbenchStores,
    WorkbenchRepository,
)
from app.interfaces.mcp.workbench_mcp import call

pytestmark = pytest.mark.asyncio
FACT = "fact:11111111-2222-3333-4444-555555555555"
PAGE = "Synthetic statement: the disclosed amount is 1500.25 units."
GRANT = "synthetic-grant-for-this-task"


def evidence_contracts(document):
    for unit in document["method_units"]:
        step = unit["contract"]
        if step["step_id"] not in ("inspect", "report"):
            continue
        step["output_schema"] = {
            "rows": [{"id": "string", "value": "number", "compute_id": "string"}],
            "facts": [{"address": "string", "value": "number"}],
            "quotes": [
                {
                    "required": "string",
                    "quotes": [
                        {"material_id": "string", "page": "number", "text": "string"}
                    ],
                }
            ],
        }
        step["acceptance"] = [
            {"kind": "json_object"},
            {"kind": "no_positioning_advice"},
            {"kind": "required_keys", "keys": ["rows", "facts", "quotes"]},
            *[
                {"kind": "list_min", "path": p, "min_items": 1}
                for p in ("rows", "facts", "quotes")
            ],
            {"kind": "computed", "path": "rows", "expression": "x * 2", "tolerance": 0},
            {"kind": "sourced", "path": "facts", "tolerance": 0},
            {"kind": "quoted", "path": "quotes", "field": "required", "equals": "yes"},
        ]
        step["on_reject"] = "human"
        step["back_to"] = None
        if step["step_id"] == "inspect":
            step["tools"] = ["read_input", "compute"]
        else:
            step["output_file"] = "report.md"
            step["acceptance"].append(
                {
                    "kind": "covers",
                    "path": "rows",
                    "field": "id",
                    "source": "inspect.rows",
                    "source_field": "id",
                }
            )


class DataPort:
    def __init__(self):
        self.issuer = TokenIssuer(generate_private_key_pem())
        self.calls = []

    async def data_mcp(self, token, message):
        claims = verify(
            token,
            self.issuer.public_pem(),
            audience="knowbase-data",
            issuer=self.issuer.issuer,
        )
        name = message["params"]["name"]
        args = message["params"]["arguments"]
        assert claims["sub"] == "synthetic-author" and claims["tools"] == [name]
        assert args["run"] == GRANT
        self.calls.append((name, args))
        if name == "read_facts":
            assert args["refs"] == [FACT]
            content = {"facts": [{"ref": FACT, "value": "1500.25"}]}
        else:
            assert name == "read_input" and args["material_id"] == "synthetic-report"
            content = {"pages": [{"page": 3, "text": PAGE}]}
        return {
            "jsonrpc": "2.0",
            "id": message["id"],
            "result": {"isError": False, "structuredContent": content},
        }


class Files:
    def __init__(self, root):
        self.root = root
        self.checked = []

    async def exists(self, *, owner, directory, path):
        assert owner == OWNER
        self.checked.append(path)
        return (self.root / path).is_file()

    async def digest(self, *, owner, directory, path):
        p = self.root / path
        return hashlib.sha256(p.read_bytes()).hexdigest() if p.is_file() else None


@pytest.mark.parametrize("batch_mode", [False, True])
@pytest.mark.parametrize(
    "fault", [None, "number", "fact", "quote", "empty_quotes", "file"]
)
async def test_selected_methods_deliver_only_verified_values_quotes_and_file(
    db, tmp_path, fault, batch_mode
):
    tid, sid = await task(db)
    await patch_catalog(db, tid, evidence_contracts)
    async with db() as session:
        repo = WorkbenchRepository(session)
        async with repo.transaction():
            await session.execute(
                text(
                    "INSERT INTO auth_user(id, issuer, subject, roles, scopes) VALUES (:id, 'test', 'synthetic-author', '[]', '[]')"
                ),
                {"id": OWNER},
            )
            await repo.insert_task_resolution(
                {
                    "task_id": tid,
                    "resolution_id": "synthetic-resolution",
                    "revision": 1,
                    "consumer": "expert:synthetic@1",
                    "params": {},
                    "grant_ref": GRANT,
                    "status": "ready",
                }
            )
    port = DataPort()
    reader = RunReader(port, port.issuer)
    files = Files(tmp_path)

    class Driver:
        def __init__(self):
            self.prompts = []
            self.output = None

        async def run_turn(self, thread_id, prompt, *, timeout):  # noqa: ASYNC109
            self.prompts.append(prompt)
            n = len(self.prompts)
            if n == 1:
                plan = proposal().model_dump(mode="json")
                plan["steps"][0]["tools"].append("compute")
                answer = plan
            elif n == 2:
                async with db() as session:
                    inputs = {
                        "name": "synthetic double",
                        "expression": "x * 2",
                        "inputs": {"x": FACT},
                    }
                    args = {"task": tid, **inputs}
                    if batch_mode:
                        args = {
                            "task": tid,
                            "batch": [
                                dict(inputs, key="double"),
                                {
                                    "key": "triple",
                                    "name": "synthetic triple",
                                    "expression": "d + x",
                                    "inputs": {"d": "batch:double", "x": FACT},
                                },
                            ],
                        }
                    computed = await call(
                        ProjectRecords(
                            WorkbenchRepository(session), owner_actor_id=OWNER
                        ),
                        "compute",
                        args,
                        reader,
                    )
                if batch_mode:
                    assert computed["results"][1]["value"] == "4500.75"
                    computed = computed["results"][0]
                assert computed["value"] == "3000.5"
                self.output = {
                    "rows": [
                        {
                            "id": "synthetic-double",
                            "compute_id": computed["id"],
                            "value": "3000.5",
                        }
                    ],
                    "facts": [{"address": FACT, "value": "1500.25"}],
                    "quotes": [
                        {
                            "required": "yes",
                            "quotes": [
                                {
                                    "material_id": "synthetic-report",
                                    "page": 3,
                                    "text": PAGE,
                                }
                            ],
                        }
                    ],
                }
                answer = self.output
            else:
                assert n in (3, 4), "must stop after the author-configured rework limit"
                if n == 4:
                    assert "this is rework #1" in prompt
                assert output_name("inspect_1") in prompt
                answer = json.loads(json.dumps(self.output))
                if fault == "number":
                    answer["rows"][0]["value"] = "3001.5"
                if fault == "fact":
                    answer["facts"][0]["value"] = "1500.26"
                if fault == "empty_quotes":
                    answer["quotes"][0]["quotes"] = []
                if fault == "quote":
                    answer["quotes"][0]["quotes"][0]["text"] = "An invented disclosure."
                path = re.search(
                    r"Write the deliverable of this step to `([^`]+)`", prompt
                )[1]
                if fault != "file":
                    dest = tmp_path / path
                    dest.parent.mkdir(parents=True, exist_ok=True)
                    dest.write_text(
                        f"# Synthetic report\n\n3000.5\n\n## Source\n{PAGE} (page 3)\n"
                    )
                    (dest.parent / "交回.md").write_text(
                        "Synthetic orchestration test only.\n"
                    )
            return TurnResult(f"turn-{n}", json.dumps(answer), {}, cost=Decimal("0.01"))

    driver = Driver()
    result = await Advisor(
        SqlWorkbenchStores(db), driver, reader=reader, deliverables=files
    ).drive(tid)
    assert result == (TaskState.SUCCEEDED if fault is None else TaskState.WAITING)
    assert len(driver.prompts) == (3 if fault is None else 4)
    assert any(name == "read_facts" for name, _ in port.calls)
    assert any(name == "read_input" for name, _ in port.calls)
    async with db() as session:
        repo = WorkbenchRepository(session)
        row = await repo.get_task(tid)
        events = await repo.list_task_events(tid, types=(COMPUTE_EVENT,))
        assert len(events) == (2 if batch_mode else 1)
        assert events[0]["payload"]["value"] == "3000.5"
        final = await repo.get_artifact(task_id=tid, name=output_name("report_1"))
        handback = await repo.get_artifact(task_id=tid, name="handback")
        if fault is None:
            assert final["workspace_path"] in files.checked
            assert (
                (tmp_path / final["workspace_path"])
                .read_text()
                .startswith("# Synthetic report")
            )
            assert row["terminal_result_ref"] == handback["id"]
            dossier = await task_dossier(repo, row)
            assert final["workspace_path"] in dossier["files"]
        else:
            assert final is None and handback is None
            assert row["terminal_result_ref"] is None
            failures = await repo.list_events(session_id=sid)
            assert any(e["type"] == "step/rejected" for e in failures)
