"""Dedicated execution via Runner.handle, durable one-turn acquisition and receipt recovery."""

import asyncio
import json
from decimal import Decimal
from types import SimpleNamespace
from uuid import uuid4

import httpx
import pytest
from test_instruction_requests_db import BILL, PRICES, ROUTE, setup
from test_workbench_ledger_db import OWNER
from test_workbench_ledger_db import db as db

from app.application.workbench.formal_methods import FormalMethods
from app.application.workbench.instruction_runner import InstructionRunner
from app.application.workbench.ledger import Ledger
from app.application.workbench.runner import Runner
from app.application.workbench.tokens import TokenIssuer, generate_private_key_pem
from app.infrastructure.workbench.provisioner import InstructionUnits
from app.infrastructure.workbench.repository import SqlWorkbenchStores


class Units:
    def __init__(self):
        self.uid, self.worker = str(uuid4()), str(uuid4())
        self.bindings, self.ops = [], []
        self.scopes = []
        self.deleted = self.reclaimed = False
        self.ready = False
        self.deliver = None
        self.after_claim = None

    async def request(
        self, owner, task, method, path, *, body=None, params=None, absent=False
    ):
        assert owner == OWNER
        self.ops.append((method, path))
        pod = {"uid": self.uid, "phase": "Running", "ready": self.ready}
        if path in ("prepare", "reservation"):
            return pod
        assert path.startswith(self.uid)
        if path.endswith("/binding"):
            self.bindings.append(body)
            if body["turn"] and self.deliver:
                await self.deliver(body["turn"])
            return pod
        if path.endswith("/worker"):
            return {
                **pod,
                "uid": self.worker,
                "ip": "127.0.0.1",
                "app_server_token": "ephemeral",
            }
        if path == self.uid and method == "GET":
            return None if self.deleted else pod
        if method == "DELETE":
            if self.bindings:
                assert params["settled_attempt"] == self.bindings[0]["attempt"]
            if path.endswith("/proof"):
                assert self.deleted
                self.reclaimed = True
            else:
                self.deleted = True
        return {}


async def stack(db, *, setup_case=setup, result=None, **kwargs):
    host, task, cred, wire = await setup_case(db, **kwargs)
    async with host() as svc:
        review_inputs = svc.formal.review_inputs
        catalog = svc.formal.source_licenses
        taskrow = await svc.repo.get_task(task)
        session = await svc.repo.get_session(str(taskrow["session_id"]))

    def formal(repo):
        svc = FormalMethods(repo, review_inputs=review_inputs)
        svc.source_licenses = catalog
        return svc

    units = Units()
    units.task = task
    units.thread = "thread-a"
    units.result = result
    issuer = TokenIssuer(generate_private_key_pem())
    turns = []
    provider_requests = []

    class Client:
        def __init__(self, ip, token, *, on_notification, on_server_request):
            assert ip == "127.0.0.1" and token == "ephemeral"
            self.notice = on_notification

        async def connect(self):
            pass

        async def close(self):
            pass

        async def request(self, method, params, *, on_result=None, timeout=60):  # noqa: ASYNC109
            if method == "thread/turns/list":
                return {"data": []}
            if method == "thread/start":
                assert (
                    params["environments"] == []
                    and not params["ephemeral"]
                    and not params["allowProviderModelFallback"]
                )
                return {
                    **params,
                    "thread": {"id": units.thread},
                    "sandbox": {"type": "readOnly"},
                }
            assert method == "turn/start" and json.loads(
                params["input"][0]["text"]
            ) == json.loads(wire["input"][0]["content"][0]["text"])
            turns.append(1)
            on_result({"turn": {"id": "turn-a"}})

            async def deliver(turn):
                assert turn == "turn-a"
                binding = units.bindings[-1]
                identity = issuer.instruction_identity(binding["ticket"])
                assert (
                    str(identity.gateway) == units.uid
                    and str(identity.attempt) == binding["attempt"]
                )
                async with host() as svc:
                    attempt = await svc.repo.get_attempt(binding["attempt"])
                    caller = attempt["refs"]["instruction_quote"]["caller"]
                    if caller == "formal-expert":
                        svc.formal = driver.formal(svc.repo)
                    _, outgoing = await svc.claim(
                        OWNER,
                        units.task,
                        binding["attempt"],
                        caller=caller,
                        credential_id=cred["id"],
                        turn_id=turn,
                        body=wire,
                    )
                    provider_requests.append(outgoing)
                    if units.after_claim:
                        await units.after_claim()
                    await svc.receipt(
                        OWNER,
                        units.task,
                        binding["attempt"],
                        turn_id=turn,
                        response_id=(
                            "receipt-a"
                            if units.task == task
                            else "receipt-" + binding["attempt"]
                        ),
                        usage=BILL,
                    )
                await self.notice(
                    "item/completed",
                    {
                        "threadId": units.thread,
                        "turnId": turn,
                        "item": {
                            "type": "agentMessage",
                            "text": json.dumps(
                                units.result
                                if units.result is not None
                                else {"summary": "ready"}
                            ),
                        },
                    },
                )
                await self.notice(
                    "turn/completed",
                    {
                        "threadId": units.thread,
                        "turn": {"id": turn, "status": "completed"},
                    },
                )

            units.deliver = deliver
            return {"turn": {"id": "turn-a"}}

    stores = SqlWorkbenchStores(db)

    async def provision(req):
        assert req.headers["Authorization"] == "Bearer service-only"
        root = f"/v1/instruction-executions/{OWNER}/{units.task}/"
        assert req.url.path.startswith(root)
        path = req.url.path[len(root) :]
        scope = None
        if path.startswith("attempts/"):
            _, scope, path = path.split("/", 2)
        units.scopes.append(scope)
        result = await units.request(
            OWNER,
            task,
            req.method,
            path,
            body=json.loads(req.content) if req.content else None,
            params=dict(req.url.params),
        )
        return httpx.Response(200 if result is not None else 404, json=result)

    driver = InstructionRunner(
        stores,
        formal,
        InstructionUnits(
            "http://provisioner.test", "service-only", httpx.MockTransport(provision)
        ),
        Client,
        issuer,
        routes=(ROUTE,),
        model=ROUTE.model,
        prices=PRICES,
        default_url="",
    )
    runner = Runner(
        stores,
        connector=None,
        publisher=None,
        runner_id="unit-runner",
        instructions=driver,
    )
    return SimpleNamespace(
        host=host,
        task=task,
        session=str(session["id"]),
        sandbox=str(session["sandbox_id"]),
        runner=runner,
        driver=driver,
        units=units,
        turns=turns,
        provider_requests=provider_requests,
        wire=wire,
        cred=cred,
    )


async def drive(s):
    await s.runner.handle(
        {"kind": "task.drive", "session_id": s.session, "payload": {"task_id": s.task}}
    )
    await s.runner.driving[s.task]


async def test_runner_resumes_prepared_worker_and_reclaims_settled_attempt(db):
    from sqlalchemy import text

    from app.application.workbench.cleanup_observation import snapshot

    s = await stack(db)
    await asyncio.gather(drive(s), s.driver.drive(s.task))
    assert s.turns == [] and s.units.bindings
    s.units.ready = True
    await asyncio.gather(drive(s), s.driver.drive(s.task))
    async with s.host() as svc:
        (attempt,) = await svc.repo.list_attempts(s.task)
        assert (await svc.repo.get_task(s.task))["state"] == "SUCCEEDED"
        assert attempt["refs"]["formal_result"]["outputs"] == {"summary": "ready"}
        assert attempt["refs"]["instruction_thread"] == {
            "worker": s.units.worker,
            "thread": "thread-a",
        }
        assert set(s.units.scopes) == {str(attempt["id"])}
        assert s.turns == [1] and attempt["budget_consumed"]["tokens"]["output"] == 1
    async with db() as session, session.begin():
        await session.execute(
            text("""
            UPDATE workbench_attempts
            SET started_at=started_at-interval '301 seconds', ended_at=ended_at-interval '301 seconds'
            WHERE id=:id
        """),
            {"id": attempt["id"]},
        )
    await s.driver.reconcile([s.sandbox])
    assert s.units.deleted and not s.units.reclaimed
    async with s.host() as svc:
        queue = (await snapshot(svc.repo))["instruction_units"]
        assert (
            queue["pending"] == 1 and queue["oldest_seconds"] >= 300 and queue["alert"]
        )
    await s.driver.cleanup(OWNER, s.task, str(attempt["id"]))
    assert s.units.reclaimed
    async with s.host() as svc:
        assert (await snapshot(svc.repo))["instruction_units"] == {
            "pending": 0,
            "oldest_seconds": 0,
            "alert": False,
        }
        assert not (await svc.repo.get_attempt(str(attempt["id"])))["refs"][
            "instruction_unit"
        ]["pending"]


async def test_interrupted_dispatch_is_never_reopened_and_unknown_bill_retains_unit(db):
    s = await stack(db)
    await drive(s)
    attempt = s.units.bindings[0]["attempt"]
    assert await s.driver.capture(
        OWNER, s.task, attempt, s.units.worker, "thread-first"
    )
    assert await s.driver.capture(
        OWNER, s.task, attempt, s.units.worker, "thread-first", "turn-first"
    )
    async with s.host() as svc:
        await svc.claim(
            OWNER,
            s.task,
            attempt,
            caller="knowlab",
            credential_id=s.cred["id"],
            turn_id="turn-first",
            body=s.wire,
        )
    s.units.ready = True
    await s.runner._settle({"id": s.task})
    assert not s.turns and s.units.bindings[-1]["turn"] == "turn-first"
    async with s.host() as svc:
        await Ledger(svc.repo).request_cancel(s.task, owner_actor_id=OWNER)
    s.driver.routes = ()  # New model work is disabled while the old bill is outstanding.
    await s.driver.reconcile([s.sandbox])
    assert ("POST", s.units.uid + "/stop") in s.units.ops
    assert not s.units.deleted
    async with s.host() as svc:
        assert (await svc.repo.get_attempt(attempt))["refs"]["usage_pending"]
        await svc.receipt(
            OWNER, s.task, attempt, turn_id="turn-first", response_id="late", usage=BILL
        )
    await s.driver.cleanup(OWNER, s.task, attempt)
    assert s.units.deleted and not s.turns


async def test_budget_shortfall_precedes_any_provisioning(db):
    s = await stack(db, budget="0.000001")
    await drive(s)
    async with s.host() as svc:
        task = await svc.repo.get_task(s.task)
        assert task["state"] == "WAITING" and not task.get("active_attempt_id")
    assert s.units.ops == []


@pytest.mark.parametrize("cancelled", [False, True])
@pytest.mark.parametrize("partial", [False, True])
async def test_lost_prepare_reply_recovers_without_second_allocation(
    db, cancelled, partial
):
    s = await stack(db)
    request = s.units.request

    proof_uid = str(uuid4())

    async def lose_reply(owner, task, method, path, **kwargs):
        result = await request(owner, task, method, path, **kwargs)
        if partial and path == "reservation":
            return {"state": "pvc_only", "proof_uid": proof_uid}
        if path == "prepare" and kwargs.get("params") == {"proof_uid": proof_uid}:
            return result
        if path == "prepare":
            raise httpx.ReadTimeout("lost after POST")
        return result

    s.units.request = lose_reply
    await drive(s)
    assert s.units.ops == [("POST", "prepare")]
    if cancelled:
        async with s.host() as svc:
            await Ledger(svc.repo).request_cancel(s.task, owner_actor_id=OWNER)
        # No ticket was delivered. The host only looks up the reserved Pod to stop it.
        await s.driver.reconcile([s.sandbox])
        assert s.units.deleted and not s.turns
    else:
        await drive(s)
        assert s.units.bindings
    assert (
        s.units.ops.count(("POST", "prepare")) == (2 if partial else 1)
        and ("GET", "reservation") in s.units.ops
    )


async def test_unacknowledged_turn_start_cannot_be_replayed_or_claimed(db):
    from datetime import UTC, datetime, timedelta

    from app.domain.workbench.capabilities import CapabilityError

    s = await stack(db)
    await drive(s)
    attempt = s.units.bindings[0]["attempt"]
    assert await s.driver.capture(OWNER, s.task, attempt, s.units.worker, "original")
    async with s.host() as svc:
        with pytest.raises(CapabilityError):
            await svc.claim(
                OWNER,
                s.task,
                attempt,
                caller="knowlab",
                credential_id=s.cred["id"],
                turn_id="guessed",
                body=s.wire,
            )
    await s.runner._settle({"id": s.task})
    assert not s.turns and not any(body["turn"] for body in s.units.bindings)
    async with s.host() as svc, svc.repo.transaction():
        await svc.repo.update_attempt(
            attempt, started_at=datetime.now(UTC) - timedelta(minutes=11)
        )
    await drive(s)
    async with s.host() as svc:
        task = await svc.repo.get_task(s.task)
        assert (
            task["state"] == "FAILED"
            and (await svc.repo.get_attempt(attempt))["failure_code"]
            == "instruction_deadline"
        )
        assert float(task["budget"]["reserved"]) == 0
    assert not s.turns


@pytest.mark.parametrize(
    ("status", "detail"),
    [
        (503, {"code": "instruction_prepare_disabled"}),
        (404, "Not Found"),
    ],
)
async def test_unavailable_provisioner_settles_without_allocating(db, status, detail):
    s = await stack(db)

    async def disabled(request):
        assert "/attempts/" in request.url.path and request.url.path.endswith(
            "/prepare"
        )
        return httpx.Response(status, json={"detail": detail})

    s.driver.units.transport = httpx.MockTransport(disabled)
    await drive(s)
    async with s.host() as svc:
        (attempt,) = await svc.repo.list_attempts(s.task)
        assert (
            attempt["failure_code"] == "instruction_unit_unavailable"
            and not attempt["refs"]["instruction_unit"]["prepare_requested"]
        )
    await s.driver.reconcile([s.sandbox])
    async with s.host() as svc:
        assert not (await svc.repo.get_attempt(str(attempt["id"])))["refs"][
            "instruction_unit"
        ]["pending"]
    assert not s.units.ops


async def test_missing_runner_credentials_reject_new_trial_without_reserving(db):
    s = await stack(db)
    s.driver.issuer = None
    await drive(s)
    async with s.host() as svc:
        task = await svc.repo.get_task(s.task)
        assert task["state"] == "FAILED"
        assert not await svc.repo.list_attempts(s.task)
        assert float(task["budget"]["reserved"]) == 0
    assert not s.units.ops


async def test_pre_upgrade_attempt_resumes_and_reclaims_its_task_scoped_unit(db):
    s = await stack(db)
    _, attempt_id = await s.driver.start(s.task)
    async with s.host() as svc, svc.repo.transaction():
        attempt = await svc.repo.get_attempt(attempt_id, for_update=True)
        refs = dict(attempt["refs"])
        refs["instruction_unit"] = {"pending": True}
        await svc.repo.update_attempt(attempt_id, refs=refs)
    s.units.ready = True
    await drive(s)
    await s.driver.cleanup(OWNER, s.task, attempt_id)
    await s.driver.cleanup(OWNER, s.task, attempt_id)
    assert set(s.units.scopes) == {None}
    assert s.units.reclaimed and s.turns == [1]


@pytest.mark.parametrize(
    "failure", ["empty_finding", "reused_gateway", "revoked_connection"]
)
async def test_strict_review_retains_evidence_and_stops_before_comparison(
    db, monkeypatch, failure
):
    from functools import partial

    from branch_review_support import review_case

    s = await stack(
        db,
        setup_case=partial(review_case, monkeypatch=monkeypatch, strict=True),
        result={"result": "" if failure == "empty_finding" else "sealed finding"},
    )
    s.units.ready = True
    await drive(s)
    if failure == "revoked_connection":
        async with s.host() as svc, svc.repo.transaction():
            await svc.repo.revoke_credential(s.cred["id"], owner_actor_id=OWNER)
    await drive(s)
    await drive(s)
    # Only the independent round incurred a charge; no assessment ever reached the provider.
    async with db() as session:
        from app.infrastructure.workbench.repository import WorkbenchRepository

        repo = WorkbenchRepository(session)
        task = await repo.get_task(s.task)
        attempts = await repo.list_attempts(s.task)
    assert task["state"] == "FAILED" and len(s.provider_requests) == 1
    assert "withheld author opinion" not in json.dumps(s.provider_requests)
    assert (
        Decimal(task["budget"]["reserved"]) == 0 and Decimal(task["budget"]["used"]) > 0
    )
    from app.application.workbench.review_decisions import OwnerDecision, decision_page

    async with db() as sql:
        repo = WorkbenchRepository(sql)
        page = await decision_page(repo, OWNER, s.task)
        assert (
            page["task_state"] == "FAILED"
            and "review-comparison" in page["missing_stages"]
        )
        if failure == "empty_finding":
            assert not page["reports"]
        else:
            (report,) = page["reports"]
            saved = await decision_page(
                repo,
                OWNER,
                s.task,
                command=OwnerDecision.model_validate(
                    {
                        "review_digest": page["review_digest"],
                        "previous_revision": 0,
                        "items": [
                            {
                                "artifact_id": report["id"],
                                "quote": "sealed finding",
                                "decision": "deferred",
                                "reason": "Comparison still missing.",
                            }
                        ],
                    }
                ),
            )
            assert saved["revision"] == 1 and saved["missing_stages"] == [
                "review-comparison"
            ]
    if failure == "empty_finding":
        assert len(attempts) == 1 and not attempts[0]["output_artifacts"]
    else:
        second = next(a for a in attempts if a["step_id"] == "review-comparison")
        assert (
            second["status"] == "FAILED" and "instruction_request" not in second["refs"]
        )
        first = next(a for a in attempts if a["step_id"] == "direct")
        assert first["output_artifacts"] == second["input_artifact_versions"]


async def test_uncertain_first_review_neither_releases_assessment_nor_reissues_request(
    db, monkeypatch
):
    from functools import partial

    from branch_review_support import review_case

    from app.domain.workbench.capabilities import CapabilityError
    from app.domain.workbench.source_tasks import SourceTask

    s = await stack(
        db, setup_case=partial(review_case, monkeypatch=monkeypatch, strict=True)
    )
    s.units.ready = True

    async def lost_reply():
        raise asyncio.CancelledError

    s.units.after_claim = lost_reply
    with pytest.raises(asyncio.CancelledError):
        await drive(s)
    await drive(s)
    async with s.host() as svc:
        task = await svc.repo.get_task(s.task)
        (attempt,) = await svc.repo.list_attempts(s.task)
        assert task["state"] == "RUNNING" and attempt["step_id"] == "direct"
        assert attempt["refs"].get("instruction_request") and not attempt["refs"].get(
            "instruction_usage"
        )
        assert (
            not attempt["output_artifacts"] and Decimal(task["budget"]["reserved"]) > 0
        )
        with pytest.raises(CapabilityError, match="Comparison attempt is not active"):
            await svc.formal.review_inputs(
                svc.repo, OWNER, SourceTask.load(task).review, comparison_task=s.task
            )
    assert len(
        s.provider_requests
    ) == 1 and "withheld author opinion" not in json.dumps(s.provider_requests)
