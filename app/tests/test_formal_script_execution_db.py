"""Qualify the script through the author-facing execution/approval/registration path."""

import asyncio
import base64
import hashlib
import json
import os
from copy import deepcopy
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from uuid import uuid4

import pytest
from formal_plan_support import freeze
from method_access_support import ACCESS
from test_capabilities_db import CIPHER, OPERATION, OWNER, Remote, service
from test_formal_execution_db import approve, settle, status
from test_formal_method_admission_db import setup as admission_setup
from test_formal_method_admission_db import validation_request
from test_skill_package import archive
from test_skill_package_db import package
from test_source_licenses import license_record
from test_workbench_ledger_db import db as db
from test_workbench_ledger_db import seed

from app.application.workbench.capabilities import Capabilities
from app.application.workbench.formal_method_catalog import FormalMethodCatalog
from app.application.workbench.formal_test_runs import FormalExecutions
from app.application.workbench.test_runs import KnowbotTestConductor
from app.bootstrap.workbench import workbench_stores
from app.domain.workbench.capabilities import MethodPermissionRevoke
from app.domain.workbench.formal_method_execution_generated import (
    FormalMethodExecutionRequest,
    FormalTestSuiteDraft,
)
from app.domain.workbench.script_contract_generated import ScriptContainerMeter
from app.domain.workbench.script_pricing import ScriptTariff
from app.infrastructure.workbench.repository import WorkbenchRepository
from app.infrastructure.workbench.skill_executor import SkillExecutor

TARIFF = ScriptTariff(
    environment="isolated-test",
    revision=1,
    destination="https://isolated.example.test",
    provisioner_url="http://localhost",
    currency="CNY",
    per_container_second="0.01",
    evidence_reference="test-local",
    evidence_digest="sha256:" + "1" * 64,
)
SOURCE = """import json, os, time
from pathlib import Path
time.sleep(1)
value = json.loads((Path(os.environ["SUNMOON_INPUT_DIR"])/"arguments.json").read_text())
(Path(os.environ["SUNMOON_OUTPUT_DIR"])/"result.json").write_text(json.dumps({"receipt":value["to"]}))
"""


class Executor:
    def __init__(self):
        self.calls = []
        self.receipts = {}
        self.meters = {}

    async def prepare(self, owner, run, body):
        pass

    async def start(self, owner, run, body):
        self.calls.append(run)
        args = json.loads(base64.b64decode(body["inputs"][0]["data_base64"]))
        raw = json.dumps({"receipt": args["to"]}).encode()
        self.receipts[run] = {
            "state": "finished",
            "result": {
                "exit_code": 0,
                "stdout": "diagnostic",
                "stderr": "",
                "timed_out": False,
                "output_truncated": False,
                "artifacts": [
                    {
                        "path": "result.json",
                        "sha256": hashlib.sha256(raw).hexdigest(),
                        "data_base64": base64.b64encode(raw).decode(),
                    }
                ],
            },
        }
        begin = datetime.now(UTC)
        self.meters[run] = ScriptContainerMeter(
            run_id=run,
            job_uid=uuid4(),
            pod_uid=uuid4(),
            container_id="test/container",
            started_at=begin,
            finished_at=begin + timedelta(seconds=4),
        )

    async def status(self, owner, run):
        return self.receipts.get(run, {"state": "absent"})

    async def container_meter(self, owner, run):
        return self.meters[run]

    async def cancel(self, owner, run):
        pass

    async def stop_metered(self, owner, run):
        pass


async def script_setup(
    db, executor=None, *, limit="1", public_review=False, registered=False
):
    await admission_setup(db)
    env, _, _ = await seed(db)
    executor = executor or Executor()
    async with db() as session:
        repo = WorkbenchRepository(session)
        svc = service(repo)
        if registered:
            from app.domain.workbench.registry_sources import SourceDraft

            source_id = uuid4()
            async with repo.transaction():
                await repo.register_direct_revision(
                    OWNER,
                    source_id,
                    1,
                    SourceDraft(kind="script", name="calculate.py", content=SOURCE),
                )
            source = next(
                x
                for x in (await svc.formal_sources.sources("author"))["items"]
                if x["kind"] == "registered_script"
            )
            record = None
        else:
            await svc.create_skill(
                OWNER,
                package().model_copy(
                    update={
                        "archive_base64": archive(
                            {"SKILL.md": "Run", "calculate.py": SOURCE}
                        )
                    }
                ),
                package=True,
            )
            source = next(
                x
                for x in (await svc.formal_sources.sources("author"))["items"]
                if x["kind"] == "skill_script"
            )
            terms = {"origin": "test/skill", "notices": ["Test package"]}
            record = license_record(
                source,
                rights={"execute": True, "deliver": public_review},
                delivery_terms=terms if public_review else None,
            )
            if public_review:
                source["delivery_terms"] = terms
            source["license_basis"] = record.basis
    doc = validation_request(source).model_dump(mode="json", by_alias=True)
    access = deepcopy(ACCESS)
    access.update(
        capability_ref="isolated-script", operations=["execute"], resources=[]
    )
    access["data_transfer"]["destination"] = TARIFF.destination
    doc["draft"]["definition"]["access_requirements"] = [access]
    doc["draft"]["definition"]["procedure"]["implementation"]["requirement_ids"] = [
        access["requirement_id"]
    ]
    suite = FormalTestSuiteDraft.model_validate(
        {
            "suite_id": str(uuid4()),
            "version": 1,
            "cases": [
                {
                    "case_id": "calculate",
                    "input": {"values": {"to": "example"}},
                    "repetitions": 1,
                    "minimum_passes": 1,
                    "expected": {
                        "outcome": "succeeded",
                        "assertions": [
                            {
                                "kind": "equals",
                                "pointer": "/receipt",
                                "value": "example",
                            }
                        ],
                    },
                }
            ],
        }
    ).freeze()
    doc.update(
        contract="knowlab/formal-method-execution/v1",
        execution={
            "suite": suite.model_dump(mode="json"),
            "runner": {
                "kind": "user",
                "environment_id": env,
                "workspace_root": "/home/u/research",
            },
            "budget": {"limit": limit, "currency": "CNY"},
            "timeout_seconds": 60,
            "max_wait_seconds": 120,
        },
    )
    req = FormalMethodExecutionRequest.model_validate(doc)

    from app.domain.workbench.source_licenses import BusinessCoverageReview

    reviews = (
        (
            BusinessCoverageReview(
                subject="author",
                method_id=req.method_id,
                definition_digest=req.draft.definition.digest(),
                source_digest=req.draft.source.fingerprint(),
                suite_digest=suite.digest,
                evidence=record.evidence,
                expires_at=record.expires_at,
            ),
        )
        if public_review and record
        else ()
    )

    def factory(repo):
        return Capabilities(
            repo,
            Remote(),
            CIPHER,
            operations=(OPERATION,),
            script_executor=executor,
            script_tariff=TARIFF.model_copy(update={"provisioner_url": executor.url})
            if isinstance(executor, SkillExecutor)
            else TARIFF,
            source_licenses=(record,) if record else (),
            business_reviews=reviews,
        )

    conductor = KnowbotTestConductor(workbench_stores(db), formal_capabilities=factory)
    async with db() as session:
        repo = WorkbenchRepository(session)
        await FormalExecutions(repo, factory(repo)).accept(req, caller="knowlab")
    return req, factory, conductor, executor


async def test_formal_script_charges_root_and_freezes_after_a_real_approval(
    db, monkeypatch
):
    req, factory, conductor, executor = await script_setup(db)
    await conductor.advance(str(req.run_id))
    action = await status(db, req, factory)
    from types import SimpleNamespace
    from uuid import UUID

    from app.domain.workbench.capabilities import ActionDecision
    from app.interfaces.endpoints import capability_routes

    pending = action["actions"][0]
    monkeypatch.setattr(
        capability_routes,
        "method_read_service",
        lambda s: factory(WorkbenchRepository(s)),
    )
    async with db() as session:
        await capability_routes.decide_call(
            UUID(pending["call_id"]),
            ActionDecision(
                decision="accept",
                token=pending["token"],
                subject_digest=pending["subject_digest"],
            ),
            SimpleNamespace(actor_id=OWNER),
            factory(WorkbenchRepository(session, browser_view=True)),
        )
    async with db() as session:
        svc = factory(WorkbenchRepository(session))
        await svc.reconcile_scripts()
    await settle(conductor, req)
    result = await status(db, req, factory)
    assert result["status"] == "passed", json.dumps(result, default=str)
    frozen = freeze(req, result)
    async with db() as session:
        repo = WorkbenchRepository(session)
        svc = factory(repo)
        await svc.formal_sources.register(frozen, caller="knowlab")
        from app.application.workbench.tool_registry import (
            formal_registration,
            installed_registration,
        )

        implementation = await formal_registration(repo, OWNER, frozen.reference)
        assert (implementation.method_kind, implementation.runner_route) == (
            "tool",
            "formal_script",
        )
        installation = await installed_registration(
            repo, OWNER, str(implementation.source_asset_id)
        )
        assert (
            implementation.source_kind == "skill_script"
            and implementation.source_entry == req.draft.source.entry
        )
        assert installation.revision == implementation.source_revision
        assert [(entry.kind, entry.entry) for entry in installation.entries] == [
            ("skill_instruction", "SKILL.md"),
            ("skill_script", "skill_run"),
        ]
        got = await FormalMethodCatalog(
            repo, svc.formal_sources, svc.operations
        ).resolve_owned(OWNER, frozen.reference)
        assert got.frozen == frozen
        ledger = await repo.script_budget_entries(executor.calls[0])
        assert str(ledger["consume"]["amount"]) == "0.040000"


async def collect_script(db, factory):
    # Restart the service for each poll and recover the original call from SQL.
    async with asyncio.timeout(120):
        while True:
            async with db() as session:
                repo = WorkbenchRepository(session)
                await factory(repo).reconcile_scripts()
                if not await repo.pending_metered_scripts():
                    return
            await asyncio.sleep(0.2)


async def mixed_expert(
    db, behavior, executor=None, *, shared_connector=None, registered=False
):
    from formal_plan_support import EchoRemote
    from test_capabilities_db import SCHEMA, URL, body
    from test_formal_host_calls_db import FORMAL_OPERATION

    from app.domain.workbench.tool_registry import ToolRegistration

    req, script_factory, _, executor = await script_setup(
        db, executor, registered=registered
    )
    async with db() as session:
        repo = WorkbenchRepository(session)
        script_svc = script_factory(repo)
        assets = (await script_svc.formal_sources.sources("author"))["items"]
        if registered:
            from app.domain.workbench.registry_sources import SourceDraft

            source_id = req.draft.source.source
            # Revising the author's script must not retarget the saved v1 draft.
            async with repo.transaction():
                await repo.register_direct_revision(
                    OWNER,
                    source_id,
                    2,
                    SourceDraft(
                        kind="script",
                        name="calculate.py",
                        content="raise ValueError('wrong revision')",
                    ),
                )
        script_records = [
            license_record(asset) for asset in assets if asset["kind"] == "skill_script"
        ]
        remote = shared_connector or EchoRemote()
        lookup = {
            "name": "lookup",
            "description": "Read one synthetic record",
            "inputSchema": SCHEMA,
        }
        source_url = "https://records.example.test/mcp"
        if shared_connector is None:
            remote.tools = [lookup]
            remote.allowed_urls = (URL, source_url)
        else:
            source_url = URL
        read_policy = FORMAL_OPERATION.model_copy(
            update={
                "service_url": source_url,
                "tool_name": "lookup",
                "descriptor_digest": ToolRegistration.discover(
                    "mcp", source_url, lookup
                ).descriptor_digest,
                "effect": "read",
                "operation": "read",
                "capability_ref": "synthetic-records",
            }
        )
        if shared_connector is not None:
            read_policy = read_policy.model_copy(
                update={
                    "read_result_id_field": "receipt",
                    "read_result_fields": ("receipt",),
                }
            )
        operations = (FORMAL_OPERATION, read_policy)
        svc = Capabilities(repo, remote, CIPHER, operations=operations)
        if shared_connector is None:
            await svc.create_connection(
                OWNER,
                body(key="read-connection").model_copy(update={"url": source_url}),
            )
        else:
            from app.domain.workbench.capabilities import AssetAction

            original = next(asset for asset in assets if asset["entry"] == "send")
            await svc.action(
                OWNER,
                original["asset_id"],
                AssetAction(revision=original["asset_revision"], action="refresh"),
            )
        sources = (await svc.formal_sources.sources("author"))["items"]
        source_read = next(a for a in sources if a["entry"] == "lookup")
        source_send = next(a for a in sources if a["entry"] == "send")
        records = [
            *script_records,
            license_record(source_send, record_id="send-license"),
            license_record(source_read, record_id="lookup-license"),
        ]

    def factory(repo):
        return Capabilities(
            repo,
            remote,
            CIPHER,
            operations=operations,
            script_executor=executor,
            script_tariff=TARIFF.model_copy(update={"provisioner_url": executor.url})
            if isinstance(executor, SkillExecutor)
            else TARIFF,
            source_licenses=records,
        )

    conductor = KnowbotTestConductor(workbench_stores(db), formal_capabilities=factory)
    leaves = []
    for carrier in (source_read, None, source_send):
        doc = req.model_dump(mode="json", by_alias=True)
        if carrier:
            carrier = {
                **carrier,
                "license_basis": license_record(
                    carrier, record_id=carrier["entry"] + "-license"
                ).basis,
            }
            doc.update(
                validation_request(carrier).model_dump(mode="json", by_alias=True)
            )
            doc["contract"] = req.contract
            access = deepcopy(ACCESS)
            if carrier["entry"] == "lookup":
                access.update(
                    capability_ref=read_policy.capability_ref, operations=["read"]
                )
                access["data_transfer"]["destination"] = source_url
            doc["draft"]["definition"]["access_requirements"] = [access]
            doc["draft"]["definition"]["procedure"]["implementation"][
                "requirement_ids"
            ] = [access["requirement_id"]]
        case = doc["execution"]["suite"]["cases"][0]
        case["input"]["values"]["to"] = "test@example.test"
        case["expected"]["assertions"][0]["value"] = "test@example.test"
        suite = dict(doc["execution"]["suite"])
        suite.pop("digest")
        suite["suite_id"] = str(uuid4())
        doc["execution"]["suite"] = (
            FormalTestSuiteDraft.model_validate(suite).freeze().model_dump(mode="json")
        )
        doc.update(run_id=str(uuid4()))
        leafreq = FormalMethodExecutionRequest.model_validate(doc)
        async with db() as session:
            repo = WorkbenchRepository(session)
            await FormalExecutions(repo, factory(repo)).accept(
                leafreq, caller="knowlab"
            )
        await conductor.advance(str(leafreq.run_id))
        await approve(db, leafreq, factory, await status(db, leafreq, factory))
        await collect_script(db, factory)
        await settle(conductor, leafreq)
        res = await status(db, leafreq, factory)
        assert res["status"] == "passed", res
        leaf = freeze(leafreq, res)
        async with db() as session:
            repo = WorkbenchRepository(session)
            await factory(repo).formal_sources.register(leaf, caller="knowlab")
        leaves.append(leaf)
    steps = []
    ceiling = []
    for name, leaf in zip(("lookup", "calculate", "send"), leaves, strict=True):
        definition = leaf.draft.definition.document()
        access = deepcopy(definition["access_requirements"][0])
        access["requirement_id"] = name
        ceiling.append(access)
        proc = definition["procedure"]
        prior = steps[-1]["id"] if steps else None
        steps.append(
            {
                "id": name,
                "candidates": [leaf.reference.model_dump(mode="json")],
                "select_when": "Only candidate",
                "required_when": "Always",
                "always_required": True,
                "depends_on": [prior] if prior else [],
                "inputs": {
                    "to": {
                        "slot": proc["inputs"]["to"],
                        "binding": {
                            "source": "step",
                            "step": prior,
                            "output": "receipt",
                        }
                        if prior
                        else {"source": "caller", "name": "to"},
                    }
                },
                "outputs": {"receipt": proc["outputs"]["receipt"]},
                "on_failure": "fail",
            }
        )
    definition["procedure"] = {
        "kind": "orchestration",
        "instructions": "Read, calculate, send",
        "steps": steps,
        "exports": {
            "receipt": {"step": "send", "output": "receipt", "always_present": True}
        },
        "max_depth": 2,
        "max_expansions": 4,
        "max_steps": 3,
        "max_replans": 0,
    }
    definition["access_requirements"] = ceiling
    doc.update(
        run_id=str(uuid4()),
        method_id=str(uuid4()),
        request_id=str(uuid4()),
        workdraft_id=str(uuid4()),
        dependencies=[x.reference.model_dump(mode="json") for x in leaves],
        draft={"allowed_callers": ["host"], "source": None, "definition": definition},
    )
    expert = FormalMethodExecutionRequest.model_validate(doc)
    async with db() as session:
        repo = WorkbenchRepository(session)
        await FormalExecutions(repo, factory(repo)).accept(expert, caller="knowlab")
    executor.calls.clear()
    remote.calls.clear()
    for i in range(3):
        await settle(conductor, expert)
        stage = await status(db, expert, factory)
        assert len(stage["actions"]) == 1, stage
        if behavior == "decline" and i == 1:
            await approve(db, expert, factory, stage, "decline")
            break
        if i == 2 and behavior == "lost_send":
            remote.fail = True
        await approve(db, expert, factory, stage)
        if i == 1:
            cid = stage["actions"][0]["call_id"]
            async with db() as session:
                repo = WorkbenchRepository(session)
                svc = factory(repo)
                call = await repo.capability_call(OWNER, cid)
                if behavior == "revoke":
                    sid = call["session_id"]
                    view = await svc.selection(OWNER, sid)
                    await svc.revoke_method_permissions(
                        OWNER,
                        sid,
                        MethodPermissionRevoke(
                            revision=view["revision"],
                            method_context=view["method"]["context"],
                        ),
                    )
                elif behavior == "cancel":
                    await svc.cancel_script(OWNER, cid)
            conductor = KnowbotTestConductor(
                workbench_stores(db), formal_capabilities=factory
            )
            await collect_script(db, factory)
            await collect_script(db, factory)
            async with db() as session:
                repo = WorkbenchRepository(session)
                entries = await repo.script_budget_entries(cid)
                usage = entries["consume"]["script_evidence"]
                assert usage["meter"]["run_id"] == cid
                assert (
                    entries["consume"]["amount"]
                    == Decimal(usage["seconds"]) * TARIFF.per_container_second
                )
                assert entries["consume"]["amount"] > 0
                if isinstance(executor, Executor):
                    assert str(entries["consume"]["amount"]) == "0.040000"
                if behavior in ("revoke", "cancel"):
                    call = await repo.capability_call(OWNER, cid)
                    assert (
                        call["status"] == "failed"
                        and "artifacts" not in call["detail"]["result"]
                    )
                    break
    await settle(conductor, expert)
    result = await status(db, expert, factory)
    if behavior == "decline":
        assert (
            result["status"] == "failed"
            and not executor.calls
            and [c[2] for c in remote.calls] == ["lookup"]
        )
    elif behavior == "lost_send":
        restarted = KnowbotTestConductor(
            workbench_stores(db), formal_capabilities=factory
        )
        await settle(restarted, expert)
        assert result["status"] == "failed", result
        assert [c[2] for c in remote.calls] == ["lookup", "send"] and len(
            executor.calls
        ) == 1
        async with db() as session:
            repo = WorkbenchRepository(session)
            original = await repo.capability_call(OWNER, stage["actions"][0]["call_id"])
            assert original["status"] == "unknown"
    elif behavior in ("revoke", "cancel"):
        assert result["status"] == "failed" and [c[2] for c in remote.calls] == [
            "lookup"
        ]
        assert len(executor.calls) == 1
    else:
        assert result["status"] == "passed", json.dumps(result, default=str)
        assert [c[2] for c in remote.calls] == ["lookup", "send"] and len(
            executor.calls
        ) == 1
        assert remote.calls[-1][3] == {"to": "test@example.test"}
        frozen = freeze(expert, result)
        async with db() as session:
            repo = WorkbenchRepository(session)
            svc = factory(repo)
            await svc.formal_sources.register(frozen, caller="knowlab")
            from app.application.workbench.tool_registry import formal_registration
            from app.domain.workbench.registry_views import FormalRegistration

            detail = await formal_registration(repo, OWNER, frozen.reference)
            listing = await repo.registry_inventory(OWNER)
            registered = [
                FormalRegistration.model_validate(row) for row in listing["formal"]
            ]
            assert detail in registered
            assert (detail.method_kind, detail.runner_route) == (
                "expert",
                "orchestration",
            )
            assert detail.source_asset_id is None and detail.dependencies
            await FormalMethodCatalog(
                repo, svc.formal_sources, svc.operations
            ).resolve_owned_admission(
                OWNER,
                frozen.reference,
                resolved={
                    (x.reference.method_id, x.reference.version): x for x in leaves
                },
            )

        if registered:
            return expert, frozen, leaves, factory, conductor
        if shared_connector is not None:
            assert source_read["asset_id"] == source_send["asset_id"]
            return source_send["asset_id"], operations, result["cases"][0]["task_id"]


async def test_registered_script_confirmation_requires_same_bytes(db):
    from sqlalchemy import text

    from app.domain.workbench.capabilities import CapabilityError

    req, factory, conductor, executor = await script_setup(db, registered=True)
    await conductor.advance(str(req.run_id))
    result = await status(db, req, factory)
    subject = result["actions"][0]["subject"]
    assert subject["registered_source"] == req.draft.source.model_dump(mode="json")
    async with db() as session, session.begin():
        await session.execute(
            text(
                "UPDATE workbench_registry_sources SET content=:content WHERE source_id=:source AND version=1"
            ),
            {"source": req.draft.source.source, "content": "print('changed')"},
        )
    with pytest.raises(CapabilityError, match="pinned digest"):
        await approve(db, req, factory, result)
    assert not executor.calls


async def test_registered_script_in_private_fixed_expert(db):
    class PinnedExecutor(Executor):
        async def start(self, owner, run, body):
            (file,) = body["files"]
            assert (
                file["path"] == "calculate.py"
                and base64.b64decode(file["data_base64"]) == SOURCE.encode()
            )
            await super().start(owner, run, body)

    from formal_recursive_support import parent_request

    expert, child, leaves, factory, conductor = await mixed_expert(
        db, "success", executor=PinnedExecutor(), registered=True
    )
    from app.application.workbench.tool_registry import formal_registration
    from app.domain.workbench.errors import Forbidden

    async with db() as session:
        repo = WorkbenchRepository(session)
        revision = await formal_registration(repo, OWNER, leaves[1].reference)
        assert revision.source_asset_id is None and revision.source_revision == 1
        assert (
            revision.runner_route == "formal_script"
            and revision.source_kind == "registered_script"
        )
        with pytest.raises(Forbidden, match="delivery rights"):
            await factory(repo).formal_sources.delivery(
                "author", leaves[1].reference, market=False
            )
    req = parent_request(expert, [child], depth=3, expansions=5, steps=4)
    async with db() as session:
        repo = WorkbenchRepository(session)
        await FormalExecutions(repo, factory(repo)).accept(req, caller="knowlab")
    for name in ("lookup", "calculate", "send"):
        await settle(conductor, req)
        run = await status(db, req, factory)
        if name == "calculate":
            (action,) = run["actions"]
            assert action["subject"]["registered_source"] == leaves[
                1
            ].draft.source.model_dump(mode="json")
        await approve(db, req, factory, run)
        await collect_script(db, factory)
    await settle(conductor, req)
    run = await status(db, req, factory)
    async with db() as session:
        repo = WorkbenchRepository(session)
        evidence = (await repo.formal_validation(OWNER, str(req.run_id)))[
            "execution_evidence"
        ]["cases"][0]
        assert run["status"] == "passed", evidence["errors"]
        assert [s["path"] for s in evidence["steps"]] == [
            ["first", n] for n in ("lookup", "calculate", "send")
        ]
        svc = factory(repo)
        frozen = freeze(req, run)
        await svc.formal_sources.register(frozen, caller="knowlab")
        deps = {
            (v.reference.method_id, v.reference.version): v for v in [*leaves, child]
        }
        assert (
            await FormalMethodCatalog(
                repo, svc.formal_sources, svc.operations
            ).resolve_owned_admission(OWNER, frozen.reference, resolved=deps)
            == frozen
        )


@pytest.mark.parametrize(
    "behavior", ["success", "decline", "revoke", "cancel", "lost_send"]
)
async def test_three_admitted_steps_and_exactly_one_script_charge(db, behavior):
    await mixed_expert(db, behavior)


@pytest.mark.skipif(
    not os.environ.get("SCRIPT_TEST_PROVISIONER_URL"),
    reason="requires local isolated provisioner",
)
async def test_real_container_in_three_step_expert(db):
    class RecordedExecutor(SkillExecutor):
        def __init__(self):
            super().__init__(
                os.environ["SCRIPT_TEST_PROVISIONER_URL"],
                os.environ["SCRIPT_TEST_PROVISIONER_TOKEN"],
            )
            self.calls = []

        async def start(self, owner, run, body):
            self.calls.append(run)
            return await super().start(owner, run, body)

    await mixed_expert(db, "success", RecordedExecutor())


@pytest.mark.parametrize(
    "failure", ["busy", "unstarted", "failed", "missing_meter", "insufficient"]
)
async def test_script_budget_failure_paths(db, failure):
    from app.domain.workbench.capabilities import ScriptNotProvisioned
    from app.domain.workbench.errors import BudgetExhausted, TestCleanupPending
    from app.domain.workbench.script_contract_generated import ScriptNotStarted

    class Controlled(Executor):
        async def prepare(self, owner, run, body):
            if failure == "busy":
                raise ScriptNotProvisioned(
                    "slot_occupied_before_creation", "Slot already occupied"
                )

        async def start(self, owner, run, body):
            await super().start(owner, run, body)
            self.receipts[run] = {"state": "failed", "error": "test failure"}
            if failure == "unstarted":
                self.meters[run] = ScriptNotStarted(
                    run_id=run, job_uid=uuid4(), pod_uid=uuid4(), reason="unscheduled"
                )
            if failure == "missing_meter":
                self.meters.pop(run)

    executor = Controlled()
    req, factory, conductor, _ = await script_setup(
        db, executor, limit="0.50" if failure == "insufficient" else "1"
    )
    await conductor.advance(str(req.run_id))
    action = await status(db, req, factory)
    cid = action["actions"][0]["call_id"]
    if failure == "insufficient":
        with pytest.raises(BudgetExhausted):
            await approve(db, req, factory, action)
        async with db() as session:
            assert not await WorkbenchRepository(session).script_budget_entries(cid)
        assert not executor.calls
        return
    await approve(db, req, factory, action)
    async with db() as session:
        repo = WorkbenchRepository(session)
        svc = factory(repo)
        await svc.reconcile_scripts()
        rows = await repo.script_budget_entries(cid)
        if failure == "missing_meter":
            assert set(rows) == {"reserve"}
            await FormalExecutions(repo, svc).cancel(
                "author", str(req.run_id), caller="knowlab"
            )
            with pytest.raises(TestCleanupPending, match="usage"):
                await repo.purge_test_run(str(req.run_id))
            assert len(executor.calls) == 1
            return
        cost = Decimal("0.04") if failure == "failed" else Decimal(0)
        assert rows["consume"]["amount"] == cost
        assert rows["release"]["amount"] + cost == rows["reserve"]["amount"]
        assert len(executor.calls) == (0 if failure == "busy" else 1)
    await settle(conductor, req)
    res = await status(db, req, factory)
    assert res["status"] == "failed" and Decimal(res["spent"]) == cost


@pytest.mark.skipif(
    not os.environ.get("SCRIPT_TEST_PROVISIONER_URL"),
    reason="requires local isolated provisioner",
)
@pytest.mark.parametrize("terminate", ["cancel", "failure"])
async def test_real_container_settlement_after_start(db, monkeypatch, terminate):
    executor = SkillExecutor(
        os.environ["SCRIPT_TEST_PROVISIONER_URL"],
        os.environ["SCRIPT_TEST_PROVISIONER_TOKEN"],
    )
    code = (
        "import time\ntime.sleep(25)"
        if terminate == "cancel"
        else "import time\ntime.sleep(2)\nraise RuntimeError('synthetic failure')"
    )
    monkeypatch.setitem(globals(), "SOURCE", code)
    req, factory, conductor, _ = await script_setup(db, executor)
    await conductor.advance(str(req.run_id))
    action = await status(db, req, factory)
    cid = action["actions"][0]["call_id"]
    await approve(db, req, factory, action)
    if terminate == "cancel":
        await asyncio.sleep(1)
        async with db() as session:
            await factory(WorkbenchRepository(session)).cancel_script(OWNER, cid)
    await collect_script(db, factory)
    async with db() as session:
        repo = WorkbenchRepository(session)
        assert (await repo.capability_call(OWNER, cid))["status"] == "failed"
        entries = await repo.script_budget_entries(cid)
        charged = entries["consume"]
        seconds = Decimal(charged["script_evidence"]["seconds"])
        assert seconds > 0 and (terminate != "cancel" or seconds < 25)
        assert charged["amount"] == seconds * TARIFF.per_container_second
        assert (
            entries["release"]["amount"] + charged["amount"]
            == entries["reserve"]["amount"]
        )
        assert (await executor.status(OWNER, cid))["state"] == "absent"
        print(
            f"{terminate}: {seconds} container seconds, {charged['amount']} CNY on the root task"
        )
    await settle(conductor, req)
    result = await status(db, req, factory)
    assert (
        result["status"] == "failed" and Decimal(result["spent"]) == charged["amount"]
    )


@pytest.mark.parametrize("missing", [False, True])
async def test_stop_waits_for_usage_and_recovers_job_cleanup(db, missing):
    from unittest.mock import AsyncMock

    executor = Executor()
    executor.cancel = AsyncMock(side_effect=[TimeoutError(), None])
    req, factory, conductor, _ = await script_setup(db, executor)
    await conductor.advance(str(req.run_id))
    action = await status(db, req, factory)
    cid = action["actions"][0]["call_id"]
    await approve(db, req, factory, action)
    final = executor.receipts[cid]
    executor.receipts[cid] = {"state": "running"}
    async with db() as session:
        repo = WorkbenchRepository(session)
        svc = factory(repo)
        await svc.cancel_script(OWNER, cid)
        await svc.reconcile_scripts()
        assert (await repo.capability_call(OWNER, cid))["status"] == "dispatching"
        if missing:
            executor.receipts[cid] = {"state": "absent"}
            await svc.reconcile_scripts()
            assert (await repo.capability_call(OWNER, cid))["status"] == "unknown"
        assert set(await repo.script_budget_entries(cid)) == {"reserve"}
    executor.receipts[cid] = final
    async with db() as session:
        repo = WorkbenchRepository(session)
        await factory(repo).reconcile_scripts()
        retained = await repo.capability_call(OWNER, cid)
        assert retained["status"] == ("unknown" if missing else "failed")
        assert retained["detail"]["script_cleanup_pending"]
        assert (await repo.script_budget_entries(cid))["consume"]["amount"] == Decimal(
            "0.04"
        )
    async with db() as session:
        repo = WorkbenchRepository(session)
        svc = factory(repo)
        await svc.reconcile_scripts()
        await svc.reconcile_scripts()
        assert not await repo.pending_metered_scripts()
        events = await repo.list_events(session_id=retained["session_id"])
        completed = [
            e
            for e in events
            if e["type"] == "capability/action_completed"
            and e["payload"]["call_id"] == cid
        ]
        assert len(completed) == 1
        assert len(executor.calls) == 1 and executor.cancel.await_count == 2
