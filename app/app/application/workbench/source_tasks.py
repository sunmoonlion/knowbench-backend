"""Submit and execute owner-confirmed registry revisions in expert sessions."""

from decimal import Decimal
from uuid import uuid4

from app.application.workbench.formal_orchestration_execution import save_step_output
from app.application.workbench.ledger import Ledger
from app.application.workbench.script_budget import reserve, settle
from app.domain.workbench.capabilities import (
    CapabilityError,
    ScriptNotProvisioned,
    digest,
)
from app.domain.workbench.errors import IdempotencyConflict, WorkbenchError
from app.domain.workbench.models import HandoverRequest
from app.domain.workbench.registry_sources import RegisteredSource, SourceRevision
from app.domain.workbench.skill_execution import method_script_outputs
from app.domain.workbench.source_tasks import SourceTask, script_input
from app.domain.workbench.states import (
    ATTEMPT_TERMINAL,
    TASK_TERMINAL,
    AttemptState,
    TaskState,
)


async def resolve(repo, owner, ref):
    row = await repo.direct_revision(owner, ref.source_id, ref.version, ref.digest)
    revision = SourceRevision.from_row(row)
    return RegisteredSource.model_validate(
        revision.model_dump(exclude={"content"})
    ), revision.content


def script_quote(svc):
    tariff = svc.operations.script_tariff
    if (
        svc.script_executor is None
        or tariff is None
        or svc.script_executor.url.rstrip("/") != tariff.provisioner_url.rstrip("/")
    ):
        raise CapabilityError("The priced isolated script executor is unavailable")
    return tariff.quote()


async def preview(svc, owner, session_id, body):
    repo = svc.repo
    session = await repo.get_session(str(session_id), owner_actor_id=owner)
    if session["kind"] != "expert" or not session["project_id"]:
        raise CapabilityError(
            "A registered source requires an owned expert project session"
        )
    source, content = await resolve(repo, owner, body)
    if source.kind == "script":
        quote = script_quote(svc)
        try:
            script_input(source, content, body.arguments)
        except ValueError as exc:
            raise CapabilityError(
                "Source name or arguments are invalid for the script executor"
            ) from exc
    else:
        if svc.instruction_service is None:
            raise CapabilityError("Dedicated instruction execution is unavailable")
        service = await svc.instruction_service(svc.formal_sources, owner)
        quote = await service.account_quote(owner, session, "CNY")
    if Decimal(quote["maximum"]) > body.budget_limit:
        raise CapabilityError("The maximum execution cost exceeds the task budget")
    return SourceTask(
        owner=owner,
        session_id=session_id,
        source=source,
        arguments=body.arguments,
        budget_limit=body.budget_limit,
        quote=quote,
    )


async def submit(svc, owner, session_id, body):
    repo = svc.repo
    async with repo.transaction():
        await repo.get_session(str(session_id), owner_actor_id=owner, for_update=True)
        prior = await repo.find_task_by_idempotency(
            tenant="default",
            owner_actor_id=owner,
            profile_id="registry-source",
            idempotency_key=body.idempotency_key,
        )
        if prior is not None:
            original = SourceTask.load(prior)
            if (
                str(original.session_id) != str(session_id)
                or prior["pack_digest"] != body.approval_digest
                or original.source.source_id != body.source_id
                or original.source.version != body.version
                or original.source.digest != body.digest
                or original.budget_limit != body.budget_limit
                or digest(original.arguments) != digest(body.arguments)
            ):
                raise IdempotencyConflict(
                    "Existing source task differs from the confirmed request"
                )
            return {
                "task_id": str(prior["id"]),
                "state": prior["state"],
                "created": False,
            }
        snap = await preview(svc, owner, session_id, body)
        if snap.task_columns()["pack_digest"] != body.approval_digest:
            raise CapabilityError(
                "Source, arguments, session or price changed since confirmation"
            )
        return await hand_over_source(repo, owner, snap, body.idempotency_key)


async def hand_over_source(repo, owner, snap, idempotency_key):
    made = await Ledger(repo).handover(
        HandoverRequest(
            idempotency_key=idempotency_key,
            session_id=str(snap.session_id),
            profile_id="registry-source",
            original_input={
                "source": snap.source.model_dump(mode="json"),
                "approval_digest": snap.task_columns()["pack_digest"],
            },
            budget_limit=snap.budget_limit,
            budget_currency=snap.quote["currency"],
        ),
        owner_actor_id=owner,
        snapshot=snap,
    )
    if made["created"] and not made.get("queued_behind"):
        session = await repo.get_session(str(snap.session_id), owner_actor_id=owner)
        await repo.enqueue_command(
            session_id=str(snap.session_id),
            sandbox_id=str(session["sandbox_id"]),
            kind="task.drive",
            payload={"task_id": made["task_id"]},
        )
    return made


async def active_source(repo, owner, task):
    snap = SourceTask.load(task)
    session = await repo.get_session(str(snap.session_id), owner_actor_id=owner)
    if (
        str(snap.owner) != owner
        or task.get("cancel_requested_at")
        or task["state"] in TASK_TERMINAL
        or session["wheel"] != "runtime"
        or str(session["active_task_id"]) != str(task["id"])
        or task["original_input"]["input"].get("approval_digest") != task["pack_digest"]
    ):
        raise CapabilityError("Registered source task approval is no longer active")
    source, content = await resolve(repo, owner, snap.source)
    if source != snap.source:
        raise CapabilityError("Registered source metadata changed")
    return snap, content


async def check_script_call(svc, owner, call):
    repo = svc.repo
    task = await repo.get_task(
        str(call["task_id"]), owner_actor_id=owner, for_update=True
    )
    attempt = await repo.get_attempt(str(call["attempt_id"]), for_update=True)
    snap, content = await active_source(repo, owner, task)
    if (
        snap.source.kind != "script"
        or str(attempt["task_id"]) != str(task["id"])
        or task["state"] != TaskState.RUNNING
        or str(task["active_attempt_id"]) != str(attempt["id"])
        or attempt["status"] != AttemptState.RUNNING
        or (attempt["refs"] or {}).get("capability_call_id") != str(call["id"])
        or str(call["session_id"]) != str(snap.session_id)
        or call["status"] != "dispatching"
        or (call["detail"].get("result") or {}).get("stop_requested")
        or call["detail"].get("script_quote") != snap.quote
        or script_quote(svc) != snap.quote
    ):
        raise CapabilityError(
            "Script execution no longer matches its approved source and tariff"
        )
    return script_input(snap.source, content, snap.arguments)


async def finish_script(repo, task, attempt, call):
    result = call["detail"].get("result") or {}
    outputs = None
    if call["status"] == "succeeded":
        try:
            outputs = method_script_outputs(result)
        except ValueError:
            pass
    artifacts = []
    if outputs is not None:
        artifacts.append(
            await save_step_output(
                repo, str(task["id"]), str(attempt["id"]), "registry-result", outputs
            )
        )
    ledger = Ledger(repo)
    await ledger.attempt_transition(
        str(attempt["id"]),
        AttemptState.COMPLETED if artifacts else AttemptState.FAILED,
        output_artifacts=artifacts,
        retryable=False,
    )
    await ledger.transition(
        str(task["id"]),
        TaskState.SUCCEEDED if artifacts else TaskState.FAILED,
        reason={"source_call": str(call["id"]), "call_status": call["status"]},
    )


async def drive_script(svc, task_id):
    repo = svc.repo
    ledger = Ledger(repo)
    async with repo.transaction():
        task = await repo.get_task(task_id, for_update=True)
        if task["state"] in TASK_TERMINAL or task["original_input"][
            "client_context"
        ].get("queued_behind"):
            return
        owner = str(task["owner_actor_id"])
        if task.get("cancel_requested_at"):
            await ledger.request_cancel(task_id, owner_actor_id=owner)
            return
        if task.get("active_attempt_id"):
            attempt = await repo.get_attempt(
                str(task["active_attempt_id"]), for_update=True
            )
            call = await repo.capability_call(
                owner, attempt["refs"]["capability_call_id"], lock=True
            )
            if (
                call["status"] != "dispatching"
                and attempt["status"] not in ATTEMPT_TERMINAL
            ):
                await finish_script(repo, task, attempt, call)
            return
        if task["state"] == TaskState.RECEIVED:
            await ledger.transition(task_id, TaskState.VALIDATING)
        try:
            snap, _ = await active_source(repo, owner, task)
            if script_quote(svc) != snap.quote or snap.source.kind != "script":
                raise CapabilityError(
                    "Script executor or quote changed before dispatch"
                )
        except (WorkbenchError, ValueError):
            await ledger.transition(
                task_id,
                TaskState.REJECTED
                if task["state"] in (TaskState.RECEIVED, TaskState.VALIDATING)
                else TaskState.FAILED,
                reason={"code": "source_authority_changed"},
            )
            return
        if task["state"] in (TaskState.RECEIVED, TaskState.VALIDATING):
            await ledger.transition(task_id, TaskState.QUEUED)
        cid = str(uuid4())
        opened = await ledger.open_attempt(
            task_id,
            step_id="direct",
            step_version=snap.source.digest.removeprefix("sha256:"),
            meta={"capability_call_id": cid},
        )
        aid = opened["attempt_id"]
        await ledger.attempt_transition(aid, AttemptState.RUNNING)
        intent = {
            "execution_kind": "registry_script",
            "script_quote": snap.quote,
            "source": snap.source.model_dump(mode="json"),
            "approval_digest": task["pack_digest"],
        }
        call = await repo.add_call(
            dict(
                id=cid,
                owner_actor_id=owner,
                session_id=str(snap.session_id),
                task_id=task_id,
                attempt_id=aid,
                kind="tool",
                origin="managed",
                call_key="registry-source:" + task_id,
                registration_id=str(snap.source.source_id),
                registration_digest=snap.source.digest,
                digest=digest([task_id, intent]),
                status="dispatching",
                detail=intent,
            )
        )
        await reserve(repo, owner, cid, snap.quote)
    # Only the transaction that created the call may send it. Recovery observes this ID.
    prepared = False
    try:
        async with repo.transaction():
            payload = await check_script_call(svc, owner, call)
        prepared = True
        await svc.script_executor.prepare(owner, cid, payload)
        async with repo.transaction():
            current = await repo.capability_call(owner, cid)
            payload = await check_script_call(svc, owner, current)
        await svc.script_executor.start(owner, cid, payload)
    except Exception as exc:
        if not prepared or isinstance(exc, ScriptNotProvisioned):
            async with repo.transaction():
                await settle(
                    repo,
                    owner,
                    cid,
                    amount=Decimal(0),
                    evidence={
                        "run_id": cid,
                        "stage": getattr(exc, "stage", "before_provisioning"),
                    },
                )
                await repo.update_call(
                    owner,
                    cid,
                    "failed",
                    {
                        **intent,
                        "result": {"isError": True, "message": "Script did not start"},
                    },
                )
        else:
            from app.application.workbench.formal_script_execution import request_stop

            await request_stop(svc, owner, call)
