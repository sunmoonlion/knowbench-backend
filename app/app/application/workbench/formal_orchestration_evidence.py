"""Reconcile an expert's complete execution against persisted calls and artifacts.

No claim in a result or an author definition creates authority. This adapter
qualifies only complete successful, one-level host executions. Failed and
partial executions retain their observed step identities without filling gaps.
"""

from decimal import Decimal, InvalidOperation

from app.application.workbench.script_budget import task_script_cost
from app.domain.workbench.capabilities import digest
from app.domain.workbench.errors import WorkbenchError
from app.domain.workbench.formal_adapter import host_kind, retained_registration
from app.domain.workbench.formal_execution import (
    case_key,
    dependency_for_step,
    orchestration_plan,
    snapshot_for_task,
    structured_outputs,
)
from app.domain.workbench.formal_host import host_intent_digest
from app.domain.workbench.formal_method_execution_generated import (
    bounded_json,
    evaluate_assertions,
    fingerprint,
)
from app.domain.workbench.formal_method_plan_generated import (
    bind_step_inputs,
    export_outputs,
    validate_plan_inputs,
    validate_step_outputs,
)
from app.domain.workbench.method_authorization import MethodAuthorization
from app.domain.workbench.states import AttemptState, TaskState
from app.domain.workbench.tool_operations import FormalExecutionCost


def _require(condition, code):
    if not condition:
        raise ValueError(code)


def _zero(value):
    number = Decimal(str(value))
    return number.is_finite() and number == 0


async def _approval(
    repo,
    owner,
    task,
    attempt,
    child,
    call,
    leaf,
    inputs,
    root,
    events,
    *,
    authorization_deadline=None,
):
    intent = call["detail"]
    interaction = await repo.get_interaction(str(call["interaction_id"]))
    # Recursive scopes have a fixed run deadline. The grant must use exactly
    # this minimum, not an arbitrary shorter lifetime; legacy calls keep the
    # interaction expiry unchanged.
    authorization_expiry = (
        min(interaction["expires_at"], authorization_deadline)
        if authorization_deadline is not None
        else interaction["expires_at"]
    )
    consumed = interaction.get("consumed_at")
    subject = (interaction.get("prompt") or {}).get("subject") or {}
    _require(
        str(interaction["task_id"]) == str(task["id"])
        and str(interaction["attempt_id"]) == str(attempt["id"])
        and str(interaction["session_id"]) == str(task["session_id"])
        and interaction["kind"] == "tool_approval"
        and interaction["subject_digest"] == call["digest"]
        and subject.get("capability_call_id") == str(call["id"])
        and fingerprint(subject.get("arguments")) == fingerprint(inputs)
        and interaction["status"] == "consumed"
        and (interaction.get("response") or {}).get("decision") == "accept"
        and str(interaction.get("responded_by")) == str(owner)
        and consumed is not None
        and interaction["created_at"] <= consumed < interaction["expires_at"],
        "exact_action_approval_missing",
    )
    assert consumed is not None
    matches = [
        event
        for event in events
        if str(event.get("task_id")) == str(task["id"])
        and str(event.get("attempt_id")) == str(attempt["id"])
        and event["payload"].get("interaction_id") == str(interaction["id"])
        and event["payload"].get("action_digest") == call["digest"]
    ]
    _require(len(matches) == 1, "authorization_confirmation_event_missing")
    payload = matches[0]["payload"]
    source = snapshot_for_task(task).execution_source(leaf)
    assert source is not None
    inference = source.is_instruction
    if source.is_registered:
        _require(
            subject.get("registered_source")
            == intent.get("registered_source")
            == source.model_dump(mode="json"),
            "registered_source_approval_mismatch",
        )
    if inference:
        from app.application.workbench.instruction_leaf import leaf_operation

        quote = attempt["refs"]["instruction_quote"]
        _, declared, expected_operation = leaf_operation(
            leaf, inputs, quote, attempt["refs"]["instruction_request"]["job_digest"]
        )
        _require(
            intent["operation"] == expected_operation
            and subject.get("instruction_quote") == quote
            and intent["instruction_quote"] == quote
            and subject.get("data_transfer") == declared["data_transfer"],
            "instruction_transfer_mismatch",
        )
    else:
        declared = leaf.draft.definition.access_requirements[0].model_dump(mode="json")
    resource = None
    if (
        "artifact_read_digest" in intent
        or "artifact_read_digest" in payload
        or "artifact_read" in subject
    ):
        from app.application.workbench.artifact_read_authority import (
            planned_read,
            verify_approval,
        )

        transfer = declared["data_transfer"]
        snapshot = snapshot_for_task(task)
        if "return_to" in (subject.get("artifact_read") or {}):
            from app.application.workbench.task_output_reader import read_binding

            binding = await read_binding(
                repo,
                task,
                snapshot,
                inputs,
                transfer,
                child=child,
                attempt=attempt,
                result=intent["result"],
            )
        else:
            binding = await planned_read(
                repo,
                task,
                snapshot,
                child,
                attempt,
                inputs,
                transfer,
                include_empty=inference,
            )
        resource = await verify_approval(
            repo,
            task,
            attempt,
            call,
            binding,
            interaction,
            matches[0],
            deadline=authorization_expiry,
        )
    auth = MethodAuthorization.model_validate(payload["authorization"])
    parent = MethodAuthorization.model_validate(payload["parent_authorization"])
    current = MethodAuthorization.model_validate(
        await repo.method_authorization(owner, str(child["id"]))
    )
    current_parent = MethodAuthorization.model_validate(
        await repo.method_authorization(owner, str(root["id"]))
    )
    _require(
        str(auth.call_id) == str(child["id"])
        and str(auth.parent_call_id) == str(root["id"])
        and auth.parent_revision == parent.revision
        and str(parent.call_id) == str(root["id"])
        and parent.parent_call_id is None
        and parent.method_id == str(snapshot_for_task(task).method_id)
        and auth.method_id == str(leaf.reference.method_id)
        and not auth.revoked
        and not parent.revoked
        and len(auth.grants) == 1
        and consumed < auth.expires_at <= parent.expires_at
        and auth.expires_at == authorization_expiry
        and payload.get("context") == task["pack_digest"]
        and intent.get("authorization")
        == {"call_id": str(child["id"]), "revision": auth.revision},
        "action_authorization_identity_mismatch",
    )
    # Current revocation/expiry does not rewrite historical action authority.
    # Exact frozen grants and lineage must still be present, including in the
    # parent's accumulated direct grants; new grants cannot enlarge this one.
    _require(
        current.call_id == auth.call_id
        and current.parent_call_id == auth.parent_call_id
        and current.parent_revision == auth.parent_revision
        and current.method_id == auth.method_id
        and current.expires_at == auth.expires_at
        and current.grants == auth.grants
        and current.revision >= auth.revision
        and current_parent.call_id == parent.call_id
        and current_parent.parent_call_id is None
        and current_parent.method_id == parent.method_id
        and current_parent.revision >= parent.revision,
        "persisted_authorization_mismatch",
    )
    grant = auth.grants[0]
    _require(
        grant in parent.grants and grant in current_parent.grants,
        "parent_direct_grant_missing",
    )
    access, operation = intent["access"], intent["operation"]
    exact = {
        **declared,
        "operations": [operation["operation"]],
        "resources": operation["resources"],
        "data_transfer": {
            **declared["data_transfer"],
            "argument_fields": sorted(inputs),
        },
    }
    source = snapshot_for_task(task).execution_source(leaf)
    assert source is not None
    _require(
        access["requirement"] == declared
        and (digest(operation) if inference else operation["policy_digest"])
        == access["operation_digest"]
        and operation["capability_ref"] == declared["capability_ref"]
        and operation["version"] == declared["version"]
        and operation["operation"] in declared["operations"]
        and operation["data_transfer"]
        == (
            declared["data_transfer"]
            if inference
            else {
                "destination": declared["data_transfer"]["destination"]
                if source.kind in ("skill_script", "registered_script")
                else source.source,
                "arguments_digest": digest(inputs),
            }
        )
        and grant.grant_id == digest([str(call["id"]), call["digest"]])
        and not grant.delegable
        and grant.resource_authority is None
        and grant.recipient_call_id == auth.call_id
        and grant.confirmation_context == task["pack_digest"]
        and grant.matches_asset(
            source.kind, source.authorization_id, source.authorization_revision
        )
        and grant.operation_digest == access["operation_digest"]
        and grant.method_ids == (str(leaf.reference.method_id),)
        and grant.requirement.model_dump(mode="json") == exact
        and consumed < grant.expires_at == authorization_expiry,
        "exact_child_direct_grant_missing",
    )
    metering = {}
    if source.kind in ("skill_script", "registered_script"):
        from app.application.workbench.script_budget import cost_proof

        metering["script_cost"] = await cost_proof(repo, call, subject)
    elif not inference:
        FormalExecutionCost.model_validate(operation["formal_execution_cost"])
    return {
        **metering,
        "operation": operation,
        "call_id": str(call["id"]),
        "method_call_id": str(child["id"]),
        "root_call_id": str(root["id"]),
        "input_digest": digest(inputs),
        "registration_id": call["registration_id"],
        "registration_digest": call["registration_digest"],
        "formal_host": intent["formal_host"],
        **({"artifact_read": resource} if resource else {}),
        "direct_grants": True,
        "approved": True,
        "authorization": auth.model_dump(mode="json"),
        "authorization_digest": digest(auth.model_dump(mode="json")),
        "parent_authorization": parent.model_dump(mode="json"),
        "interaction_id": str(interaction["id"]),
        "approval": {
            "subject_digest": interaction["subject_digest"],
            "decision": "accept",
            "responded_by": str(owner),
            "consumed_at": consumed.isoformat(),
            "expires_at": interaction["expires_at"].isoformat(),
        },
    }


def step_registration(leaf, inputs, refs, intent):
    if leaf.draft.source.is_instruction:
        from app.application.workbench.instruction_leaf import leaf_operation

        registration, _, _ = leaf_operation(
            leaf,
            inputs,
            refs["instruction_quote"],
            refs["instruction_request"]["job_digest"],
        )
        _require(
            intent["tool"] == registration.descriptor(),
            "instruction_descriptor_mismatch",
        )
    else:
        registration = retained_registration(leaf.draft, intent["tool"])
    return registration


async def instruction_step_proof(repo, task, attempt, leaf, outputs, intent):
    from app.application.workbench.formal_method_catalog import (
        instruction_receipt_cost,
    )
    from app.application.workbench.instruction_evidence import instruction_evidence
    from app.domain.workbench.formal_execution import validate_values

    procedure = leaf.draft.definition.procedure
    if procedure.kind != "direct":
        raise ValueError("instruction_procedure_mismatch")
    validate_values(
        procedure.outputs,
        intent["result"]["structuredContent"],
    )
    proof = await instruction_evidence(repo, task, attempt, leaf, outputs, leaf=True)
    _require(proof is not None, "instruction_receipt_missing")
    instruction_receipt_cost(
        proof,
        leaf.draft.source,
        task["pack_digest"],
        attempt["turn_ids"],
        task["budget"]["currency"],
        set(),
    )
    return proof


async def _step_evidence(
    repo,
    owner,
    task,
    snapshot,
    step,
    index,
    attempt,
    previous,
    item,
    root,
    events,
    accepted,
    versions,
):
    refs = attempt.get("refs") or {}
    actual = refs.get("formal_result") or {}
    leaf = dependency_for_step(snapshot, step)
    source = snapshot_for_task(task).execution_source(leaf)
    _require(source is not None, "leaf_source_missing")
    assert source is not None
    _require(
        len(leaf.draft.definition.access_requirements)
        == (0 if source.is_instruction else 1),
        "leaf_access_mismatch",
    )
    row = await repo.formal_method(
        await repo.actor_for_subject(leaf.owner_subject),
        str(leaf.reference.method_id),
        leaf.reference.version,
    )
    _require(
        row is not None
        and row["document"] == leaf.model_dump(mode="json", by_alias=True)
        and row["caller"] == "knowlab",
        "leaf_registration_mismatch",
    )
    identity = {
        "identity_basis": "formal_dependency",
        "task_id": str(task["id"]),
        "snapshot_digest": task["pack_digest"],
        "slot": step.id,
        "step_index": index,
        "reference": leaf.reference.model_dump(mode="json"),
    }
    inputs = bind_step_inputs(step, snapshot.arguments, accepted)
    bindings = [
        version for version in versions if version["step_id"] in step.depends_on
    ]
    _require(
        str(attempt["task_id"]) == str(task["id"])
        and str(attempt["session_id"]) == str(task["session_id"])
        and attempt["step_id"] == step.id
        and attempt["step_version"] == digest(identity)
        and attempt["status"] == AttemptState.COMPLETED
        and (source.is_instruction or attempt.get("turn_ids") == [])
        and not refs.get("usage_pending")
        and not refs.get("pending_usage")
        and (
            source.is_instruction
            or (
                _zero((attempt.get("budget_consumed") or {}).get("amount", "0"))
                and not any(
                    (attempt.get("budget_consumed") or {}).get("tokens", {}).values()
                )
            )
        )
        and refs.get("kind") == "formal_orchestration_step"
        and all(
            refs.get(key) == identity[key]
            for key in ("slot", "step_index", "reference", "snapshot_digest")
        )
        and refs.get("input_bindings") == bindings
        and refs.get("arguments_digest") == digest(inputs)
        and actual.get("outcome") == "succeeded"
        and actual.get("rejection_code") is None
        and attempt.get("ended_at") is not None
        and (index == 0 or previous["ended_at"] <= attempt["created_at"]),
        "step_attempt_or_input_mismatch",
    )
    child = await repo.call_by_key(
        owner,
        str(task["session_id"]),
        "formal-method-step:" + str(task["id"]) + ":" + step.id,
    )
    _require(child is not None, "step_method_call_missing")
    _require(
        str(child["id"]) == refs.get("method_call_id")
        and child["kind"] == "method"
        and child["origin"] == "method"
        and child["status"] == "succeeded"
        and str(child["owner_actor_id"]) == str(owner)
        and str(child["parent_call_id"]) == str(root["id"])
        and str(child["session_id"]) == str(task["session_id"])
        and str(child["task_id"]) == str(task["id"])
        and child["digest"] == digest(identity)
        and all(child["detail"].get(key) == value for key, value in identity.items())
        and child["detail"].get("result") == actual,
        "step_method_call_mismatch",
    )
    call = await repo.capability_call(owner, refs["capability_call_id"])
    intent = call["detail"]
    expected_host = {
        "task_id": str(task["id"]),
        "attempt_id": str(attempt["id"]),
        "snapshot_digest": task["pack_digest"],
        "slot": step.id,
        "method_id": str(leaf.reference.method_id),
        "run_id": str(snapshot.run_id),
        "case_id": snapshot.case_id,
        "repetition": snapshot.repetition,
        "source_digest": source.fingerprint(),
        "reference": leaf.reference.model_dump(mode="json"),
        "parent_method_id": str(snapshot.method_id),
        "method_call_id": str(child["id"]),
        "inputs_digest": digest(inputs),
    }
    registration = step_registration(leaf, inputs, refs, intent)
    _require(
        str(call["id"]) == refs["capability_call_id"]
        and call["kind"] == "tool"
        and call["origin"] == "managed"
        and call["status"] == "succeeded"
        and str(call["owner_actor_id"]) == str(owner)
        and str(call["session_id"]) == str(task["session_id"])
        and str(call["task_id"]) == str(task["id"])
        and str(call["attempt_id"]) == str(attempt["id"])
        and str(call["parent_call_id"]) == str(child["id"])
        and call["call_key"] == "formal-host:" + str(task["id"]) + ":" + step.id
        and intent.get("execution_kind") == host_kind(source, instruction_leaf=True)
        and intent.get("formal_host") == expected_host
        and fingerprint(intent.get("arguments")) == fingerprint(inputs)
        and call["digest"] == host_intent_digest(intent)
        and (
            call.get("connection_id") is None
            if source.kind
            in (
                "skill_script",
                "registered_script",
                "skill_instruction",
                "registered_instruction",
            )
            else str(call.get("connection_id")) == str(source.asset_id)
        )
        and str(call["source_asset_id"]) == str(source.asset_id)
        and intent["revision"] == source.authorization_revision
        and intent["tool"]["name"] == registration.name
        and call["registration_id"] == registration.tool_id
        and call["registration_digest"] == registration.descriptor_digest,
        "step_tool_call_mismatch",
    )
    item["governance"] = await _approval(
        repo, owner, task, attempt, child, call, leaf, inputs, root, events
    )
    outputs = validate_step_outputs(
        step,
        (
            intent["result"]["structuredContent"]
            if source.is_instruction
            else structured_outputs(leaf.draft, intent["result"])
        ),
    )
    bounded_json(outputs)
    _require(
        fingerprint(outputs) == fingerprint(actual["outputs"])
        and actual["outputs_digest"] == digest(outputs),
        "step_result_mismatch",
    )
    artifacts = attempt.get("output_artifacts") or []
    _require(len(artifacts) == 1, "step_artifact_missing")
    version = artifacts[0]
    artifact = await repo.get_artifact(
        task_id=str(task["id"]),
        name="formal-step:" + step.id,
        version=version["version"],
    )
    _require(
        artifact is not None
        and str(artifact["id"]) == version["id"]
        and str(artifact["task_id"]) == str(task["id"])
        and str(artifact["attempt_id"]) == str(attempt["id"])
        and artifact["name"] == version["name"] == "formal-step:" + step.id
        and artifact["version"] == version["version"] == 1
        and artifact["kind"] == "step"
        and artifact["digest"] == version["digest"]
        and fingerprint(artifact["content"]) == fingerprint(outputs),
        "step_artifact_mismatch",
    )
    if source.is_instruction:
        item["governance"]["instruction"] = await instruction_step_proof(
            repo, task, attempt, leaf, outputs, intent
        )
        item["model_turn_ids"] = attempt["turn_ids"]
    accepted[step.id] = outputs
    versions.append({"step_id": step.id, **version})
    item.update(
        reference=leaf.reference.model_dump(mode="json"),
        inputs_digest=digest(inputs),
        input_bindings=bindings,
        artifact=version,
        outputs=outputs,
        outputs_digest=digest(outputs),
    )


async def collect_plan_evidence(repo, request, case, repetition, result) -> dict:
    evidence = {
        "result": result,
        "governance": None,
        "validated": False,
        "errors": [],
        "steps": [],
    }
    stage = "root"
    try:
        owner = await repo.actor_for_subject(request.owner_subject)
        _require(owner is not None and result.get("task_id"), "task_or_owner_missing")
        task = await repo.get_task(result["task_id"], owner_actor_id=owner)
        snapshot = snapshot_for_task(task)
        plan = orchestration_plan(snapshot)
        attempts = await repo.list_attempts(str(task["id"]))
        evidence.update(
            snapshot_digest=task["pack_digest"], snapshot=snapshot.document()
        )
        # Observed partial identities are retained even when no complete chain
        # exists. They are never invented to fill mandatory plan slots.
        for attempt in attempts:
            refs = attempt.get("refs") or {}
            observed_call = None
            if refs.get("capability_call_id"):
                observed = await repo.capability_call(owner, refs["capability_call_id"])
                observed_call = {
                    "call_id": str(observed["id"]),
                    "parent_call_id": str(observed.get("parent_call_id")),
                    "status": observed["status"],
                    "digest": observed["digest"],
                    "adapter_result_digest": digest(observed["detail"].get("result")),
                }
            evidence["steps"].append(
                {
                    "attempt_id": str(attempt["id"]),
                    "slot": refs.get("slot"),
                    "reference": refs.get("reference"),
                    "status": attempt["status"],
                    "outcome": (refs.get("formal_result") or {}).get("outcome"),
                    "method_call_id": refs.get("method_call_id"),
                    "call_id": refs.get("capability_call_id"),
                    "observed_call": observed_call,
                    "input_bindings": refs.get("input_bindings"),
                    "output_artifacts": attempt.get("output_artifacts"),
                    "outputs_digest": (refs.get("formal_result") or {}).get(
                        "outputs_digest"
                    ),
                    "governance": None,
                }
            )
        _require(
            str(task["owner_actor_id"]) == str(owner)
            and str(task["id"]) == result["task_id"]
            and snapshot.run_id == request.run_id
            and snapshot.method_id == request.method_id
            and snapshot.request_digest == request.fingerprint()
            and snapshot.draft == request.draft
            and snapshot.case_id == result["case_id"] == case.case_id
            and snapshot.repetition == result["repetition"] == repetition
            and snapshot.caller == case.caller == result.get("caller", "host") == "host"
            and fingerprint(snapshot.arguments) == fingerprint(case.input.values)
            and {
                (ref.method_id, ref.version, ref.digest) for ref in request.dependencies
            }
            == {
                (
                    leaf.reference.method_id,
                    leaf.reference.version,
                    leaf.reference.digest,
                )
                for leaf in snapshot.dependencies
            },
            "plan_snapshot_identity_mismatch",
        )
        run = await repo.get_test_run(str(request.run_id))
        rows = await repo.list_test_cases(str(request.run_id))
        matches = [
            row for row in rows if row["case_id"] == case_key(case.case_id, repetition)
        ]
        _require(
            run["request"] == request.model_dump(mode="json", by_alias=True)
            and run["digest"] == request.fingerprint().removeprefix("sha256:")
            and run["owner_subject"] == request.owner_subject
            and run["caller"] == "knowlab"
            and len(matches) == 1
            and matches[0].get("result") == result
            and str(matches[0].get("task_id")) == str(task["id"])
            and str(matches[0].get("session_id")) == str(task["session_id"]),
            "persisted_plan_case_mismatch",
        )
        _require(
            task["state"] == TaskState.SUCCEEDED
            and result["outcome"] == "succeeded"
            and task.get("active_attempt_id") is None
            and task.get("active_interaction_id") is None
            and task.get("cancel_requested_at") is None
            and len(attempts) == len(plan)
            and str(attempts[-1]["id"]) == result["attempt_id"]
            and task["current_step"] == len(plan) - 1,
            "plan_execution_incomplete",
        )
        validate_plan_inputs(snapshot.draft.definition, snapshot.arguments)
        root = await repo.call_by_key(
            owner, str(task["session_id"]), "task:" + str(task["id"])
        )
        _require(root is not None, "plan_root_call_missing")
        _require(
            root["kind"] == "method"
            and root["origin"] == "method"
            and root["status"] == "succeeded"
            and root.get("parent_call_id") is None
            and str(root["owner_actor_id"]) == str(owner)
            and str(root["session_id"]) == str(task["session_id"])
            and str(root["task_id"]) == str(task["id"])
            and root["digest"] == digest(["task:" + str(task["id"])])
            and root["detail"].get("identity_basis") == "host_task"
            and root["detail"].get("task_id") == str(task["id"]),
            "plan_root_call_mismatch",
        )
        events = await repo.list_record_events(
            session_id=str(task["session_id"]), types=("method/permissionsConfirmed",)
        )
        model_events = await repo.list_record_events(
            session_id=str(task["session_id"]),
            types=("turn/started", "turn/completed", "thread/tokenUsage/updated"),
        )
        _require(not model_events, "unexpected_plan_model_execution")
        _require(
            Decimal(str(task["budget"].get("used", "0")))
            == await task_script_cost(repo, task["id"])
            + sum(
                Decimal(a["refs"]["instruction_usage"]["amount"])
                for a in attempts
                if "instruction_quote" in a["refs"]
            )
            and _zero(task["budget"].get("reserved", "0")),
            "unexpected_plan_cost",
        )
        accepted, versions = {}, []
        for index, (step, attempt, item) in enumerate(
            zip(plan, attempts, evidence["steps"], strict=True)
        ):
            stage = step.id
            await _step_evidence(
                repo,
                owner,
                task,
                snapshot,
                step,
                index,
                attempt,
                attempts[index - 1] if index else None,
                item,
                root,
                events,
                accepted,
                versions,
            )
        stage = "exports"
        exported = export_outputs(snapshot.draft.definition, accepted)
        exports = attempts[-1]["refs"].get("formal_exports")
        _require(
            exports == {"outputs": exported, "outputs_digest": digest(exported)}
            and result.get("call_id") == attempts[-1]["refs"]["capability_call_id"],
            "plan_export_mismatch",
        )
        assertions = evaluate_assertions(case, exported)
        passed = case.expected.outcome == "succeeded" and all(
            item.passed for item in assertions
        )
        _require(
            result["assertions"]
            == [item.model_dump(mode="json") for item in assertions]
            and result["status"] == ("passed" if passed else "failed")
            and result.get("rejection_code") is None,
            "plan_assertion_or_verdict_mismatch",
        )
        evidence.update(
            validated=True,
            outputs=exported,
            outputs_digest=digest(exported),
            root_call_id=str(root["id"]),
            model_turn_ids=[
                t for s in evidence["steps"] for t in s.get("model_turn_ids", [])
            ],
        )
    except ValueError as exc:
        # All messages generated here are field/stage identifiers, not data.
        code = (
            str(exc)
            if str(exc).replace("_", "").isalnum() and len(str(exc)) < 100
            else "invalid_persisted_evidence"
        )
        evidence["errors"].append(f"{stage}:{code}")
    except (
        WorkbenchError,
        KeyError,
        TypeError,
        IndexError,
        AttributeError,
        InvalidOperation,
    ):
        evidence["errors"].append(f"{stage}:missing_or_invalid_persisted_evidence")
    return evidence
