"""One paid Responses request per isolated instruction Attempt.

Called by the trusted trial host. The outbound transport owns the provider key
and forwards only after the claim transaction has committed.
"""

from decimal import Decimal

from app.application.workbench.ledger import Ledger
from app.domain.workbench.capabilities import CapabilityError, digest
from app.domain.workbench.errors import IdempotencyConflict
from app.domain.workbench.formal_execution import snapshot_for_task
from app.domain.workbench.instruction_requests import (
    InstructionRoute,
    instruction_job,
    verify_instruction_body,
)
from app.domain.workbench.pricing import Price, PriceList, Usage, cost_of
from app.domain.workbench.provider_url import validate_provider_base_url
from app.domain.workbench.states import ATTEMPT_TERMINAL, AttemptState, TaskState


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

    async def _authority(self, owner, task, caller):
        snapshot = snapshot_for_task(task)
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
        session = await self.repo.get_session(
            str(task["session_id"]), owner_actor_id=owner
        )
        if session["wheel"] != "advisor" or str(session["active_task_id"]) != str(
            task["id"]
        ):
            raise CapabilityError("Instruction task does not control its session")
        credential = await self.repo.model_request_credential(owner)
        quote = self.route.quote(self.prices)
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
        }, instruction_job(request, entry, snapshot.arguments)

    async def open(self, owner, task_id, *, caller):
        async with self.repo.transaction(independent=True):
            task = await self.repo.get_task(
                task_id, owner_actor_id=owner, for_update=True
            )
            quote, _ = await self._authority(owner, task, caller)
            if (
                task["state"] != TaskState.QUEUED
                or task.get("cancel_requested_at")
                or await self.repo.list_attempts(task_id)
            ):
                raise IdempotencyConflict("Instruction task already started or closed")
            ledger = Ledger(self.repo)
            opened = await ledger.open_attempt(
                task_id,
                step_id="direct",
                step_version=task["pack_digest"],
                reserve=Decimal(quote["maximum"]),
                meta={
                    "instruction_quote": quote,
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
            quote, job = await self._authority(owner, task, caller)
            verify_instruction_body(body, job)
            if (
                attempt["status"] != AttemptState.RUNNING
                or refs.get("permission_revocation")
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
            outgoing = {**body, "max_output_tokens": limit}
            await self.repo.update_attempt(
                attempt_id,
                refs={
                    **refs,
                    "runtime_turn_id": turn_id,
                    "instruction_request": {
                        "turn_id": turn_id,
                        "digest": digest(outgoing),
                        "output_tokens": limit,
                    },
                },
            )
        return quote["base_url"] + "/responses", outgoing

    async def receipt(
        self, owner, task_id, attempt_id, *, turn_id, response_id, usage: Usage
    ):
        """Called only with complete provider usage. A lost response leaves the hold intact."""
        async with self.repo.transaction():
            await self.repo.get_task(task_id, owner_actor_id=owner, for_update=True)
            attempt = await self.repo.get_attempt(attempt_id, for_update=True)
            refs = dict(attempt["refs"])
            claim = refs.get("instruction_request")
            if (
                str(attempt["task_id"]) != task_id
                or not claim
                or claim["turn_id"] != turn_id
                or not isinstance(response_id, str)
                or not 0 < len(response_id) <= 256
            ):
                raise IdempotencyConflict(
                    "Usage does not identify the claimed instruction"
                )
            quote = refs["instruction_quote"]
            tokens = usage.as_json()
            if (
                any(type(n) is not int or n < 0 for n in tokens.values())
                or usage.output > claim["output_tokens"]
                or usage.total - usage.output > quote["context_tokens"]
            ):
                raise CapabilityError(
                    "Provider usage differs from the instruction limits"
                )
            rates = quote["rates"]
            price = Price(
                input=Decimal(rates["input"]),
                cached_input=Decimal(rates["cached_input"]),
                cache_write=Decimal(rates["cache_write"]),
                output=Decimal(rates["output"]),
            )
            amount = cost_of(usage, price)
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
            await self.repo.update_attempt(
                attempt_id, refs={**refs, "instruction_usage": bill}
            )
            if attempt["status"] in ATTEMPT_TERMINAL:
                await Ledger(self.repo).record_late_cost(
                    task_id,
                    attempt_id=attempt_id,
                    consumed=Decimal(bill["amount"]),
                    tokens=tokens,
                    turn_id=turn_id,
                )
