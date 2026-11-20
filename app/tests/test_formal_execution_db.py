"""Real admission→Task→approval→MCP→evaluation→retained receipt, synthetic remote."""

import asyncio
import json
from copy import deepcopy
from uuid import uuid4

import pytest
from method_access_support import ACCESS
from sqlalchemy import text
from test_capabilities_db import OPERATION, OWNER, Remote
from test_formal_host_calls_db import host_service
from test_formal_method_admission_db import setup as admission_setup
from test_source_licenses import license_record
from test_workbench_ledger_db import db as db  # noqa: F401
from test_workbench_ledger_db import seed

from app.application.workbench.formal_test_runs import FormalExecutions
from app.application.workbench.test_runs import KnowbotTestConductor, KnowbotTestService
from app.bootstrap.workbench import workbench_stores
from app.domain.workbench.capabilities import CapabilityError
from app.domain.workbench.errors import Forbidden, IdempotencyConflict, NotFound
from app.domain.workbench.formal_execution import FORMAL_RUNTIME
from app.domain.workbench.formal_method_admission_generated import FrozenFormalMethod
from app.domain.workbench.formal_method_execution_generated import (
    FormalExecutionDecision,
    FormalMethodExecutionRequest,
    FormalTestSuiteDraft,
)
from app.domain.workbench.source_licenses import BusinessCoverageReview
from app.infrastructure.workbench.repository import WorkbenchRepository


class StructuredRemote(Remote):
    async def call(self, url, token, name, arguments):
        result = await super().call(url, token, name, arguments)
        return {**result, "structuredContent": {"receipt": "expected-never-sent"}}


async def setup(db, *, cases=None, remote=None, public_review=False):
    admission, asset = await admission_setup(db, licensed=True)
    env, _, _ = await seed(db)
    doc = admission.model_dump(mode="json", by_alias=True)
    doc["draft"]["definition"]["access_requirements"] = [deepcopy(ACCESS)]
    doc["draft"]["definition"]["procedure"]["implementation"]["requirement_ids"] = [
        ACCESS["requirement_id"]
    ]
    record = license_record(
        doc["draft"]["source"], rights={"execute": True, "deliver": public_review}
    )
    doc["draft"]["source"]["license_basis"] = record.basis
    suite = FormalTestSuiteDraft.model_validate(
        {
            "suite_id": str(uuid4()),
            "version": 1,
            "cases": cases
            or [
                {
                    "case_id": "notice",
                    "input": {"values": {"to": "test@example.test"}},
                    "repetitions": 1,
                    "minimum_passes": 1,
                    "expected": {
                        "outcome": "succeeded",
                        "assertions": [
                            {
                                "kind": "equals",
                                "pointer": "/receipt",
                                "value": "expected-never-sent",
                            }
                        ],
                    },
                }
            ],
        }
    ).freeze()
    request = FormalMethodExecutionRequest.model_validate(
        {
            **doc,
            "contract": FORMAL_RUNTIME,
            "execution": {
                "suite": suite.model_dump(mode="json"),
                "runner": {
                    "kind": "user",
                    "environment_id": env,
                    "workspace_root": "/home/u/research",
                },
                "budget": {"limit": "1", "currency": "CNY"},
                "timeout_seconds": 60,
                "max_wait_seconds": 120,
            },
        }
    )
    review = (
        BusinessCoverageReview(
            subject="author",
            method_id=request.method_id,
            definition_digest=request.draft.definition.digest(),
            source_digest=request.draft.source.fingerprint(),
            suite_digest=suite.digest,
            evidence=record.evidence,
            expires_at=record.expires_at,
        )
        if public_review
        else None
    )
    remote = remote or StructuredRemote()

    def factory(repo):
        capabilities = host_service(repo, record, remote)
        if review:
            capabilities.formal_sources.business_reviews = (review,)
        return capabilities

    conductor = KnowbotTestConductor(workbench_stores(db), formal_capabilities=factory)
    async with db() as s:
        repo = WorkbenchRepository(s)
        result = await FormalExecutions(repo, factory(repo)).accept(
            request, caller="knowlab"
        )
        assert result["status"] == "pending"
    return request, asset, remote, factory, conductor


async def status(db, request, factory):
    async with db() as s:
        repo = WorkbenchRepository(s)
        return await FormalExecutions(repo, factory(repo)).status(
            request.owner_subject, str(request.run_id), caller="knowlab"
        )


async def approve(db, request, factory, result, decision="accept"):
    action = result["actions"][0]
    async with db() as s:
        repo = WorkbenchRepository(s)
        return await FormalExecutions(repo, factory(repo)).decide(
            request.owner_subject,
            str(request.run_id),
            action["call_id"],
            FormalExecutionDecision(
                owner_subject=request.owner_subject,
                case_id=action["case_id"],
                repetition=action["repetition"],
                token=action["token"],
                subject_digest=action["subject_digest"],
                decision=decision,
            ),
            caller="knowlab",
        )


async def settle(conductor, request):
    for _ in range(4):
        await conductor.advance(str(request.run_id))


async def test_real_execution_approves_once_seals_private_receipt_and_survives_cleanup(
    db,
):
    request, _, remote, factory, conductor = await setup(db)
    run_id = str(request.run_id)
    await conductor.advance(run_id)
    pending = await status(db, request, factory)
    assert pending["status"] == "waiting" and len(pending["actions"]) == 1
    assert not remote.calls
    task_id = pending["cases"][0]["task_id"]
    async with db() as s:
        repo = WorkbenchRepository(s)
        task = await repo.get_task(task_id)
        assert "expected-never-sent" not in json.dumps(task["pack_snapshot"])
        assert "expected-never-sent" not in json.dumps(task["original_input"])
        assert task["pack_source"] == "formal-test"
        with pytest.raises(NotFound):
            await KnowbotTestService(repo).status(run_id)
        with pytest.raises(NotFound):
            await KnowbotTestService(repo).cancel(run_id)
    await approve(db, request, factory, pending)
    await settle(conductor, request)
    result = await status(db, request, factory)
    assert result["status"] == "passed", result
    assert result["cases"][0]["outcome"] == "succeeded"
    receipt = result["admission"]["receipt"]
    assert receipt["execution"]["request_digest"] == request.fingerprint()
    assert "business" not in receipt["checks"] and receipt["uncovered"]
    assert len(remote.calls) == 1 and remote.calls[0][3] == {"to": "test@example.test"}
    version = FrozenFormalMethod.model_validate(
        {
            "contract": "knowlab/frozen-formal-method/v1",
            "owner_subject": "author",
            "reference": {
                "method_id": request.method_id,
                "version": 1,
                "digest": request.draft.definition.digest(),
            },
            "draft": request.draft,
            "validation": receipt,
            "visibility": "private",
        }
    )
    async with db() as s:
        repo = WorkbenchRepository(s)
        service = FormalExecutions(repo, factory(repo))
        registered = await service.formal.register(version, caller="knowlab")
        proof = (await repo.formal_validation(OWNER, run_id))["execution_evidence"]
        assert proof["cases"][0]["model_turn_ids"] == []
        assert proof["cases"][0]["governance"]["approved"]
        async with repo.transaction():
            await repo.purge_test_run(run_id)
        assert (await service.status("author", run_id, caller="knowlab"))["admission"][
            "receipt"
        ] == receipt
        assert (await repo.formal_validation(OWNER, run_id))[
            "execution_evidence"
        ] == proof
        assert await service.formal.register(version, caller="knowlab") == registered
    await settle(
        KnowbotTestConductor(workbench_stores(db), formal_capabilities=factory), request
    )
    assert len(remote.calls) == 1


async def test_unknown_external_result_never_replayed_or_admitted(db):
    remote = StructuredRemote()
    remote.fail = True
    request, _, remote, factory, conductor = await setup(db, remote=remote)
    await conductor.advance(str(request.run_id))
    await approve(db, request, factory, await status(db, request, factory))
    await settle(conductor, request)
    result = await status(db, request, factory)
    assert result["status"] == "failed" and result["admission"]["receipt"] is None
    assert result["cases"][0]["outcome"] == "unknown"
    await settle(
        KnowbotTestConductor(workbench_stores(db), formal_capabilities=factory), request
    )
    assert len(remote.calls) == 1


async def test_cancel_before_approval_and_cleanup_guard(db):
    request, _, remote, factory, conductor = await setup(db)
    await conductor.advance(str(request.run_id))
    waiting = await status(db, request, factory)
    async with db() as s:
        repo = WorkbenchRepository(s)
        with pytest.raises(ValueError, match="sealed before cleanup"):
            async with repo.transaction():
                await repo.purge_test_run(str(request.run_id))
        svc = FormalExecutions(repo, factory(repo))
        result = await svc.cancel("author", str(request.run_id), caller="knowlab")
        assert result["status"] == "cancelled" and not result["actions"]
        assert result["admission"]["receipt"] is None
    with pytest.raises(Forbidden):
        await approve(db, request, factory, waiting)
    await settle(conductor, request)
    assert not remote.calls


class PausingRemote(StructuredRemote):
    def __init__(self):
        super().__init__()
        self.started = asyncio.Event()
        self.release = asyncio.Event()

    async def call(self, url, token, name, arguments):
        self.started.set()
        await self.release.wait()
        return await super().call(url, token, name, arguments)


async def test_cancel_during_dispatch_retains_unknown_instead_of_claiming_rollback(db):
    request, _, remote, factory, conductor = await setup(db, remote=PausingRemote())
    await conductor.advance(str(request.run_id))
    waiting = await status(db, request, factory)
    action = asyncio.create_task(approve(db, request, factory, waiting))
    try:
        await asyncio.wait_for(remote.started.wait(), timeout=5)
        async with db() as s:
            repo = WorkbenchRepository(s)
            result = await FormalExecutions(repo, factory(repo)).cancel(
                "author", str(request.run_id), caller="knowlab"
            )
            assert result["status"] == "cancelled"
            assert result["cases"][0]["outcome"] == "unknown"
            assert result["admission"]["receipt"] is None
    finally:
        remote.release.set()
        await action
    await settle(conductor, request)
    assert len(remote.calls) == 1
    final = await status(db, request, factory)
    assert final["cases"][0]["outcome"] == "unknown"
    async with db() as s:
        call = await WorkbenchRepository(s).capability_call(
            OWNER, final["cases"][0]["call_id"]
        )
        assert call["status"] == "succeeded"  # late fact remains available


@pytest.mark.parametrize(
    "result",
    [
        {"content": [{"type": "text", "text": '{"receipt":"expected-never-sent"}'}]},
        {"structuredContent": {"receipt": 123}},
        {"structuredContent": {"receipt": "wrong"}},
        {"structuredContent": {"receipt": "expected-never-sent", "undeclared": 1}},
    ],
)
async def test_text_schema_and_assertion_failures_cannot_mint_a_receipt(db, result):
    class OutputRemote(Remote):
        async def call(self, url, token, name, arguments):
            await super().call(url, token, name, arguments)
            return result

    request, _, remote, factory, conductor = await setup(db, remote=OutputRemote())
    await conductor.advance(str(request.run_id))
    await approve(db, request, factory, await status(db, request, factory))
    await settle(conductor, request)
    final = await status(db, request, factory)
    assert final["status"] == "failed" and final["admission"]["receipt"] is None
    assert len(remote.calls) == 1


async def test_negative_input_case_and_repetition_threshold_require_positive_execution(
    db,
):
    cases = [
        {
            "case_id": "missing",
            "input": {"values": {}},
            "repetitions": 1,
            "minimum_passes": 1,
            "expected": {
                "outcome": "rejected",
                "rejection_code": "input_invalid",
                "assertions": [],
            },
        },
        {
            "case_id": "good",
            "input": {"values": {"to": "test@example.test"}},
            "repetitions": 2,
            "minimum_passes": 1,
            "expected": {
                "outcome": "succeeded",
                "assertions": [
                    {
                        "kind": "equals",
                        "pointer": "/receipt",
                        "value": "expected-never-sent",
                    }
                ],
            },
        },
    ]

    class UnstableRemote(StructuredRemote):
        async def call(self, url, token, name, arguments):
            value = await super().call(url, token, name, arguments)
            if len(self.calls) == 2:
                value["structuredContent"]["receipt"] = "a known but failing answer"
            return value

    request, _, remote, factory, conductor = await setup(
        db, cases=cases, remote=UnstableRemote()
    )
    for _ in range(16):
        await conductor.advance(str(request.run_id))
        result = await status(db, request, factory)
        if result["actions"]:
            await approve(db, request, factory, result)
        if result["status"] in ("passed", "failed"):
            break
    assert result["status"] == "passed", result
    assert [c["status"] for c in result["cases"]] == ["passed", "passed", "failed"]
    assert result["cases"][0]["outcome"] == "rejected" and len(remote.calls) == 2


@pytest.mark.parametrize("approval_wait", [False, True])
async def test_deadline_checked_before_final_pass_and_wait_stops_at_response(
    db, approval_wait
):
    request, _, _, factory, conductor = await setup(db)
    await conductor.advance(str(request.run_id))
    waiting = await status(db, request, factory)
    await approve(db, request, factory, waiting)
    async with db() as s:
        await s.execute(
            text(
                "UPDATE workbench_test_runs SET created_at=now()-interval '110 seconds', started_at=now()-interval '100 seconds' WHERE run_id=:r"
            ),
            {"r": str(request.run_id)},
        )
        if approval_wait:
            await s.execute(
                text(
                    "UPDATE workbench_interactions SET created_at=now()-interval '95 seconds', consumed_at=now()-interval '10 seconds' WHERE id=:i"
                ),
                {"i": waiting["actions"][0]["interaction_id"]},
            )
        await s.commit()
    await settle(conductor, request)
    final = await status(db, request, factory)
    assert final["status"] == ("passed" if approval_wait else "failed"), final
    async with db() as s:
        run = await WorkbenchRepository(s).get_test_run(str(request.run_id))
        assert run["waited_seconds"] == (85 if approval_wait else 0)


async def test_repeated_accept_is_exact_and_uncharged_policy_is_mandatory(db):
    request, _, _, factory, _ = await setup(db)

    async def accept(body, *, reviewed=True):
        async with db() as s:
            repo = WorkbenchRepository(s)
            caps = factory(repo)
            if not reviewed:
                caps = host_service(
                    repo,
                    license_record(request.draft.source.model_dump()),
                    StructuredRemote(),
                    operations=(OPERATION,),
                )
            return await FormalExecutions(repo, caps).accept(body, caller="knowlab")

    results = await asyncio.gather(accept(request), accept(request))
    assert results[0] == results[1]
    changed = request.model_copy(update={"revision": 2})
    with pytest.raises(IdempotencyConflict):
        await accept(changed)
    with pytest.raises(CapabilityError, match="no incremental"):
        await accept(request.model_copy(update={"run_id": uuid4()}), reviewed=False)


@pytest.mark.parametrize("withdrawal", ["source", "cost_policy"])
async def test_final_seal_rechecks_source_and_current_cost_policy(db, withdrawal):
    request, asset, remote, factory, conductor = await setup(db)
    run_id = str(request.run_id)
    await conductor.advance(run_id)
    await approve(db, request, factory, await status(db, request, factory))
    await conductor.advance(run_id)  # settle actual dispatch
    await conductor.advance(run_id)  # score actual output
    if withdrawal == "source":
        async with db() as s:
            await s.execute(
                text(
                    "UPDATE workbench_capability_assets SET status='disabled' WHERE id=:id"
                ),
                {"id": asset["id"]},
            )
            await s.commit()
    else:

        def withdrawn(repo):
            return host_service(
                repo,
                license_record(request.draft.source.model_dump()),
                remote,
                operations=(OPERATION,),
            )

        conductor.formal_capabilities = withdrawn
    await conductor.advance(run_id)
    final = await status(db, request, factory)
    assert final["status"] == "failed" and final["admission"]["receipt"] is None
    assert len(remote.calls) == 1


async def test_market_candidate_uses_real_execution_and_current_review(db):
    request, _, remote, factory, conductor = await setup(db, public_review=True)
    await conductor.advance(str(request.run_id))
    await approve(db, request, factory, await status(db, request, factory))
    await settle(conductor, request)
    result = await status(db, request, factory)
    receipt = result["admission"]["receipt"]
    assert "business" in receipt["checks"] and not receipt["uncovered"]
    assert len(remote.calls) == 1
    version = FrozenFormalMethod.model_validate(
        dict(
            contract="knowlab/frozen-formal-method/v1",
            owner_subject="author",
            reference={
                "method_id": request.method_id,
                "version": 1,
                "digest": request.draft.definition.digest(),
            },
            draft=request.draft,
            validation=receipt,
            visibility="public",
        )
    )
    async with db() as session:
        service = factory(WorkbenchRepository(session)).formal_sources
        await service.register(version, caller="knowlab")
        checked = await service.delivery("author", version.reference, market=True)
        assert checked["reference"] == version.reference.model_dump(mode="json")
        assert "credentials" not in json.dumps(checked)
        with pytest.raises(NotFound):
            await service.delivery("other", version.reference, market=True)
        service.business_reviews = ()
        with pytest.raises(Forbidden, match="coverage"):
            await service.delivery("author", version.reference, market=True)
