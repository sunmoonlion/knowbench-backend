"""Drive a retained single-turn instruction trial on its dedicated provisioned unit."""

from datetime import UTC, datetime, timedelta
from uuid import UUID

from app.application.ports.workbench import AppServerError
from app.application.workbench.instruction_requests import (
    InstructionRequests,
    issue_instruction_ticket,
)
from app.application.workbench.instruction_turn import (
    read_instruction_turn,
    run_instruction_turn,
)
from app.application.workbench.ledger import Ledger
from app.application.workbench.provisioning import (
    InstructionPrepareNotSent,
    ProvisionerFailed,
    ProvisioningUnavailable,
)
from app.domain.workbench.capabilities import CapabilityError, digest
from app.domain.workbench.errors import BudgetExhausted, IdempotencyConflict, NotFound
from app.domain.workbench.formal_execution import snapshot_for_task, validate_values
from app.domain.workbench.models import InteractionPrompt
from app.domain.workbench.states import (
    ATTEMPT_TERMINAL,
    TASK_TERMINAL,
    AttemptState,
    TaskState,
    WaitingReason,
)


class InstructionRunner:
    def __init__(
        self,
        stores,
        formal,
        units,
        connector,
        issuer,
        *,
        routes,
        model,
        prices,
        default_url,
    ):
        self.stores, self.formal, self.units, self.connector, self.issuer = (
            stores,
            formal,
            units,
            connector,
            issuer,
        )
        self.routes, self.model, self.prices, self.default_url = (
            routes,
            model,
            prices,
            default_url,
        )

    @property
    def configured(self):
        return self.units is not None and self.issuer is not None

    async def service(self, repo, owner):
        return await InstructionRequests.for_account(
            self.formal(repo),
            owner,
            routes=self.routes,
            model=self.model,
            prices=self.prices,
            default_url=self.default_url,
        )

    async def start(self, task_id):
        async with self.stores() as repo, repo.transaction():
            task = await repo.get_task(task_id, for_update=True)
            if (
                task["state"] in TASK_TERMINAL
                or task["state"] == TaskState.WAITING
                or (
                    (task.get("original_input") or {})
                    .get("client_context", {})
                    .get("queued_behind")
                )
            ):
                return None
            owner = str(task["owner_actor_id"])
            if task.get("cancel_requested_at"):
                await Ledger(repo).request_cancel(task_id, owner_actor_id=owner)
                return None
            if task["state"] == TaskState.RECEIVED:
                await Ledger(repo).transition(task_id, TaskState.VALIDATING)
            if task["state"] in (TaskState.RECEIVED, TaskState.VALIDATING):
                await Ledger(repo).transition(task_id, TaskState.QUEUED)
            if task.get("active_attempt_id"):
                return owner, str(task["active_attempt_id"])
        if not self.configured:
            raise ProvisioningUnavailable("Dedicated instruction execution is disabled")
        async with self.stores() as repo:
            async with repo.transaction():
                svc = await self.service(repo, owner)
            attempt = await svc.open(owner, task_id, caller="knowlab", managed=True)
            return owner, str(attempt)

    async def gateway(self, owner, task_id, attempt_id):
        async with self.stores() as repo, repo.transaction():
            task = await repo.get_task(task_id, owner_actor_id=owner, for_update=True)
            attempt = await repo.get_attempt(attempt_id, for_update=True)
            refs = attempt["refs"]
            if bound := refs.get("instruction_gateway"):
                return bound["uid"]
            request = self.units.for_attempt(attempt)
            unit = refs["instruction_unit"]
            prepare = (
                not unit.get("prepare_requested")
                and task["state"] == TaskState.RUNNING
                and str(task.get("active_attempt_id")) == attempt_id
            )
            if prepare:
                await repo.update_attempt(
                    attempt_id,
                    refs={
                        **refs,
                        "instruction_unit": {**unit, "prepare_requested": True},
                    },
                )
            elif not unit.get("prepare_requested"):
                return None
        if prepare:
            try:
                pod = await request(owner, task_id, "POST", "prepare")
            except (InstructionPrepareNotSent, ProvisioningUnavailable):
                await self.update_unit(task_id, attempt_id, prepare_requested=False)
                raise
        else:
            # A timeout/cancelled HTTP request can still be in flight. Observation
            # after takeover must never allocate another Pod or recreate its proof.
            pod = await request(owner, task_id, "GET", "reservation", absent=True)
            if not pod:
                return None
        uid = str(UUID(pod["uid"]))
        async with self.stores() as repo, repo.transaction():
            task = await repo.get_task(task_id, owner_actor_id=owner, for_update=True)
            attempt = await repo.get_attempt(attempt_id, for_update=True)
            refs = attempt["refs"]
            if str(attempt["task_id"]) != task_id or "instruction_unit" not in refs:
                raise CapabilityError("Unexpected instruction attempt")
            session = await repo.get_session(
                str(task["session_id"]), owner_actor_id=owner
            )
            binding = {"uid": uid, "sandbox": str(session["sandbox_id"])}
            if refs.get("instruction_gateway", binding) != binding:
                raise CapabilityError("Instruction gateway was replaced")
            if "instruction_gateway" not in refs:
                await repo.update_attempt(
                    attempt_id, refs={**refs, "instruction_gateway": binding}
                )
        return uid

    async def bind(self, owner, task_id, attempt_id, uid, turn=None):
        async with self.stores() as repo:
            ticket = await issue_instruction_ticket(
                repo,
                self.issuer,
                UUID(owner),
                UUID(task_id),
                UUID(attempt_id),
                gateway_uid=UUID(uid),
            )
            attempt = await repo.get_attempt(attempt_id)
            request = self.units.for_attempt(attempt)
            refs = attempt["refs"]
            model = refs["instruction_quote"]["model"]
        await request(
            owner,
            task_id,
            "PUT",
            uid + "/binding",
            body={
                "attempt": attempt_id,
                "model": model,
                "ticket": ticket.token,
                "turn": turn,
            },
        )

    async def capture(self, owner, task_id, attempt_id, worker, thread, turn=None):
        async with self.stores() as repo, repo.transaction():
            task = await repo.get_task(task_id, owner_actor_id=owner, for_update=True)
            attempt = await repo.get_attempt(attempt_id, for_update=True)
            refs = attempt["refs"]
            if (
                task["state"] != TaskState.RUNNING
                or task.get("cancel_requested_at")
                or str(task["active_attempt_id"]) != attempt_id
                or refs.get("permission_revocation")
            ):
                return False
            binding = {"worker": worker, "thread": thread}
            if turn is None:
                if refs.get("instruction_thread"):
                    return False
                refs = {**refs, "instruction_thread": binding}
            else:
                if (
                    refs.get("instruction_thread") != binding
                    or refs.get("runtime_turn_id", turn) != turn
                ):
                    raise CapabilityError("Instruction turn binding changed")
                refs = {**refs, "runtime_turn_id": turn}
            await repo.update_attempt(attempt_id, refs=refs)
            return True

    async def finish(
        self,
        task_id,
        attempt_id,
        *,
        thread=None,
        outputs=None,
        code=None,
        expired=False,
    ):
        async with self.stores() as repo, repo.transaction():
            task = await repo.get_task(task_id, for_update=True)
            if task["state"] in TASK_TERMINAL or str(
                task.get("active_attempt_id")
            ) != str(attempt_id):
                return
            led = Ledger(repo)
            if attempt_id is not None:
                attempt = await repo.get_attempt(attempt_id, for_update=True)
                refs = attempt["refs"]
                dispatch = refs.get("instruction_thread") or {}
                if not expired and thread != dispatch.get("thread"):
                    return
                if outputs is not None:
                    turn = refs.get("runtime_turn_id")
                    claim = refs.get("instruction_request") or {}
                    if (
                        not dispatch
                        or not turn
                        or claim.get("turn_id") != turn
                        or refs["instruction_unit"].get("protocol_error")
                        or refs.get("permission_revocation")
                    ):
                        raise CapabilityError(
                            "Instruction output lacks its authorized usage receipt"
                        )
                    step = snapshot_for_task(task).draft.definition.procedure
                    if step.kind != "direct":
                        raise CapabilityError(
                            "Instruction output requires a direct method"
                        )
                    outputs = validate_values(step.outputs, outputs)
                    bill = refs.get("instruction_usage")
                    if bill is None:
                        # The gateway may still be delivering its durable receipt.
                        # Re-read this same worker turn on the next runner poll.
                        return
                    if bill["turn_id"] != turn:
                        raise CapabilityError(
                            "Usage belongs to another instruction turn"
                        )
                result = {
                    "outcome": "failed" if code else "succeeded",
                    "outputs": outputs or {},
                    "outputs_digest": digest(outputs or {}),
                    "detail": code or "",
                    "rejection_code": None,
                }
                await repo.update_attempt(
                    attempt_id, refs={**refs, "formal_result": result}
                )
                await led.attempt_transition(
                    attempt_id,
                    AttemptState.FAILED if code else AttemptState.COMPLETED,
                    failure_code=code,
                    retryable=False,
                )
            await led.transition(
                task_id,
                TaskState.FAILED if code else TaskState.SUCCEEDED,
                reason={"code": code or "instruction_completed"},
            )

    async def drive(self, task_id):
        attempt_id = thread = None
        try:
            opened = await self.start(task_id)
            if not opened:
                return
            owner, attempt_id = opened
            async with self.stores() as repo:
                attempt = await repo.get_attempt(attempt_id)
                refs = attempt["refs"]
                if "instruction_unit" not in refs:
                    return
                if datetime.now(UTC) - attempt["started_at"] >= timedelta(minutes=10):
                    await self.finish(
                        task_id, attempt_id, code="instruction_deadline", expired=True
                    )
                    return
                if not self.configured:
                    # Existing dispatch may have incurred a charge. Keep its original binding.
                    return
                thread = (refs.get("instruction_thread") or {}).get("thread")
                if refs["instruction_unit"].get("protocol_error"):
                    raise CapabilityError(
                        "Instruction worker requested an unexpected tool"
                    )
                async with repo.transaction():
                    svc = await self.service(repo, owner)
                task = await repo.get_task(task_id, owner_actor_id=owner)
                quote, job = await svc.authority(owner, task, "knowlab")
                if quote != refs["instruction_quote"]:
                    raise CapabilityError("Instruction route changed")
            request = self.units.for_attempt(attempt)
            uid = await self.gateway(owner, task_id, attempt_id)
            if uid is None:
                return
            if dispatch := refs.get("instruction_thread"):
                # A lost turn/start reply has no observed turn id and must never be replayed.
                turn = refs.get("runtime_turn_id")
                if not turn:
                    return
                await self.renew(owner, task_id, attempt_id, uid, turn)
                worker = await request(owner, task_id, "PUT", uid + "/worker")
                if worker["uid"] != dispatch["worker"] or not worker["ready"]:
                    return
                try:
                    recovered = await read_instruction_turn(
                        self.connector, worker, dispatch["thread"], turn
                    )
                except (AppServerError, OSError):
                    # The original Pod can still be starting or persisting its history.
                    return
                except (CapabilityError, ValueError):
                    await self.finish(
                        task_id,
                        attempt_id,
                        thread=dispatch["thread"],
                        code="instruction_history_invalid",
                    )
                    return
                if recovered is not None:
                    thread = dispatch["thread"]
                    await self.finish(
                        task_id,
                        attempt_id,
                        thread=dispatch["thread"],
                        outputs=recovered,
                    )
                return
            await self.renew(owner, task_id, attempt_id, uid, None)
            gateway = await request(owner, task_id, "GET", uid)
            if gateway["phase"] in ("Failed", "Succeeded"):
                raise CapabilityError("Instruction gateway stopped")
            if not gateway["ready"]:
                return
            worker = await request(owner, task_id, "PUT", uid + "/worker")
            if worker["phase"] in ("Failed", "Succeeded"):
                raise CapabilityError("Instruction worker stopped")
            if not worker["ready"]:
                return
            worker_uid = str(UUID(worker["uid"]))

            async def take(tid):
                nonlocal thread
                if await self.capture(owner, task_id, attempt_id, worker_uid, tid):
                    thread = tid
                    return True
                return False

            async def observed(turn):
                if not await self.capture(
                    owner, task_id, attempt_id, worker_uid, thread, turn
                ):
                    raise CapabilityError("Instruction stopped before dispatch")
                await self.bind(owner, task_id, attempt_id, uid, turn)

            async def reject(observed_thread):
                if thread and thread == observed_thread:
                    await self.update_unit(task_id, attempt_id, protocol_error=True)

            outputs = await run_instruction_turn(
                self.connector,
                worker,
                quote["model"],
                job,
                take_thread=take,
                bind_turn=observed,
                reject=reject,
            )
            if thread is not None:
                if not isinstance(outputs, dict):
                    raise CapabilityError("Instruction returned no output slots")
                await self.finish(task_id, attempt_id, thread=thread, outputs=outputs)
        except BudgetExhausted:
            async with self.stores() as repo, repo.transaction():
                task = await repo.get_task(task_id, for_update=True)
                if task["state"] == TaskState.QUEUED and not task.get(
                    "active_attempt_id"
                ):
                    await Ledger(repo).open_interaction(
                        task_id,
                        kind="resource",
                        prompt=InteractionPrompt(
                            title="本次指令试跑预算不足",
                            question="是否追加预算继续？",
                            options=[
                                {"id": "topup", "label": "追加"},
                                {"id": "stop", "label": "停止"},
                            ],
                            subject={"budget": task["budget"]},
                        ),
                        waiting_reason=WaitingReason.RESOURCE,
                    )
        except IdempotencyConflict:
            # A concurrent drive or cancellation can win the ledger lock.
            return
        except ProvisioningUnavailable:
            await self.finish(
                task_id, attempt_id, thread=thread, code="instruction_unit_unavailable"
            )
        except ProvisionerFailed:
            # The provisioner may have applied the operation. Retry by the same UID on the next pass.
            return
        except (OSError, AppServerError):
            # A broken socket may already have started the turn. Poll its original
            # history; the absolute Attempt deadline bounds missing acknowledgements.
            return
        except (CapabilityError, NotFound, ValueError):
            await self.finish(
                task_id, attempt_id, thread=thread, code="instruction_trial_failed"
            )

    async def renew(self, owner, task_id, attempt_id, uid, turn):
        async with self.stores() as repo:
            unit = (await repo.get_attempt(attempt_id))["refs"]["instruction_unit"]
            if (
                unit.get("renew_after", 0) > datetime.now(UTC).timestamp()
                and unit.get("bound_turn") == turn
            ):
                return
        await self.bind(owner, task_id, attempt_id, uid, turn)
        await self.update_unit(
            task_id,
            attempt_id,
            renew_after=int(datetime.now(UTC).timestamp()) + 120,
            bound_turn=turn,
        )

    async def update_unit(self, task_id, attempt_id, **changes):
        async with self.stores() as repo, repo.transaction():
            await repo.get_task(task_id, for_update=True)
            attempt = await repo.get_attempt(attempt_id, for_update=True)
            refs = dict(attempt["refs"])
            refs["instruction_unit"] = {**refs["instruction_unit"], **changes}
            await repo.update_attempt(attempt_id, refs=refs)

    async def cleanup(self, owner, task_id, attempt_id):
        async with self.stores() as repo:
            attempt = await repo.get_attempt(attempt_id)
            refs = attempt["refs"]
            if attempt["status"] not in ATTEMPT_TERMINAL or not refs[
                "instruction_unit"
            ].get("pending"):
                return
        uid = await self.gateway(owner, task_id, attempt_id)
        if uid is None:
            if not refs["instruction_unit"].get("prepare_requested"):
                await self.update_unit(task_id, attempt_id, pending=False)
            return
        request = self.units.for_attempt(attempt)
        settled = {"settled_attempt": attempt_id}
        pod = await request(owner, task_id, "GET", uid, absent=True)
        if pod is not None:
            await request(owner, task_id, "POST", uid + "/stop", absent=True)
        if refs.get("usage_pending") or (
            refs.get("instruction_request") and not refs.get("instruction_usage")
        ):
            if pod is not None:
                await self.renew(
                    owner, task_id, attempt_id, uid, refs.get("runtime_turn_id")
                )
            return
        if pod is not None:
            await request(owner, task_id, "DELETE", uid, params=settled, absent=True)
            return
        await request(owner, task_id, "DELETE", uid + "/proof", params=settled)
        await self.update_unit(task_id, attempt_id, pending=False)

    async def reconcile(self, sandbox_ids):
        if not self.configured:
            return
        async with self.stores() as repo, repo.transaction():
            due = await repo.claim_instruction_cleanup(sandbox_ids)
        for row in due:
            try:
                await self.cleanup(*row)
            except ProvisionerFailed:
                continue
