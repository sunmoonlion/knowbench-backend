"""Confirm a planned destination for one already received and V0-checked artifact."""

from dataclasses import dataclass
from decimal import Decimal

from app.domain.workbench.capabilities import CapabilityError
from app.domain.workbench.errors import NotFound, StaleStateVersion
from app.domain.workbench.formal_execution import case_key
from app.domain.workbench.formal_method_execution_generated import (
    Digest,
    ReceiveBranch,
    fingerprint,
)
from app.domain.workbench.review_routes import RouteKey


class RoutePreviewRequest(ReceiveBranch):
    review_digest: Digest


@dataclass
class CheckedReview:
    page: dict
    sandbox_id: str | None
    first_round: dict
    assessment: dict | None

    def projection(self, binding):
        if (
            binding.option_digest != self.page["confirmed"]
            or binding.revision != self.page["revision"]
            or (
                self.page["requirement"] == "different_model_family"
                and not self.page.get("verified_producer")
            )
        ):
            raise StaleStateVersion(
                "Review confirmation changed or requires verified model families"
            )
        option = next(
            o for o in self.page["options"] if o["digest"] == binding.option_digest
        )
        return option, self.sandbox_id, self.first_round


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
        verified = None
        if scope["requirement"] == "different_model_family":
            verified = await repo.sealed_artifact_model(
                owner,
                run_id,
                command.case_id,
                command.repetition,
                command.step,
                delivery["artifact"],
            )
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
                repo,
                owner,
                scope["requirement"],
                sandbox=scope["sandbox_id"],
                producer_route=RouteKey(**verified) if verified else None,
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
                    **(
                        {"verified_producer": producer}
                        if verified and producer and producer.get("family")
                        else {}
                    ),
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
        recommended = next(
            (o["digest"] for o in options if o["pin"] == preference), None
        )
        if not preference and len(options) == 1:
            recommended = options[0]["digest"]
        if not confirmed and (previous or (preference and not recommended)):
            recommended = None
            gap = gap or "selection_changed"
        page = {
            **(
                {"verified_producer": producer}
                if verified and producer and producer.get("family")
                else {}
            ),
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
        }
        return CheckedReview(
            page,
            scope["sandbox_id"],
            {name: delivery["outputs"][name] for name in scope["handoff"]},
            {
                review["deferred"]["assessment_output"]: delivery["outputs"][
                    review["deferred"]["assessment_output"]
                ]
            }
            if scope["deferred"]
            else None,
        )


async def review_projection(
    executions, policy, owner, binding, *, comparison_task=None
):
    selector = RoutePreviewRequest.model_validate(
        binding.model_dump(include=set(RoutePreviewRequest.model_fields))
    )
    checked = await branch_route(
        executions,
        policy,
        owner,
        str(binding.run_id),
        selector,
    )
    option, sandbox, inputs = checked.projection(binding)
    if comparison_task is not None:
        from app.application.workbench.review_comparison import comparison_arguments

        if checked.assessment is None:
            raise CapabilityError("The confirmed review has no deferred assessment")
        inputs = await comparison_arguments(
            executions.repo, owner, binding, comparison_task, inputs, checked.assessment
        )
    return option, sandbox, inputs


def selected_quote(option, base):
    quote = {**option["quote"], "credential_id": option["pin"]["credential_id"]}
    if quote != base:
        quote["review_connection"] = True
    if producer := option.get("verified_producer"):
        if not option.get("family") or producer["family"] == option["family"]:
            raise CapabilityError(
                "Verified reviewer family must differ from the producer"
            )
        quote.update(family=option["family"], producer_family=producer["family"])
    return quote
