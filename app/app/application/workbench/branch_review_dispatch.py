"""Run a confirmed single-round review through the registered instruction executor."""

from decimal import Decimal
from uuid import UUID

from pydantic import Field

from app.application.workbench.branch_review_route import (
    RoutePreviewRequest,
    branch_route,
    prior_review,
    review_key,
)
from app.application.workbench.source_tasks import hand_over_source, resolve
from app.domain.workbench.capabilities import CapabilityError
from app.domain.workbench.errors import IdempotencyConflict
from app.domain.workbench.formal_method_generated import Digest
from app.domain.workbench.source_tasks import ReviewBinding, SourceTask


class DispatchReview(RoutePreviewRequest):
    revision: int = Field(strict=True, ge=1)
    option_digest: Digest
    session_id: UUID
    source_id: UUID
    version: int = Field(strict=True, ge=1)
    digest: Digest


async def review_projection(executions, policy, owner, binding):
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
    return checked.projection(binding)


def session_matches(row, sandbox):
    return (
        str(row["sandbox_id"]) == sandbox
        and row["kind"] == "expert"
        and row["project_id"]
        and not row.get("archived_at")
        and not row.get("origin")
    )


async def current_quote(executions, owner, sandbox):
    svc = executions.capabilities
    if not svc.instruction_service:
        raise CapabilityError("Dedicated instruction execution is unavailable")
    service = await svc.instruction_service(svc.formal_sources, owner)
    return await service.account_quote(owner, {"sandbox_id": sandbox}, "CNY")


def selected_quote(option):
    return {**option["quote"], "credential_id": option["pin"]["credential_id"]}


async def dispatch_choices(executions, owner, checked):
    page = checked.page
    repo = executions.repo
    existing = await prior_review(repo, owner, page["review_digest"])
    if existing:
        snap = SourceTask.load(existing)
        if snap.review and snap.review.review_digest == page["review_digest"]:
            session = await repo.get_session(str(snap.session_id), owner_actor_id=owner)
            return {
                "project_id": str(session["project_id"]),
                "task_id": str(existing["id"]),
                "state": existing["state"],
                "session_id": str(snap.session_id),
            }
    if (
        not page["confirmed"]
        or page["rounds"] != 1
        or page["requirement"] != "isolated_reviewer"
    ):
        return None
    sandbox_id = checked.sandbox_id
    option = next(o for o in page["options"] if o["digest"] == page["confirmed"])
    try:
        if await current_quote(executions, owner, sandbox_id) != selected_quote(option):
            return {"unavailable": "routeNotLoaded"}
    except CapabilityError:
        return {"unavailable": "executorUnavailable"}
    sessions = await repo.list_sessions(owner_actor_id=owner)
    sources = await repo.direct_registrations(owner)
    return {
        "sessions": [
            {"id": str(s["id"]), "title": s.get("title")}
            for s in sessions
            if session_matches(s, sandbox_id) and s["wheel"] == "user"
        ],
        "sources": [dict(s) for s in sources if s["kind"] == "instruction"],
    }


async def dispatch_review(executions, policy, owner, run_id, body):
    repo = executions.repo
    binding = ReviewBinding(
        run_id=run_id,
        **body.model_dump(exclude={"session_id", "source_id", "digest", "version"}),
    )
    async with repo.transaction():
        # Serialize replays even after the initiating browser has lost the reply.
        option, sandbox, _ = await review_projection(executions, policy, owner, binding)
        prior = await prior_review(repo, owner, body.review_digest)
        if prior:
            snap = SourceTask.load(prior)
            if (
                snap.review != binding
                or snap.session_id != body.session_id
                or (snap.source.source_id, snap.source.version, snap.source.digest)
                != (body.source_id, body.version, body.digest)
            ):
                raise IdempotencyConflict("This review already has its own task")
            return {"task_id": str(prior["id"]), "state": prior["state"]}
        session = await repo.get_session(
            str(body.session_id), owner_actor_id=owner, for_update=True
        )
        if not session_matches(session, sandbox):
            raise CapabilityError(
                "Choose an expert project session on the confirmed sandbox"
            )
        source, _ = await resolve(repo, owner, body)
        quote = await current_quote(executions, owner, sandbox)
        if (
            source.kind != "instruction"
            or quote != selected_quote(option)
            or Decimal(quote["maximum"]) <= 0
        ):
            raise CapabilityError(
                "The registered instruction or loaded route is unavailable"
            )
        snap = SourceTask(
            source=source,
            arguments={},
            budget_limit=Decimal(quote["maximum"]),
            quote=quote,
            owner=owner,
            session_id=body.session_id,
            review=binding,
        )
        task = await hand_over_source(repo, owner, snap, review_key(body.review_digest))
        return {"task_id": task["task_id"], "state": task["state"]}
