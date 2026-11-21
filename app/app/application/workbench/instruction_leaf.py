"""Bind a single fixed-plan inference to its private method, input versions and approved route."""

from datetime import UTC, datetime

from app.application.workbench.artifact_read_authority import EVENT, check, planned_read
from app.application.workbench.formal_orchestration_execution import (
    step_call_key,
    step_context,
)
from app.domain.workbench.capabilities import CapabilityError, digest
from app.domain.workbench.formal_execution import validate_values
from app.domain.workbench.formal_host import validate_host_call
from app.domain.workbench.instruction_requests import instruction_job
from app.domain.workbench.method_authorization import (
    MethodAuthorization,
    check_delegation,
)
from app.domain.workbench.method_capabilities import AccessRequirement
from app.domain.workbench.tool_registry import ToolRegistration


async def leaf_input(svc, owner, task, attempt, snapshot, frozen, child, arguments):
    formal, repo = svc.formal, svc.repo
    source = frozen.draft.source
    subject = await repo.subject_of_actor(owner)
    procedure = frozen.draft.definition.procedure
    if (
        frozen.owner_subject != subject
        or frozen.visibility != "private"
        or source is None
        or source.kind != "skill_instruction"
        or source.license_basis == "pending"
        or frozen.draft.allowed_callers != ("host",)
        or procedure.kind != "direct"
        or procedure.max_model_turns != 1
        or frozen.draft.definition.access_requirements
        or frozen.draft.definition.what_requires_approval
        or procedure.implementation.requirement_ids
    ):
        raise CapabilityError(
            "A fixed instruction step needs an owned, licensed single-turn method"
        )
    asset = await formal._installed_source(owner, subject, source)
    if asset["detail"].get("tool_requirements") or asset["detail"].get("dependencies"):
        raise CapabilityError("Instruction requires additional tools")
    job = instruction_job(
        frozen,
        await formal._instruction_text(owner, asset),
        validate_values(procedure.inputs, arguments),
    )
    quote = await svc.route_quote(owner, task, "knowlab")
    if quote != attempt["refs"].get("instruction_quote"):
        raise CapabilityError("Instruction route differs from the reserved tariff")
    registration, requirement, operation = leaf_operation(
        frozen, arguments, quote, digest(job)
    )
    binding = await planned_read(
        repo,
        task,
        snapshot,
        child,
        attempt,
        arguments,
        requirement["data_transfer"],
        include_empty=True,
    )
    return job, asset, quote, registration, requirement, operation, binding


def leaf_operation(frozen, arguments, quote, job_digest):
    procedure = frozen.draft.definition.procedure
    source = frozen.draft.source
    requirement = AccessRequirement.model_validate(
        {
            "requirement_id": "model_request",
            "capability_ref": "instruction.responses",
            "version": "1",
            "operations": ["inference"],
            "resources": [
                {"resource_role": "credential", "values": [quote["credential_id"]]}
            ],
            "data_transfer": {
                "purpose": "执行本步骤的一次模型推理",
                "destination": quote["base_url"] + "/responses",
                "argument_fields": sorted(arguments),
            },
        }
    ).model_dump(mode="json")
    registration = ToolRegistration.discover(
        "skill_instruction",
        source.digest,
        {
            "name": "instruction_responses",
            "description": "Single scoped model request",
            "inputSchema": {
                "type": "object",
                "properties": {k: v.schema_ for k, v in procedure.inputs.items()},
                "additionalProperties": False,
            },
        },
    )
    operation = {
        k: requirement[k]
        for k in ("capability_ref", "version", "resources", "data_transfer")
    }
    operation.update(
        operation="inference",
        effect="transfer",
        job_digest=job_digest,
        quote_digest=digest(quote),
    )
    return registration, requirement, operation


async def approved_leaf(svc, owner, task, caller):
    repo = svc.repo
    attempt = await repo.get_attempt(str(task["active_attempt_id"]), for_update=True)
    refs = attempt["refs"]
    snapshot, _, frozen, arguments = await step_context(repo, task, attempt)
    run = await repo.test_run_for_task(str(task["id"]))
    if (
        caller != "knowlab"
        or not run
        or run["caller"] != caller
        or run.get("cancel_requested_at")
        or run["status"] not in ("accepted", "running", "waiting")
        or run["digest"] != snapshot.request_digest.removeprefix("sha256:")
        or str(run["run_id"]) != str(snapshot.run_id)
    ):
        raise CapabilityError("Instruction leaf has no active author test")
    from app.application.workbench.formal_method_catalog import FormalMethodCatalog

    catalog = FormalMethodCatalog(repo, svc.formal, svc.formal.operations)
    if (
        await catalog.resolve_owned_admission(owner, frozen.reference, resolved={})
        != frozen
    ):
        raise CapabilityError("Instruction dependency qualification changed")
    call_id = refs.get("capability_call_id")
    if not call_id:
        raise CapabilityError("Instruction leaf has no approved external call")
    call = await repo.capability_call(owner, call_id, lock=True)
    validate_host_call(task, attempt, call)
    child = await repo.call_by_key(
        owner, str(task["session_id"]), step_call_key(str(task["id"]), refs["slot"])
    )
    if child is None or child["id"] != refs["method_call_id"]:
        raise CapabilityError("Instruction recipient differs from its approved child")
    job, asset, quote, registration, requirement, operation, binding = await leaf_input(
        svc, owner, task, attempt, snapshot, frozen, child, arguments
    )
    intent = call["detail"]
    interaction = await repo.get_interaction(str(call["interaction_id"]))
    grant_ref = intent.get("authorization") or {}
    raw = await repo.method_authorization(owner, child["id"], lock=True)
    parent = await repo.method_authorization(owner, child["parent_call_id"], lock=True)
    if not raw or not parent or call["status"] != "dispatching":
        raise CapabilityError("Instruction leaf has no current parent authorization")
    auth = MethodAuthorization.model_validate(raw)
    check_delegation(MethodAuthorization.model_validate(parent), auth)
    grant = next(
        (g for g in auth.grants if g.grant_id == digest([call["id"], call["digest"]])),
        None,
    )
    if (
        not grant
        or grant.recipient_call_id != auth.call_id
        or str(auth.call_id) != child["id"]
        or str(auth.parent_call_id) != child["parent_call_id"]
        or auth.method_id != str(frozen.reference.method_id)
        or grant_ref != {"call_id": child["id"], "revision": auth.revision}
        or not grant.matches_asset("skill_instruction", asset["id"], asset["revision"])
        or grant.confirmation_context != task["pack_digest"]
        or grant.operation_digest != digest(operation)
        or grant.requirement.model_dump(mode="json") != requirement
        or intent["operation"] != operation
        or intent["instruction_quote"] != quote
        or intent["artifact_read_digest"] != digest(binding)
        or call["registration_digest"] != registration.descriptor_digest
        or call["source_asset_id"] != str(asset["id"])
        or interaction["status"] != "consumed"
        or str(interaction["responded_by"]) != owner
        or str(interaction["attempt_id"]) != str(attempt["id"])
        or interaction["subject_digest"] != call["digest"]
        or interaction.get("response", {}).get("decision") != "accept"
        or interaction["expires_at"] <= datetime.now(UTC)
    ):
        raise CapabilityError(
            "Instruction egress differs from the exact approved invocation"
        )
    state = await repo.latest_call_event(call, EVENT)
    if not state:
        raise CapabilityError("Missing instruction input transfer authority")
    check(state, call, binding, interaction["expires_at"])
    return quote, job


async def cancel_leaf_call(repo, owner, attempt):
    """Task and Attempt are locked in the same order as the gateway claim."""
    refs = attempt["refs"]
    if not refs.get("capability_call_id"):
        return
    call = await repo.capability_call(owner, refs["capability_call_id"], lock=True)
    if (
        call["detail"].get("execution_kind") != "formal_instruction"
        or str(call["attempt_id"]) != str(attempt["id"])
        or call["status"] not in ("awaiting_approval", "dispatching")
    ):
        return
    claimed = bool(refs.get("instruction_request"))
    status = "unknown" if claimed else "failed"
    if call["status"] == "awaiting_approval":
        status = "expired"
    reason = (
        "cancelled_after_model_request" if claimed else "cancelled_before_model_request"
    )
    await repo.update_call(
        owner, call["id"], status, {**call["detail"], "result": {"error": reason}}
    )
