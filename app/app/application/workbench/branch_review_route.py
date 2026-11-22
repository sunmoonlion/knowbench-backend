"""Confirm a planned destination for one already received and V0-checked artifact."""

from decimal import Decimal

from pydantic import Field

from app.domain.workbench.errors import NotFound, StaleStateVersion
from app.domain.workbench.formal_execution import case_key
from app.domain.workbench.formal_method_execution_generated import (
    Digest,
    ReceiveBranch,
    fingerprint,
)


class RoutePreviewRequest(ReceiveBranch):
    review_digest: Digest


class RouteConfirmation(RoutePreviewRequest):
    previous_revision: int = Field(strict=True, ge=0)
    option_digest: Digest


async def branch_route(executions, policy, owner, run_id, command):
    repo = executions.repo
    async with repo.transaction():
        subject = await repo.subject_of_actor(owner)
        if not subject or await repo.actor_for_subject(subject) != owner:
            raise NotFound("No such author")
        selector = ReceiveBranch.model_validate(
            command.model_dump(include=set(ReceiveBranch.model_fields))
        )
        delivery = await executions.receive_branch(
            subject, run_id, selector, caller="knowlab", received_only=True
        )
        review = delivery.get("review")
        if not review or review["digest"] != command.review_digest:
            raise StaleStateVersion("The reviewed artifact or criteria changed")
        key = case_key(command.case_id, command.repetition)
        last = await repo.latest_review_route(run_id, key, command.step)
        revision = last["revision"] if last else 0
        sandbox = await repo.current_sandbox(owner)
        scope = {
            "owner": owner,
            "run_id": run_id,
            **selector.model_dump(mode="json"),
            "review_digest": review["digest"],
            "artifact": delivery["artifact"]["id"],
            "version": delivery["artifact"]["version"],
            "handoff": review["handoff"],
            "deferred": review.get("deferred"),
            "requirement": review["criteria"]["reviewer"],
            "sandbox_id": str(sandbox["id"]) if sandbox else None,
        }
        rounds = 2 if scope["deferred"] else 1
        options = []
        gap = None
        preference = None
        producer = None
        if review["status"] != "awaiting_isolation":
            gap = "v0_failed"
        elif not sandbox:
            gap = "sandbox_unavailable"
        else:
            view = await policy.choices(
                repo, owner, scope["requirement"], sandbox=scope["sandbox_id"]
            )
            preference, producer, gap = (
                view["preference"],
                view["producer"],
                view["gap"],
            )
            for option in view["options"]:
                quote = option["quote"]
                if quote is None or quote["currency"] != "CNY":
                    continue
                candidate = {
                    **option,
                    "planned_producer": producer,
                    "rounds": rounds,
                    "maximum": str(Decimal(quote["maximum"]) * rounds),
                }
                options.append({**candidate, "digest": fingerprint([scope, candidate])})
            if not options and not gap:
                gap = "quote_unavailable"
        previous = last["scope"]["option"]["digest"] if last else None
        valid = {o["digest"]: o for o in options}
        confirmed = previous if previous in valid else None
        if isinstance(command, RouteConfirmation):
            option = valid.get(command.option_digest)
            if not option or (
                command.previous_revision != revision
                and not (
                    confirmed == command.option_digest
                    and command.previous_revision == revision - 1
                )
            ):
                raise StaleStateVersion(
                    "Review scope, route, price or confirmation changed"
                )
            if not await repo.lock_active_credential(
                owner, option["pin"]["credential_id"]
            ):
                raise StaleStateVersion("Review connection revoked")
            if confirmed != command.option_digest:
                revision += 1
                await repo.record_review_route(
                    run_id, key, command.step, revision, {**scope, "option": option}
                )
                confirmed = command.option_digest
        recommended = next(
            (o["digest"] for o in options if o["pin"] == preference), None
        )
        if not preference and len(options) == 1:
            recommended = options[0]["digest"]
        if not confirmed and (previous or (preference and not recommended)):
            recommended = None
            gap = gap or "selection_changed"
        return {
            "artifact": scope["artifact"],
            "version": scope["version"],
            "review_digest": review["digest"],
            "handoff": scope["handoff"],
            "deferred": scope["deferred"],
            "requirement": scope["requirement"],
            "options": options,
            "planned_producer": producer,
            "rounds": rounds,
            "revision": revision,
            "confirmed": confirmed,
            "recommended": recommended,
            "gap": gap,
            "isolated_execution_available": False,
        }
