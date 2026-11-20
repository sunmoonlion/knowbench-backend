"""Root ledger and one-shot dispatch across independent PostgreSQL sessions."""

import asyncio
import json
from contextlib import asynccontextmanager
from dataclasses import replace
from decimal import Decimal

import pytest
from test_formal_instruction_db import ARGS, ENTRY, install
from test_workbench_ledger_db import OWNER, handover, seed
from test_workbench_ledger_db import db as db

from app.application.workbench.formal_methods import FormalMethods
from app.application.workbench.instruction_requests import InstructionRequests
from app.application.workbench.ledger import Ledger
from app.domain.workbench.capabilities import CapabilityError, digest
from app.domain.workbench.errors import BudgetExhausted, IdempotencyConflict, NotFound
from app.domain.workbench.formal_execution import (
    FORMAL_RUNTIME,
    FormalTaskSnapshot,
    PinnedFormalMethod,
)
from app.domain.workbench.instruction_requests import InstructionRoute, instruction_job
from app.domain.workbench.pricing import Price, PriceList, Usage
from app.domain.workbench.states import AttemptState, TaskState
from app.infrastructure.workbench.repository import WorkbenchRepository

ROUTE = InstructionRoute(
    "local",
    "instruction-test",
    "https://instruction.example.test/v1",
    "local-test",
    16000,
    500,
)
PRICES = PriceList(
    "fixture", "CNY", {"local-test": Price(*(Decimal(s) for s in ("2", "1", "3", "4")))}
)
BILL = Usage(input=1, output=1)


async def setup(db, *, budget="1", credential_url=ROUTE.base_url):
    license, req, asset = await install(db)
    _, sandbox, sid = await seed(db)
    snapshot = FormalTaskSnapshot(
        runtime_contract=FORMAL_RUNTIME,
        run_id=req.run_id,
        case_id="instruction",
        repetition=1,
        request_digest="sha256:" + digest(req.model_dump(mode="json", by_alias=True)),
        method_id=req.method_id,
        draft=req.draft,
        arguments=ARGS,
        definition_digest=req.draft.definition.digest(),
    )
    async with db() as session:
        repo = WorkbenchRepository(session)
        async with repo.transaction():
            cred = await repo.add_credential(
                owner_actor_id=OWNER,
                provider=ROUTE.provider,
                ciphertext="fixture",
                hint="test",
                sandbox_id=sandbox,
                base_url=credential_url,
            )
        led = Ledger(repo)
        task = (
            await led.handover(
                handover(sid, budget=budget),
                owner_actor_id=OWNER,
                snapshot=PinnedFormalMethod(snapshot, "trial-1"),
            )
        )["task_id"]
        await led.transition(task, TaskState.VALIDATING)
        await led.transition(task, TaskState.QUEUED)

    @asynccontextmanager
    async def host(route=ROUTE, licenses=(license,), default_url=""):
        async with db() as session:
            repo = WorkbenchRepository(session)
            yield InstructionRequests(
                FormalMethods(repo, source_licenses=licenses),
                route,
                PRICES,
                default_base_url=default_url,
            )

    job = instruction_job(req, ENTRY, ARGS)
    body = {
        "model": ROUTE.model,
        "stream": True,
        "store": False,
        "input": [
            {
                "type": "message",
                "role": "user",
                "content": [{"type": "input_text", "text": json.dumps(job["input"])}],
            }
        ],
        "text": {"format": {"type": "json_schema", "schema": job["output_schema"]}},
    }
    return host, task, cred, body


async def test_concurrent_requests_charge_failure_once_and_keep_exact_route(db):
    host, task, cred, original = await setup(db)
    async with host() as svc:
        attempt = await svc.open(OWNER, task, caller="knowlab")
        assert Decimal(
            (await svc.repo.get_task(task))["budget"]["reserved"]
        ) == Decimal("0.050000")
    outgoing = []

    async def dispatch():
        async with host() as svc:
            url, body = await svc.claim(
                OWNER,
                task,
                attempt,
                caller="knowlab",
                credential_id=cred["id"],
                turn_id="t",
                body=original,
            )
        outgoing.append((url, body))

    results = await asyncio.gather(dispatch(), dispatch(), return_exceptions=True)
    assert sum(isinstance(r, IdempotencyConflict) for r in results) == 1
    assert outgoing == [
        (ROUTE.base_url + "/responses", {**original, "max_output_tokens": 500})
    ]
    async with host() as svc:
        await svc.receipt(
            OWNER, task, attempt, turn_id="t", response_id="r", usage=BILL
        )
        await Ledger(svc.repo).attempt_transition(attempt, AttemptState.FAILED)
        await svc.receipt(
            OWNER, task, attempt, turn_id="t", response_id="r", usage=BILL
        )
        with pytest.raises(IdempotencyConflict):
            await svc.receipt(
                OWNER, task, attempt, turn_id="t", response_id="other", usage=BILL
            )
        budget = (await svc.repo.get_task(task))["budget"]
        assert Decimal(budget["reserved"]) == 0 and Decimal(budget["used"]) == Decimal(
            "0.000006"
        )
        charges = [
            e for e in await svc.repo.ledger_list(task) if e["entry"] == "consume"
        ]
        assert len(charges) == 1 and str(charges[0]["attempt_id"]) == attempt
        with pytest.raises(IdempotencyConflict):
            await svc.open(OWNER, task, caller="knowlab")


async def test_lost_response_and_cancellation_keep_budget_until_late_bill(db):
    host, task, cred, original = await setup(db)
    async with host() as svc:
        attempt = await svc.open(OWNER, task, caller="knowlab")
        await svc.claim(
            OWNER,
            task,
            attempt,
            caller="knowlab",
            credential_id=cred["id"],
            turn_id="t",
            body=original,
        )
    async with host() as svc:
        with pytest.raises(IdempotencyConflict):
            await svc.claim(
                OWNER,
                task,
                attempt,
                caller="knowlab",
                credential_id=cred["id"],
                turn_id="reconnect",
                body=original,
            )
        ledger = Ledger(svc.repo)
        with pytest.raises(IdempotencyConflict):
            await ledger.attempt_transition(attempt, AttemptState.COMPLETED)
        await ledger.request_cancel(task, owner_actor_id=OWNER)
        row = await svc.repo.get_attempt(attempt)
        assert row["status"] == AttemptState.CANCELLED
        assert Decimal(row["refs"]["usage_pending"]["reserved"]) == Decimal("0.050000")
        with pytest.raises(IdempotencyConflict):
            await ledger.record_late_cost(
                task,
                attempt_id=attempt,
                consumed=Decimal(0),
                tokens=None,
                turn_id=None,
                not_started=True,
            )
        for turn, usage in [
            ("other", BILL),
            ("t", Usage(output=501)),
            ("t", Usage(input=-1)),
        ]:
            with pytest.raises((IdempotencyConflict, CapabilityError)):
                await svc.receipt(
                    OWNER, task, attempt, turn_id=turn, response_id="r", usage=usage
                )

    async def late():
        async with host() as svc:
            await svc.receipt(
                OWNER, task, attempt, turn_id="t", response_id="r", usage=BILL
            )

    await asyncio.gather(late(), late())
    async with host() as svc:
        budget = (await svc.repo.get_task(task))["budget"]
        assert Decimal(budget["reserved"]) == 0 and Decimal(budget["used"]) == Decimal(
            "0.000006"
        )
        assert (
            len(
                [e for e in await svc.repo.ledger_list(task) if e["entry"] == "consume"]
            )
            == 1
        )


async def test_budget_route_account_source_and_output_rechecked_before_claim(db):
    host, task, cred, original = await setup(db, budget="0.049")
    async with host() as svc:
        with pytest.raises(BudgetExhausted):
            await svc.open(OWNER, task, caller="knowlab")
        assert not await svc.repo.list_attempts(task)
    route = replace(ROUTE, context_tokens=1000)
    async with host(route) as svc:
        attempt = await svc.open(OWNER, task, caller="knowlab")

    async def claim(svc, body=original):
        return await svc.claim(
            OWNER,
            task,
            attempt,
            caller="knowlab",
            credential_id=cred["id"],
            turn_id="t",
            body=body,
        )

    async with host() as svc:
        with pytest.raises(CapabilityError):
            await claim(svc)
    async with host(route, licenses=()) as svc:
        with pytest.raises(CapabilityError):
            await claim(svc)
    async with host(route) as svc:
        for changes in (
            {"model": "other"},
            {"max_output_tokens": False},
            {"max_output_tokens": 800},
            {"input": []},
            {"text": {"format": {"type": "json_schema", "schema": {}}}},
            {"tools": [{"type": "web_search"}]},
            {"previous_response_id": "r"},
            {
                "input": [
                    dict(
                        original["input"][0],
                        content=[{"type": "input_text", "text": "{}"}],
                    )
                ]
            },
        ):
            with pytest.raises(CapabilityError):
                await claim(svc, original | changes)
        with pytest.raises(CapabilityError):
            await svc.claim(
                OWNER,
                task,
                attempt,
                caller="knowlab",
                credential_id="previous-sandbox-key",
                turn_id="t",
                body=original,
            )
        async with svc.repo.transaction():
            with pytest.raises(RuntimeError, match="must commit"):
                await claim(svc)
        with pytest.raises(NotFound):
            await svc.claim(
                OWNER,
                task,
                attempt,
                caller="other",
                credential_id=cred["id"],
                turn_id="t",
                body=original,
            )
        async with svc.repo.transaction():
            assert await svc.repo.revoke_credential(cred["id"], owner_actor_id=OWNER)
        with pytest.raises(CapabilityError):
            await claim(svc)
        assert (
            "instruction_request" not in (await svc.repo.get_attempt(attempt))["refs"]
        )
        await Ledger(svc.repo).request_cancel(task, owner_actor_id=OWNER)
        budget = (await svc.repo.get_task(task))["budget"]
        assert Decimal(budget["reserved"]) == Decimal(budget["used"]) == 0


async def test_default_provider_credential_can_only_use_its_deployment_endpoint(db):
    host, task, _, _ = await setup(db, credential_url=None)
    for endpoint in ("", "https://different.example.test/v1"):
        async with host(default_url=endpoint) as svc:
            with pytest.raises(CapabilityError):
                await svc.open(OWNER, task, caller="knowlab")
    async with host(default_url=ROUTE.base_url) as svc:
        attempt = await svc.open(OWNER, task, caller="knowlab")
        assert (await svc.repo.get_attempt(attempt))["model"] == ROUTE.model
