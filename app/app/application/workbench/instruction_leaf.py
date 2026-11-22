"""Bind a single fixed-plan inference to its private method, input versions and approved route."""

from datetime import UTC, datetime

from app.application.workbench.artifact_read_authority import EVENT, check, planned_read
from app.application.workbench.formal_orchestration_execution import (
    step_call_key,
    step_context,
)
from app.domain.workbench.capabilities import CapabilityError, digest
from app.domain.workbench.formal_execution import (
    dependency_for_step,
    execution_slot,
    expanded_plan,
    recursive_plan,
    validate_values,
)
from app.domain.workbench.formal_host import validate_host_call
from app.domain.workbench.formal_method_generated import ReviewPhaseStep
from app.domain.workbench.instruction_requests import instruction_job
from app.domain.workbench.method_authorization import (
    MethodAuthorization,
    check_delegation,
)
from app.domain.workbench.method_capabilities import AccessRequirement
from app.domain.workbench.ordinary_expert import ORDINARY_SOURCE, ConsumerPlan
from app.domain.workbench.tool_registry import ToolRegistration


async def leaf_input(svc, owner, task, attempt, snapshot, frozen, child, arguments):
    formal, repo = svc.formal, svc.repo
    source = frozen.draft.source
    subject = await repo.subject_of_actor(owner)
    procedure = frozen.draft.definition.procedure
    if (
        (
            not isinstance(snapshot, ConsumerPlan)
            and (frozen.owner_subject != subject or frozen.visibility != "private")
        )
        or source is None
        or not source.is_instruction
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
    asset, entry = await formal.instruction_source(owner, subject, source)
    active_step = expanded_plan(snapshot).leaf_nodes[int(task["current_step"])].step
    slots = phase_inputs(frozen, active_step)
    job = instruction_job(
        frozen,
        entry,
        validate_values(slots, arguments),
    )
    if isinstance(active_step, ReviewPhaseStep):
        job["input"]["phase"] = active_step.phase
        job["input"]["handoff"] = {
            "blind": "Record independent findings using only the received outputs.",
            "comparison": "Compare each sealed finding with the author assessment and the approved outputs.",
            "revision": "Revise the original submission using the sealed review findings.",
        }[active_step.phase]
    quote = await leaf_quote(svc, owner, task, snapshot)
    if quote != attempt["refs"].get("instruction_quote"):
        raise CapabilityError("Instruction route differs from the reserved tariff")
    registration, requirement, operation = leaf_operation(
        frozen, arguments, quote, digest(job), step=active_step
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


async def leaf_quote(svc, owner, task, snapshot):
    ordinary = isinstance(snapshot, ConsumerPlan)
    from app.application.workbench.ordinary_local_tools import graph_route
    from app.domain.workbench.formal_method_plan_generated import review_group

    slot = execution_slot(snapshot, int(task["current_step"]))
    procedure = snapshot.draft.definition.procedure
    edge = review_group(procedure)
    active_step = expanded_plan(snapshot).leaf_nodes[int(task["current_step"])].step
    role = active_step.role if isinstance(active_step, ReviewPhaseStep) else slot
    caller = ORDINARY_SOURCE if ordinary else "knowlab"
    quote = await svc.route_quote(owner, task, caller)
    if edge and procedure.kind == "orchestration" and procedure.collaboration:
        session = await svc.repo.get_session(
            str(task["session_id"]), owner_actor_id=owner
        )
        requirement = edge.requirement if role == edge.reviewer else None
        route = await graph_route(
            svc,
            owner,
            session,
            task["budget"]["currency"],
            requirement=requirement,
            producer=role in edge.producers,
        )
        if (
            ordinary
            and isinstance(active_step, ReviewPhaseStep)
            and active_step.phase == "revision"
        ):
            reviewer = await graph_route(
                svc,
                owner,
                session,
                task["budget"]["currency"],
                requirement=edge.requirement,
            )
            policies = snapshot._review_rounds[active_step.iteration - 1]["policies"]
            if policies.get(edge.reviewer) != digest(reviewer):
                raise CapabilityError(
                    "The next review route changed before the revision was sent"
                )
        if requirement == "different_model_family":
            required = (
                (active_step.producer,)
                if isinstance(active_step, ReviewPhaseStep)
                else edge.different_family_from
            )
            attempts = await svc.repo.list_attempts(str(task["id"]))
            for producer in required:
                completed = [
                    a
                    for a in attempts
                    if a["step_id"] == producer and a["status"] == "COMPLETED"
                ]
                if len(completed) != 1:
                    raise CapabilityError("No sealed producer identity")
                refs = completed[0]["refs"]
                prior = refs["instruction_quote"]
                if (
                    prior.get("family") != route.get("producer_family")
                    or (refs.get("instruction_usage") or {}).get("model")
                    != prior["model"]
                ):
                    raise CapabilityError(
                        "The producer model family has no matching supplier receipt"
                    )
        quote = {**route, "caller": caller, "snapshot_digest": task["pack_digest"]}
    if ordinary:
        basis = {
            k: v for k, v in quote.items() if k not in ("snapshot_digest", "caller")
        }
        expected = snapshot.policies.get(slot)
        if isinstance(active_step, ReviewPhaseStep) and active_step.iteration:
            expected = snapshot._review_rounds[active_step.iteration - 1][
                "policies"
            ].get(active_step.template)
        if digest(basis) != expected:
            raise CapabilityError(
                "Instruction account, route or price changed since confirmation"
            )
    return quote


def phase_inputs(frozen, step):
    if isinstance(step, ReviewPhaseStep):
        return {k: port.slot for k, port in step.inputs.items()}
    return frozen.draft.definition.procedure.inputs


def leaf_operation(frozen, arguments, quote, job_digest, *, step=None):
    slots = phase_inputs(frozen, step)
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
        source.kind,
        source.digest,
        {
            "name": "instruction_responses",
            "description": "Single scoped model request",
            "inputSchema": {
                "type": "object",
                "properties": {k: v.schema_ for k, v in slots.items()},
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


async def approved_leaf(svc, owner, task, caller, *, grants=None):
    repo = svc.repo
    attempt = await repo.get_attempt(str(task["active_attempt_id"]), for_update=True)
    refs = attempt["refs"]
    snapshot, _, frozen, arguments = await step_context(repo, task, attempt)
    ordinary = isinstance(snapshot, ConsumerPlan)
    if ordinary:
        from app.application.workbench.ordinary_expert_execution import check_execution

        if caller != ORDINARY_SOURCE:
            raise CapabilityError("Wrong caller for ordinary instruction")
        await check_execution(svc.formal, owner, task, snapshot, grants)
    else:
        from app.domain.workbench.formal_execution import FormalTaskSnapshot

        run = await repo.test_run_for_task(str(task["id"]))
        if (
            not isinstance(snapshot, FormalTaskSnapshot)
            or caller != "knowlab"
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
    if recursive_plan(snapshot):
        from app.application.workbench.formal_recursive_runtime import execution_scopes
        from app.domain.workbench.formal_recursive import node_key

        scopes = await execution_scopes(repo, task, snapshot)
        path = tuple(refs["path"])
        if not ordinary:
            nodes = {node.path: node for node in expanded_plan(snapshot).nodes}
            for depth in range(1, len(path)):
                ancestor = dependency_for_step(snapshot, nodes[path[:depth]].step)
                live, _ = await svc.formal.registry.dependency(
                    owner, ancestor.reference, shared=False
                )
                if live != ancestor:
                    raise CapabilityError("Instruction ancestor registration changed")
        grant_parent_id = str(scopes[0].call_id)
        key = node_key(task["id"], refs["path"])
    else:
        grant_parent_id = None
        key = step_call_key(str(task["id"]), refs["slot"])
    child = await repo.call_by_key(owner, str(task["session_id"]), key)
    if child is None or child["id"] != refs["method_call_id"]:
        raise CapabilityError("Instruction recipient differs from its approved child")
    job, asset, quote, registration, requirement, operation, binding = await leaf_input(
        svc, owner, task, attempt, snapshot, frozen, child, arguments
    )
    source = frozen.draft.source
    assert source is not None  # leaf_input verified the exact instruction source.
    intent = call["detail"]
    interaction = await repo.get_interaction(str(call["interaction_id"]))
    grant_ref = intent.get("authorization") or {}
    raw = await repo.method_authorization(owner, child["id"], lock=True)
    grant_parent_id = grant_parent_id or child["parent_call_id"]
    parent = await repo.method_authorization(owner, grant_parent_id, lock=True)
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
        or str(auth.parent_call_id) != grant_parent_id
        or auth.method_id != str(frozen.reference.method_id)
        or grant_ref != {"call_id": child["id"], "revision": auth.revision}
        or not grant.matches_asset(source.kind, asset["id"], asset["revision"])
        or grant.confirmation_context != task["pack_digest"]
        or grant.operation_digest != digest(operation)
        or grant.requirement.model_dump(mode="json") != requirement
        or intent["operation"] != operation
        or intent["instruction_quote"] != quote
        or intent["artifact_read_digest"] != digest(binding)
        or call["registration_digest"] != registration.descriptor_digest
        or str(call["source_asset_id"]) != str(source.asset_id)
        or (
            source.is_registered
            and not (
                intent.get("registered_source")
                == interaction["prompt"]["subject"].get("registered_source")
                == source.model_dump(mode="json")
            )
        )
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
