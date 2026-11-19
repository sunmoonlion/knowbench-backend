"""Reconcile admission evidence against actual tasks, approvals and adapter results.

This collector grants nothing and runs no operation. Expected business failures
may be valid evidence; uncertain dispatch and unproved authority never qualify.
"""

from app.domain.workbench.capabilities import digest
from app.domain.workbench.errors import WorkbenchError
from app.domain.workbench.formal_execution import (
    case_key,
    direct_procedure,
    snapshot_for_task,
    structured_outputs,
    validate_values,
)
from app.domain.workbench.formal_host import host_call_key
from app.domain.workbench.formal_method_execution_generated import (
    bounded_json,
    evaluate_assertions,
    fingerprint,
)
from app.domain.workbench.method_authorization import MethodAuthorization
from app.domain.workbench.states import (
    ATTEMPT_TERMINAL,
    TASK_TERMINAL,
    AttemptState,
    TaskState,
)
from app.domain.workbench.tool_registry import ToolRegistration


async def collect_case_evidence(repo, request, case, repetition, result) -> dict:
    """Return retained, secret-free facts and whether they prove this exact case.

    ``validated`` concerns provenance/governance, not whether assertions passed.
    An authorization_denied result remains unqualified until the driver records
    a structured denial proof; its text and its failure code are insufficient.
    """
    from app.domain.workbench.formal_method_generated import Orchestration

    if isinstance(request.draft.definition.procedure, Orchestration):
        from app.domain.workbench.formal_execution import recursive_plan

        if result.get("task_id"):
            try:
                owner = await repo.actor_for_subject(request.owner_subject)
                task = await repo.get_task(result["task_id"], owner_actor_id=owner)
                recursive = recursive_plan(snapshot_for_task(task))
            except (WorkbenchError, ValueError, TypeError, KeyError):
                recursive = False
            if recursive:
                from app.application.workbench.formal_recursive_evidence import (
                    collect_recursive_evidence,
                )

                return await collect_recursive_evidence(
                    repo, request, case, repetition, result
                )
        from app.application.workbench.formal_orchestration_evidence import (
            collect_plan_evidence,
        )

        return await collect_plan_evidence(repo, request, case, repetition, result)
    evidence = {"result": result, "governance": None, "validated": False, "errors": []}
    errors = evidence["errors"]

    def require(condition, code):
        if not condition:
            errors.append(code)

    try:
        owner = await repo.actor_for_subject(request.owner_subject)
        require(owner is not None, "owner_missing")
        if not result.get("task_id") or not result.get("attempt_id"):
            errors.append("task_or_attempt_missing")
            return evidence
        task = await repo.get_task(result["task_id"], owner_actor_id=owner)
        attempt = await repo.get_attempt(result["attempt_id"])
        snapshot = snapshot_for_task(task)
        refs, actual = (
            attempt.get("refs") or {},
            (attempt.get("refs") or {}).get("formal_result") or {},
        )
        evidence.update(
            snapshot_digest=task["pack_digest"],
            model_turn_ids=attempt.get("turn_ids"),
            outputs_digest=actual.get("outputs_digest"),
        )
        require(str(task["owner_actor_id"]) == str(owner), "task_owner_mismatch")
        require(
            str(attempt["task_id"]) == str(task["id"]) == result["task_id"],
            "attempt_task_mismatch",
        )
        require(
            str(attempt["id"]) == result["attempt_id"]
            and str(attempt["session_id"]) == str(task["session_id"]),
            "attempt_identity_mismatch",
        )
        require(
            task["state"] in TASK_TERMINAL
            and attempt["status"] in ATTEMPT_TERMINAL
            and task.get("active_attempt_id") is None,
            "execution_not_terminal",
        )
        model = case.caller == "model"
        if not model:
            require(attempt.get("turn_ids") == [], "unexpected_model_turns")
        require(
            refs.get("kind") == "formal_method"
            and refs.get("slot") == "direct"
            and refs.get("snapshot_digest") == task["pack_digest"],
            "attempt_snapshot_mismatch",
        )
        require(
            str(snapshot.run_id) == str(request.run_id)
            and snapshot.case_id == case.case_id
            and snapshot.repetition == repetition
            and snapshot.request_digest == request.fingerprint()
            and snapshot.method_id == request.method_id
            and snapshot.draft == request.draft
            and snapshot.caller == case.caller == result.get("caller", "host")
            and fingerprint(snapshot.arguments) == fingerprint(case.input.values),
            "case_snapshot_mismatch",
        )
        require(
            result["case_id"] == case.case_id and result["repetition"] == repetition,
            "result_case_mismatch",
        )
        run = await repo.get_test_run(str(request.run_id))
        require(
            bool(run)
            and run["request"] == request.model_dump(mode="json", by_alias=True)
            and run["digest"] == request.fingerprint().removeprefix("sha256:")
            and run["owner_subject"] == request.owner_subject
            and run["caller"] == "knowlab",
            "run_request_mismatch",
        )
        rows = await repo.list_test_cases(str(request.run_id))
        matches = [
            r
            for r in rows
            if r["case_id"] == case_key(case.case_id, repetition)
            and str(r.get("task_id")) == result["task_id"]
            and str(r.get("session_id")) == str(task["session_id"])
        ]
        require(
            len(matches) == 1 and matches[0].get("result") == result,
            "persisted_case_mismatch",
        )
        outcome = result["outcome"]
        require(
            (
                task["state"] == TaskState.SUCCEEDED
                and attempt["status"] == AttemptState.COMPLETED
            )
            if outcome == "succeeded"
            else (
                task["state"] == TaskState.FAILED
                and attempt["status"] == AttemptState.FAILED
            ),
            "terminal_state_result_mismatch",
        )
        require(
            actual.get("outcome") == outcome
            and actual.get("rejection_code") == result.get("rejection_code"),
            "driver_result_mismatch",
        )
        require(
            refs.get("capability_call_id") == result.get("call_id"),
            "attempt_call_mismatch",
        )
        outputs = actual.get("outputs") or {}
        bounded_json(outputs)
        require(
            actual.get("outputs_digest") == digest(outputs),
            "driver_output_digest_mismatch",
        )
        assertions = (
            evaluate_assertions(case, outputs) if outcome == "succeeded" else ()
        )
        require(
            result["assertions"] == [a.model_dump(mode="json") for a in assertions],
            "assertion_result_mismatch",
        )
        passed = outcome == case.expected.outcome and (
            all(a.passed for a in assertions)
            if outcome == "succeeded"
            else actual.get("rejection_code") == case.expected.rejection_code
        )
        expected_status = (
            "passed" if passed else "cancelled" if outcome == "cancelled" else "failed"
        )
        require(result["status"] == expected_status, "case_verdict_mismatch")
        require(
            outcome not in ("pending", "unknown", "cancelled"),
            "execution_outcome_unqualified",
        )
        call_id = result.get("call_id")
        if call_id is None:
            if model:
                errors.append("model_call_evidence_missing")
                return evidence
            existing = await repo.call_by_key(
                owner, str(task["session_id"]), host_call_key(str(task["id"]))
            )
            require(existing is None, "unreported_dispatch_exists")
            require(
                outcome == "rejected"
                and attempt.get("failure_code") == actual.get("rejection_code"),
                "unbound_outcome",
            )
            if result.get("rejection_code") == "input_invalid":
                invalid = False
                try:
                    validate_values(
                        direct_procedure(request.draft).inputs, snapshot.arguments
                    )
                except ValueError:
                    invalid = True
                require(invalid, "input_rejection_not_reproducible")
            elif result.get("rejection_code") == "authorization_denied":
                evidence["denial_verified"] = not errors
                errors.append("authorization_denial_has_no_structured_proof")
            else:
                errors.append("rejection_requires_a_call")
            evidence["validated"] = not errors
            return evidence

        call = await repo.capability_call(owner, call_id)
        context_key = "formal_probe" if model else "formal_host"
        intent, host = call["detail"], call["detail"].get(context_key) or {}
        original_intent = {
            k: v
            for k, v in intent.items()
            if k not in {"approval_token", "authorization", "result"}
        }
        if not model:
            original_intent["formal_host"] = {
                k: v for k, v in host.items() if k != "attempt_id"
            }
        require(call["digest"] == digest(original_intent), "call_digest_mismatch")
        source = request.draft.source
        require(source is not None, "source_missing")
        if source is None:
            return evidence
        require(
            str(call["id"]) == call_id
            and str(call.get("task_id")) == result["task_id"]
            and str(call.get("attempt_id")) == result["attempt_id"]
            and str(call["session_id"]) == str(task["session_id"])
            and call.get("kind") == "tool"
            and call.get("origin") == "managed"
            and (model or call.get("call_key") == host_call_key(result["task_id"]))
            and intent.get("execution_kind")
            == ("formal_probe" if model else "formal_mcp"),
            "call_identity_mismatch",
        )
        require(
            host
            == {
                "task_id": result["task_id"],
                "attempt_id": result["attempt_id"],
                "snapshot_digest": task["pack_digest"],
                "slot": "direct",
                "method_id": str(request.method_id),
                "run_id": str(request.run_id),
                "case_id": case.case_id,
                "repetition": repetition,
                "source_digest": source.fingerprint(),
            },
            "host_identity_mismatch",
        )
        require(
            fingerprint(intent.get("arguments")) == fingerprint(snapshot.arguments),
            "call_input_mismatch",
        )
        require(
            str(call.get("source_asset_id")) == str(source.asset_id)
            and str(call.get("connection_id")) == str(source.asset_id)
            and intent.get("revision") == source.asset_revision,
            "call_source_mismatch",
        )
        registration = ToolRegistration.discover("mcp", source.source, intent["tool"])
        require(
            intent["tool"]["name"] == source.entry
            and call.get("registration_id") == registration.tool_id
            and call.get("registration_digest") == registration.descriptor_digest
            and source.digest == "sha256:" + registration.descriptor_digest,
            "registration_mismatch",
        )
        parent = await repo.call_by_key(
            owner, str(task["session_id"]), "task:" + result["task_id"]
        )
        if parent is None:
            errors.append("parent_call_missing")
            return evidence
        require(
            str(parent["id"]) == str(call["parent_call_id"])
            and parent.get("kind") == "method"
            and parent.get("origin") == "method"
            and str(parent.get("task_id")) == result["task_id"]
            and str(parent.get("session_id")) == str(task["session_id"])
            and parent.get("call_key") == "task:" + result["task_id"],
            "parent_identity_mismatch",
        )
        if model:
            from app.application.workbench.formal_model_evidence import (
                collect_model_provenance,
            )

            provenance = await collect_model_provenance(
                repo, request, case, task, attempt, call, parent, result
            )
            evidence["model_provenance"] = provenance
            errors.extend(provenance["errors"])
            require(provenance["validated"], "model_provenance_unqualified")
        interaction = await repo.get_interaction(str(call["interaction_id"]))
        require(
            str(interaction.get("task_id")) == result["task_id"]
            and str(interaction.get("attempt_id")) == result["attempt_id"]
            and str(interaction.get("session_id")) == str(task["session_id"])
            and interaction["kind"] == "tool_approval"
            and interaction["subject_digest"] == call["digest"],
            "interaction_identity_mismatch",
        )
        subject = (interaction.get("prompt") or {}).get("subject") or {}
        require(
            subject.get("capability_call_id") == call_id
            and fingerprint(subject.get("arguments"))
            == fingerprint(snapshot.arguments),
            "approval_preview_mismatch",
        )
        consumed_at = interaction.get("consumed_at")
        approved = (
            interaction["status"] == "consumed"
            and (interaction.get("response") or {}).get("decision") == "accept"
            and str(interaction.get("responded_by")) == str(owner)
            and consumed_at is not None
            and consumed_at < interaction["expires_at"]
        )
        governance = dict(
            call_id=call_id,
            status=call["status"],
            input_digest=digest(snapshot.arguments),
            registration_id=call["registration_id"],
            registration_digest=call["registration_digest"],
            operation=intent.get("operation"),
            formal_host=host,
            authorization_digest=None,
            authorization_revision=None,
            direct_grants=False,
            interaction_id=str(interaction["id"]),
            interaction_status=interaction["status"],
            approved=approved,
            approval={
                "decision": (interaction.get("response") or {}).get("decision"),
                "subject_digest": interaction["subject_digest"],
                "responded_by": str(interaction.get("responded_by"))
                if interaction.get("responded_by")
                else None,
                "consumed_at": consumed_at.isoformat() if consumed_at else None,
                "expires_at": interaction["expires_at"].isoformat(),
            },
        )
        if model:
            governance["formal_probe"] = governance.pop("formal_host")
        evidence["governance"] = governance
        if outcome == "rejected":
            declined = (
                call["status"] == "declined"
                and interaction["status"] == "consumed"
                and (interaction.get("response") or {}).get("decision") == "decline"
                and str(interaction.get("responded_by")) == str(owner)
            )
            expired = (
                call["status"] == "expired"
                and interaction["status"] in {"expired", "cancelled"}
                and attempt.get("ended_at") is not None
                and interaction["expires_at"] <= attempt["ended_at"]
                and (interaction.get("response") or {}).get("decision") != "accept"
            )
            require(
                result.get("rejection_code") == "approval_denied"
                and (declined or expired),
                "approval_rejection_not_proven",
            )
            require("result" not in intent, "rejected_call_has_dispatch_result")
        else:
            require(approved, "exact_approval_not_proven")
            raw_auth = await repo.method_authorization(owner, str(parent["id"]))
            auth = MethodAuthorization.model_validate(raw_auth)
            require(
                str(auth.call_id) == str(parent["id"])
                and auth.parent_call_id is None
                and auth.method_id == str(request.method_id)
                and not auth.revoked
                and len(auth.grants) == 1
                and consumed_at is not None
                and consumed_at < auth.expires_at,
                "authorization_identity_mismatch",
            )
            grant = auth.grants[0]
            access = intent["access"]
            declared = request.draft.definition.access_requirements[0].model_dump(
                mode="json"
            )
            require(access["requirement"] == declared, "declared_access_mismatch")
            operation = intent["operation"]
            exact = {
                **declared,
                "operations": [operation["operation"]],
                "resources": operation["resources"],
                "data_transfer": {
                    **declared["data_transfer"],
                    "argument_fields": sorted(snapshot.arguments),
                },
            }
            direct = (
                not grant.delegable
                and grant.recipient_call_id == auth.call_id
                and str(grant.connection_id) == str(source.asset_id)
                and grant.connection_revision == source.asset_revision
                and grant.operation_digest == access["operation_digest"]
                and grant.confirmation_context == task["pack_digest"]
                and grant.method_ids == (str(request.method_id),)
                and grant.requirement.model_dump(mode="json") == exact
                and grant.expires_at == auth.expires_at == interaction["expires_at"]
                and intent.get("authorization")
                == {"call_id": str(parent["id"]), "revision": auth.revision}
            )
            require(direct, "direct_authority_not_proven")
            governance.update(
                authorization_digest=digest(raw_auth),
                authorization_revision=auth.revision,
                direct_grants=direct,
                authorization=auth.model_dump(mode="json"),
            )
            adapter_result = intent.get("result")
            bounded_json(adapter_result)
            require(isinstance(adapter_result, dict), "adapter_result_missing")
            if outcome == "succeeded":
                require(
                    call["status"] == "succeeded", "success_without_adapter_success"
                )
                returned = structured_outputs(request.draft, adapter_result)
                bounded_json(returned)
                require(
                    fingerprint(returned) == fingerprint(outputs),
                    "adapter_output_mismatch",
                )
            elif outcome == "failed":
                if call["status"] == "succeeded":
                    invalid = False
                    try:
                        structured_outputs(request.draft, adapter_result)
                    except ValueError:
                        invalid = True
                    require(invalid, "unexplained_failed_output")
                else:
                    require(
                        call["status"] == "failed", "failure_without_adapter_failure"
                    )
            else:
                errors.append("unsupported_result_outcome")
        evidence["validated"] = not errors
    except (
        WorkbenchError,
        ValueError,
        KeyError,
        TypeError,
        IndexError,
        AttributeError,
    ):
        errors.append("missing_or_invalid_persisted_evidence")
    return evidence
