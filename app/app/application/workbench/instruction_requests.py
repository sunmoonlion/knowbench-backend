"""One paid Responses request per isolated instruction Attempt.

Called by the trusted trial host. The outbound transport owns the provider key
and forwards only after the claim transaction has committed.
"""

from decimal import Decimal
from uuid import UUID

from app.application.workbench.ledger import Ledger
from app.application.workbench.tokens import InstructionGateway
from app.domain.workbench.capabilities import CapabilityError, digest
from app.domain.workbench.errors import Forbidden, IdempotencyConflict
from app.domain.workbench.formal_execution import snapshot_for_task
from app.domain.workbench.instruction_requests import (
    InstructionRoute,
    instruction_job,
    instruction_usage_cost,
    leaf_responses_body,
    verify_instruction_body,
)
from app.domain.workbench.pricing import PriceList, Usage
from app.domain.workbench.provider_url import validate_provider_base_url
from app.domain.workbench.states import ATTEMPT_TERMINAL, AttemptState, TaskState


async def gateway_binding(repo, owner: UUID, task_id: UUID, attempt_id: UUID):
    """Trusted host signs only the gateway and credential pinned when opening this Attempt."""
    await repo.get_task(str(task_id), owner_actor_id=str(owner))
    attempt = await repo.get_attempt(str(attempt_id))
    refs = attempt.get("refs") or {}
    gateway = refs.get("instruction_gateway")
    if str(attempt["task_id"]) != str(task_id) or not gateway:
        raise Forbidden("Attempt has no instruction gateway binding")
    quote = refs["instruction_quote"]
    return InstructionGateway(
        owner,
        task_id,
        attempt_id,
        UUID(gateway["sandbox"]),
        UUID(gateway["uid"]),
        UUID(quote["credential_id"]),
    ), quote


async def issue_instruction_ticket(
    repo, issuer, owner: UUID, task_id: UUID, attempt_id: UUID, *, gateway_uid: UUID
):
    """The trusted scheduler supplies the UID observed for the gateway receiving the ticket."""
    binding, _ = await gateway_binding(repo, owner, task_id, attempt_id)
    if binding.gateway != gateway_uid:
        raise Forbidden(
            "Cannot deliver the instruction ticket to a replacement gateway"
        )
    return issuer.instruction_ticket(binding)


class InstructionRequests:
    def __init__(
        self,
        formal,
        route: InstructionRoute,
        prices: PriceList,
        *,
        default_base_url: str = "",
    ):
        self.formal, self.repo = formal, formal.repo
        self.route, self.prices = route, prices
        self.default_base_url = default_base_url

    @classmethod
    async def for_account(cls, formal, owner, *, routes, model, prices, default_url):
        credential = await formal.repo.model_request_credential(owner) or {}
        url = credential.get("base_url") or default_url
        if url:
            url = validate_provider_base_url(url)
            for route in routes:
                if (route.provider, route.model, route.base_url) == (
                    credential.get("provider"),
                    model,
                    url,
                ):
                    return cls(formal, route, prices, default_base_url=default_url)
        raise CapabilityError("No authorized instruction model route")

    async def authority(self, owner, task, caller):
        if task.get("pack_source") == "registry-source":
            from app.application.workbench.source_tasks import active_source
            from app.domain.workbench.source_tasks import instruction_input

            snap, content = await active_source(self.repo, owner, task)
            quote = await self.route_quote(owner, task, caller)
            if (
                caller != "registry-source"
                or snap.source.kind != "instruction"
                or {
                    k: v
                    for k, v in quote.items()
                    if k not in ("caller", "snapshot_digest")
                }
                != snap.quote
            ):
                raise CapabilityError(
                    "Registered instruction approval or route changed"
                )
            return quote, instruction_input(content, snap.arguments)
        snapshot = snapshot_for_task(task)
        if snapshot.dependencies:
            from app.application.workbench.instruction_leaf import approved_leaf

            return await approved_leaf(self, owner, task, caller)
        subject = await self.repo.subject_of_actor(owner)
        request, entry = await self.formal.prepare_instruction_trial(
            subject, snapshot.run_id, snapshot.arguments, caller=caller
        )
        if (
            request.draft != snapshot.draft
            or request.method_id != snapshot.method_id
            or snapshot.caller != "host"
        ):
            raise CapabilityError("Instruction task differs from its validated draft")
        return await self.route_quote(owner, task, caller), instruction_job(
            request, entry, snapshot.arguments
        )

    async def route_quote(self, owner, task, caller):
        session = await self.repo.get_session(
            str(task["session_id"]), owner_actor_id=owner
        )
        if session["wheel"] != "advisor" or str(session["active_task_id"]) != str(
            task["id"]
        ):
            raise CapabilityError("Instruction task does not control its session")
        credential = await self.repo.model_request_credential(owner)
        quote = self.route.quote(self.prices)
        context = (task.get("original_input") or {}).get("client_context") or {}
        if context.get("entry") == "formal-method-execution" and Decimal(
            quote["maximum"]
        ) > Decimal(task["budget"]["limit"]):
            raise CapabilityError("Instruction route exceeds the frozen case budget")
        base_url = (credential or {}).get("base_url") or self.default_base_url
        if (
            credential is None
            or credential["provider"] != self.route.provider
            or credential["sandbox_id"]
            and str(credential["sandbox_id"]) != str(session["sandbox_id"])
            or not base_url
            or validate_provider_base_url(base_url) != quote["base_url"]
            or task["budget"]["currency"] != quote["currency"]
        ):
            raise CapabilityError(
                "No active credential and tariff for this instruction route"
            )
        return {
            **quote,
            "credential_id": str(credential["id"]),
            "caller": caller,
            "snapshot_digest": task["pack_digest"],
        }

    async def open(
        self, owner, task_id, *, caller, gateway_uid: UUID | None = None, managed=False
    ):
        async with self.repo.transaction(independent=True):
            task = await self.repo.get_task(
                task_id, owner_actor_id=owner, for_update=True
            )
            if (
                task.get("pack_source") != "registry-source"
                and snapshot_for_task(task).dependencies
            ):
                raise CapabilityError(
                    "Only the fixed-plan host may open an instruction leaf"
                )
            quote, _ = await self.authority(owner, task, caller)
            if (
                task["state"] != TaskState.QUEUED
                or task.get("cancel_requested_at")
                or await self.repo.list_attempts(task_id)
            ):
                raise IdempotencyConflict("Instruction task already started or closed")
            gateway = {}
            if gateway_uid is not None:
                session = await self.repo.get_session(
                    str(task["session_id"]), owner_actor_id=owner
                )
                gateway = {
                    "instruction_gateway": {
                        "uid": str(gateway_uid),
                        "sandbox": str(UUID(str(session["sandbox_id"]))),
                    }
                }
            ledger = Ledger(self.repo)
            opened = await ledger.open_attempt(
                task_id,
                step_id="direct",
                step_version=task["pack_digest"],
                reserve=Decimal(quote["maximum"]),
                meta={
                    **gateway,
                    **(
                        {"instruction_unit": {"pending": True, "scope": "attempt"}}
                        if managed
                        else {}
                    ),
                    "instruction_quote": quote,
                    "instruction_output_version": 1,
                    "model": self.route.model,
                    "model_provider": self.route.provider,
                },
            )
            await ledger.attempt_transition(opened["attempt_id"], AttemptState.RUNNING)
            return opened["attempt_id"]

    async def claim(
        self, owner, task_id, attempt_id, *, caller, credential_id, turn_id, body
    ):
        """The winning gateway receives the exact route and output-limited body once."""
        if not isinstance(turn_id, str) or not 0 < len(turn_id) <= 128:
            raise CapabilityError("Instruction request needs its observed turn")
        async with self.repo.transaction(independent=True):
            task = await self.repo.get_task(
                task_id, owner_actor_id=owner, for_update=True
            )
            if (
                task["state"] != TaskState.RUNNING
                or task.get("cancel_requested_at")
                or str(task["active_attempt_id"]) != attempt_id
            ):
                raise CapabilityError("Instruction attempt is not active")
            attempt = await self.repo.get_attempt(attempt_id, for_update=True)
            refs = dict(attempt["refs"])
            if refs.get("instruction_request"):
                raise IdempotencyConflict("Instruction request already claimed")
            quote, job = await self.authority(owner, task, caller)
            verify_instruction_body(body, job)
            if (
                attempt["status"] != AttemptState.RUNNING
                or refs.get("instruction_unit", {}).get("protocol_error")
                or refs.get("permission_revocation")
                or (
                    "instruction_unit" in refs
                    and refs.get("runtime_turn_id") != turn_id
                )
                or refs.get("runtime_turn_id") not in (None, turn_id)
                or refs.get("instruction_quote") != quote
                or credential_id != quote["credential_id"]
                or body.get("model") != self.route.model
                or Decimal(attempt["budget_allocated"]["amount"])
                != Decimal(quote["maximum"])
            ):
                raise CapabilityError(
                    "Instruction request differs from its reserved route"
                )
            limit = body.get("max_output_tokens", self.route.output_tokens)
            if type(limit) is not int or not 0 < limit <= self.route.output_tokens:
                raise CapabilityError("Instruction output exceeds its reserved limit")
            leaf = refs.get("kind") == "formal_orchestration_step"
            outgoing = (
                leaf_responses_body(job, self.route.model, limit)
                if leaf or task.get("pack_source") == "registry-source"
                else {**body, "max_output_tokens": limit}
            )
            await self.repo.update_attempt(
                attempt_id,
                refs={
                    **refs,
                    "runtime_turn_id": turn_id,
                    "instruction_request": {
                        "turn_id": turn_id,
                        "digest": digest(outgoing),
                        "output_tokens": limit,
                        **({"job_digest": digest(job)} if leaf else {}),
                    },
                },
            )
        return quote["base_url"] + "/responses", outgoing

    async def receipt(
        self, owner, task_id, attempt_id, *, turn_id, response_id, usage: Usage
    ):
        await record_instruction_receipt(
            self.repo,
            owner,
            task_id,
            attempt_id,
            turn_id=turn_id,
            response_id=response_id,
            usage=usage,
        )


async def record_instruction_receipt(
    repo, owner, task_id, attempt_id, *, turn_id, response_id, usage: Usage
):
    """Called only with complete provider usage. A lost response leaves the hold intact."""
    async with repo.transaction():
        await repo.get_task(task_id, owner_actor_id=owner, for_update=True)
        attempt = await repo.get_attempt(attempt_id, for_update=True)
        refs = dict(attempt["refs"])
        claim = refs.get("instruction_request")
        if (
            str(attempt["task_id"]) != task_id
            or not claim
            or claim["turn_id"] != turn_id
            or not isinstance(response_id, str)
            or not 0 < len(response_id) <= 256
        ):
            raise IdempotencyConflict("Usage does not identify the claimed instruction")
        quote = refs["instruction_quote"]
        tokens = usage.as_json()
        amount = instruction_usage_cost(quote, claim, usage)
        bill = {
            "response_id": response_id,
            "turn_id": turn_id,
            "tokens": tokens,
            "amount": str(amount),
        }
        previous = refs.get("instruction_usage")
        if previous is not None:
            if previous != bill:
                raise IdempotencyConflict("Conflicting instruction usage receipt")
            return
        await repo.update_attempt(attempt_id, refs={**refs, "instruction_usage": bill})
        if attempt["status"] in ATTEMPT_TERMINAL:
            await Ledger(repo).record_late_cost(
                task_id,
                attempt_id=attempt_id,
                consumed=Decimal(bill["amount"]),
                tokens=tokens,
                turn_id=turn_id,
            )
