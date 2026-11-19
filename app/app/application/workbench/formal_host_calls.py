"""Direct formal actions through the shared approval, call and adapter boundary.

Task/Attempt and the persisted test request establish host provenance. No model
turn, MCP observation or conversation capability token is manufactured here.
"""

import uuid
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Any

from app.application.workbench.call_policy import authorize_operation
from app.application.workbench.ledger import Ledger
from app.application.workbench.method_calls import root_call
from app.application.workbench.task_output_reader import TOOL as TASK_OUTPUT_TOOL
from app.application.workbench.tool_registry import registered_call
from app.domain.workbench.capabilities import CapabilityError, digest
from app.domain.workbench.errors import Forbidden
from app.domain.workbench.formal_execution import (
    case_key,
    direct_procedure,
    execution_slot,
    expanded_plan,
    recursive_plan,
    snapshot_for_task,
    validate_values,
)
from app.domain.workbench.formal_host import (
    check_host_inputs,
    host_call_key,
    host_intent_digest,
)
from app.domain.workbench.formal_method_admission_generated import (
    FormalMethodValidationRequest,
)
from app.domain.workbench.formal_method_execution_generated import (
    FormalMethodExecutionRequest,
    bounded_json,
)
from app.domain.workbench.formal_model import model_call_key, model_intent_digest
from app.domain.workbench.method_authorization import (
    MethodAuthorization,
    PermissionGrant,
    delegate,
)
from app.domain.workbench.method_capabilities import AccessRequirement
from app.domain.workbench.models import InteractionPrompt
from app.domain.workbench.skill_execution import public_call
from app.domain.workbench.states import AttemptState, TaskState, WaitingReason


@dataclass
class HostContext:
    task: dict
    attempt: dict
    asset: dict
    registration: Any
    access: dict
    operation: dict
    intent: dict
    method_call: dict | None = None
    scopes: list[MethodAuthorization] | None = None
    knowbase_read: dict | None = None
    artifact_read: dict | None = None

    @property
    def identity(self):
        # The Attempt is evidence of who dispatched this logical action, not
        # an excuse to duplicate it after a worker restart or a new Attempt.
        return (
            model_intent_digest(self.intent)
            if self.intent.get("execution_kind") == "formal_probe"
            else host_intent_digest(self.intent)
        )


class FormalHostCalls:
    def __init__(self, capabilities):
        self.capabilities = capabilities
        self.repo = capabilities.repo

    async def context(
        self, owner, task_id, attempt_id, *, caller="host"
    ) -> HostContext:
        """Caller owns a transaction; lock the authoritative Task before its work."""
        task = await self.repo.get_task(task_id, owner_actor_id=owner, for_update=True)
        try:
            snapshot = snapshot_for_task(task)
        except ValueError as exc:
            raise CapabilityError("Formal execution snapshot is invalid") from exc
        if snapshot.caller != caller:
            raise CapabilityError(
                "Formal execution caller differs from its admitted test"
            )
        attempt = await self.repo.get_attempt(attempt_id, for_update=True)
        refs = attempt.get("refs") or {}
        step = frozen = child = None
        scopes = None
        draft, arguments, slot = snapshot.draft, snapshot.arguments, "direct"
        if snapshot.dependencies:
            from app.application.workbench.formal_orchestration import resolve_plan
            from app.application.workbench.formal_orchestration_execution import (
                step_call_key,
                step_context,
                step_identity,
            )

            if caller != "host":
                raise CapabilityError("Formal orchestration cannot use a model probe")
            _, step, frozen, arguments = await step_context(self.repo, task, attempt)
            live = await resolve_plan(
                self.capabilities, owner, snapshot.draft, snapshot.method_id
            )
            if live != snapshot.dependencies:
                raise CapabilityError("Formal dependency admission changed")
            draft, slot = (
                frozen.draft,
                execution_slot(snapshot, int(task["current_step"])),
            )
            if recursive_plan(snapshot):
                from app.application.workbench.formal_recursive_runtime import (
                    execution_scopes,
                    resolve_leaf,
                )

                child, _, _, _ = await resolve_leaf(self.repo, task, snapshot)
                scopes = await execution_scopes(self.repo, task, snapshot)
            else:
                child = await self.repo.call_by_key(
                    owner, str(task["session_id"]), step_call_key(task_id, slot)
                )
                root = await self.repo.call_by_key(
                    owner, str(task["session_id"]), "task:" + task_id
                )
                identity = step_identity(task, step, int(task["current_step"]))
                if (
                    child is None
                    or root is None
                    or child["id"] != refs.get("method_call_id")
                    or child["parent_call_id"] != root["id"]
                    or child["digest"] != digest(identity)
                    or any(child["detail"].get(k) != v for k, v in identity.items())
                ):
                    raise CapabilityError(
                        "Formal child method differs from its host plan"
                    )
        if (
            str(attempt["task_id"]) != task_id
            or str(task.get("active_attempt_id")) != attempt_id
            or attempt["status"] not in (AttemptState.CREATED, AttemptState.RUNNING)
            or refs.get("kind")
            != ("formal_orchestration_step" if step else "formal_method")
            or refs.get("snapshot_digest") != task["pack_digest"]
            or refs.get("slot") != slot
            or refs.get("permission_revocation")
            or task["state"] not in (TaskState.RUNNING, TaskState.WAITING)
            or task.get("cancel_requested_at")
        ):
            raise CapabilityError("Formal host Attempt no longer owns the operation")
        session_id = str(task["session_id"])
        session = await self.repo.get_session(session_id, owner_actor_id=owner)
        if (
            str(session.get("active_task_id")) != task_id
            or session["wheel"] != "advisor"
        ):
            raise CapabilityError("Formal execution no longer owns this session")
        run = await self.repo.test_run_for_task(task_id)
        if (
            run is None
            or str(run["run_id"]) != str(snapshot.run_id)
            or run.get("caller") != "knowlab"
            or run.get("status")
            in ("passed", "failed", "error", "timeout", "cancelled")
            or run.get("cancel_requested_at")
        ):
            raise CapabilityError("Formal Task has no active test execution")
        try:
            request = FormalMethodExecutionRequest.model_validate(run["request"])
        except ValueError as exc:
            raise CapabilityError("Formal execution request is invalid") from exc
        if (
            request.fingerprint() != snapshot.request_digest
            or run["digest"] != snapshot.request_digest.removeprefix("sha256:")
            or str(request.run_id) != str(snapshot.run_id)
            or request.method_id != snapshot.method_id
            or request.draft != snapshot.draft
            or request.owner_subject != run["owner_subject"]
            or await self.repo.actor_for_subject(request.owner_subject) != owner
        ):
            raise CapabilityError("Formal snapshot differs from its accepted request")
        validation = await self.repo.formal_validation(owner, str(snapshot.run_id))
        if (
            validation is None
            or validation["caller"] != run["caller"]
            or FormalMethodValidationRequest.model_validate(validation["request"])
            != request.admission_request()
            or validation["request_digest"]
            != digest(
                request.admission_request().model_dump(mode="json", by_alias=True)
            )
            or validation["result"].get("status") != "pending"
        ):
            raise CapabilityError("Formal execution lacks its exact pending admission")
        cases = await self.repo.list_test_cases(str(snapshot.run_id))
        persisted = [
            c
            for c in cases
            if c["case_id"] == case_key(snapshot.case_id, snapshot.repetition)
            and str(c.get("task_id")) == task_id
            and str(c.get("session_id")) == session_id
            and not c.get("result")
        ]
        case = next(
            (c for c in request.execution.suite.cases if c.case_id == snapshot.case_id),
            None,
        )
        if (
            len(persisted) != 1
            or case is None
            or case.caller != snapshot.caller
            or case.input.model_prompt != snapshot.model_prompt
            or not 1 <= snapshot.repetition <= case.repetitions
            or digest(case.input.values) != digest(snapshot.arguments)
        ):
            raise CapabilityError("Formal Task is not its persisted case repetition")
        if step is not None:
            validate_values(direct_procedure(draft).inputs, arguments)
            if len(draft.definition.access_requirements) != 1:
                raise CapabilityError(
                    "Formal leaves require one exact operation permission"
                )
        elif caller == "host":
            check_host_inputs(snapshot)
        else:
            bounded_json(snapshot.arguments)
            validate_values(
                direct_procedure(snapshot.draft, caller="model").inputs,
                snapshot.arguments,
            )
            if (
                len(snapshot.draft.definition.access_requirements) != 1
                or request.execution.model_probe != snapshot.model_probe
            ):
                raise CapabilityError(
                    "Model probe limits or permissions differ from its test"
                )
        source = draft.source
        if source is None or not await self.capabilities.formal_sources._source(
            owner, request.owner_subject, source
        ):
            raise CapabilityError("Source execution rights have not been verified")
        asset = await self.repo.capability_asset(owner, str(source.asset_id), lock=True)
        tool = next(
            (t for t in asset["detail"].get("tools", []) if t["name"] == source.entry),
            None,
        )
        if asset["kind"] != "connection" or tool is None:
            raise CapabilityError("Formal source has no current MCP operation")
        registration = registered_call(asset, {"tool": tool})
        policy = self.capabilities.operations.require(registration)
        if policy.formal_execution_cost is None:
            raise CapabilityError(
                "Formal execution requires reviewed zero incremental provider cost"
            )
        access = {
            "requirement": draft.definition.access_requirements[0].model_dump(
                mode="json"
            ),
            "operation_digest": policy.policy_digest,
        }
        decision = authorize_operation(
            registration,
            arguments,
            operations=self.capabilities.operations,
            method=True,
            access=access,
            operation_digest=policy.policy_digest,
        )
        if not decision.requires_approval:
            raise CapabilityError("Formal external operations require exact approval")
        host = {
            "task_id": task_id,
            "attempt_id": attempt_id,
            "snapshot_digest": task["pack_digest"],
            "slot": slot,
            "method_id": str(frozen.reference.method_id)
            if frozen
            else str(snapshot.method_id),
            "run_id": str(snapshot.run_id),
            "case_id": snapshot.case_id,
            "repetition": snapshot.repetition,
            "source_digest": source.fingerprint(),
        }
        if frozen is not None and child is not None:
            host.update(
                parent_method_id=str(snapshot.method_id),
                reference=frozen.reference.model_dump(mode="json"),
                method_call_id=child["id"],
                inputs_digest=digest(arguments),
            )
        if scopes is not None:
            from app.domain.workbench.formal_recursive import TREE_ADAPTER

            leaf = expanded_plan(snapshot).leaf_nodes[int(task["current_step"])]
            host.update(
                adapter=TREE_ADAPTER,
                path=list(leaf.path),
                parent_method_id=scopes[-1].method_id,
                parent_method_call_id=refs["parent_method_call_id"],
                root_method_call_id=refs["root_method_call_id"],
            )
        knowbase_read = None
        reader = self.capabilities.knowbase_reader
        if reader and asset["detail"]["url"] == reader.url:
            from app.application.workbench.knowbase_method_read import (
                TOOL,
                read_binding,
            )

            material = arguments.get("material_id")
            if (
                source.entry != TOOL
                or policy.effect != "read"
                or not any(
                    r.resource_role == "material"
                    and r.argument_path == ("material_id",)
                    for r in policy.resources
                )
                or not any(
                    r["resource_role"] == "material" and r["values"] == [material]
                    for r in decision.operation["resources"]
                )
            ):
                raise CapabilityError(
                    "KnowBase requires an exact reviewed material read"
                )
            knowbase_read = await read_binding(
                self.repo,
                task,
                snapshot,
                arguments,
                access["requirement"]["data_transfer"]["purpose"],
                child,
                scopes,
            )
        intent = {
            "execution_kind": "formal_mcp",
            "formal_host": host,
            "source_asset_id": asset["id"],
            "connection_id": asset["id"],
            "revision": asset["revision"],
            "connection_name": asset["name"],
            "account_label": asset["detail"].get("account_label", ""),
            "purpose": access["requirement"]["data_transfer"]["purpose"],
            "tool": tool,
            "arguments": arguments,
            "operation": decision.operation,
            "access": access,
        }
        artifact_read = None
        if (
            self.capabilities.task_outputs_url
            and asset["detail"]["url"] == self.capabilities.task_outputs_url
            and source.entry == TASK_OUTPUT_TOOL
        ):
            from app.application.workbench.task_output_reader import (
                read_binding as output_binding,
            )

            if policy.effect != "read" or not any(
                r.resource_role == "task" and r.argument_path == ("task_id",)
                for r in policy.resources
            ):
                raise CapabilityError(
                    "Task output reads require a reviewed task resource"
                )
            artifact_read = await output_binding(
                self.repo,
                task,
                snapshot,
                arguments,
                access["requirement"]["data_transfer"],
                child=child,
                attempt=attempt,
            )
        elif child is not None:
            from app.application.workbench.artifact_read_authority import planned_read

            artifact_read = await planned_read(
                self.repo,
                task,
                snapshot,
                child,
                attempt,
                arguments,
                access["requirement"]["data_transfer"],
            )
        if artifact_read:
            intent["artifact_read_digest"] = digest(artifact_read)
        return HostContext(
            task,
            attempt,
            asset,
            registration,
            access,
            decision.operation,
            intent,
            child,
            scopes,
            knowbase_read,
            artifact_read,
        )

    async def request(self, owner, task_id, attempt_id, *, model=None):
        async with self.repo.transaction():
            task = await self.repo.get_task(
                task_id, owner_actor_id=owner, for_update=True
            )
            attempt = await self.repo.get_attempt(attempt_id, for_update=True)
            snapshot = snapshot_for_task(task)
            caller = "model" if model is not None else "host"
            if snapshot.caller != caller:
                raise CapabilityError("This test requires its own caller entry")
            slot = (
                execution_slot(snapshot, int(task["current_step"]))
                if snapshot.dependencies
                else "direct"
            )
            key = (
                model_call_key(task_id)
                if model is not None
                else host_call_key(task_id, slot)
            )
            refs = attempt.get("refs") or {}
            if (
                str(attempt["task_id"]) != task_id
                or str(task.get("active_attempt_id")) != attempt_id
                or refs.get("kind")
                != (
                    "formal_orchestration_step"
                    if snapshot.dependencies
                    else "formal_method"
                )
                or refs.get("slot") != slot
                or refs.get("snapshot_digest") != task.get("pack_digest")
            ):
                raise CapabilityError(
                    "Formal host Attempt no longer owns the operation"
                )
            await self.repo.recover_calls(owner, str(task["session_id"]))
            existing = await self.repo.call_by_key(owner, str(task["session_id"]), key)
            if existing:
                # Returning persisted facts does not require renewed authority.
                # It must never create another operation after source revocation,
                # expiry, an uncertain dispatch, or an Attempt replacement.
                try:
                    snapshot_for_task(task)
                except ValueError as exc:
                    raise CapabilityError("Formal execution snapshot changed") from exc
                host = (
                    existing["detail"].get(
                        "formal_probe" if model is not None else "formal_host"
                    )
                    or {}
                )
                if (
                    existing["origin"] != "managed"
                    or existing["detail"].get("execution_kind")
                    != ("formal_probe" if model is not None else "formal_mcp")
                    or (
                        model is not None
                        and existing["detail"].get("invocation")
                        != model["invocation"].snapshot()
                    )
                    or str(existing.get("task_id")) != task_id
                    or host.get("task_id") != task_id
                    or host.get("slot") != slot
                    or host.get("snapshot_digest") != task["pack_digest"]
                ):
                    raise Forbidden("Host logical action was reused for different work")
                if snapshot.dependencies:
                    from app.domain.workbench.formal_host import validate_host_call

                    validate_host_call(task, attempt, existing)
                return public_call(existing)
            context = await self.context(owner, task_id, attempt_id, caller=caller)
            if model is not None:
                from app.application.workbench.formal_model_calls import (
                    bind_model_intent,
                )

                bind_model_intent(context, model)
            session_id = str(context.task["session_id"])
            await self.repo.recover_calls(owner, session_id)
            existing = await self.repo.call_by_key(owner, session_id, key)
            if existing:
                if (
                    existing["digest"] != context.identity
                    or existing["origin"] != "managed"
                ):
                    raise Forbidden("Host logical action was reused for different work")
                return public_call(existing)
            if context.task["state"] != TaskState.RUNNING or context.task.get(
                "active_interaction_id"
            ):
                raise CapabilityError(
                    "Resolve the existing interaction before requesting an operation"
                )
            root = await root_call(self.repo, context.task)
            prior_authority = await self.repo.method_authorization(
                owner, root["id"], lock=True
            )
            if prior_authority and (
                context.method_call is None or prior_authority["revoked"]
            ):
                raise CapabilityError(
                    "Formal action already has an authorization history"
                )
            call_id = str(uuid.uuid4())
            operation = context.operation
            opened = await Ledger(self.repo).open_interaction(
                task_id,
                attempt_id=attempt_id,
                kind="tool_approval",
                waiting_reason=WaitingReason.APPROVAL,
                subject_digest=context.identity,
                ttl=timedelta(minutes=15),
                prompt=InteractionPrompt(
                    title="确认正式方法的外部操作",
                    question=(
                        "请核对产物版本、输出字段、用途和接收方法链。批准即允许沿所示调用链仅传递这些字段，并执行这一次操作。"
                        if context.artifact_read
                        and "return_to" not in context.artifact_read
                        else "请核对来源任务、产物版本、字段与接收入口。批准即允许本次方法读取所示字段并把结果返回给该入口。"
                        if context.artifact_read
                        else "请核对本次账号、用途、资源范围和完整参数。仅批准这一次操作，不授予委派或后续操作权限。"
                    ),
                    options=[
                        {"value": "accept", "label": "仅允许这次"},
                        {"value": "decline", "label": "拒绝"},
                    ],
                    subject={
                        "capability_call_id": call_id,
                        "connection": context.asset["name"],
                        "account": context.intent["account_label"],
                        "purpose": context.intent["purpose"],
                        "tool": context.intent["tool"]["name"],
                        **(
                            {"artifact_read": context.artifact_read}
                            if context.artifact_read
                            else {}
                        ),
                        "arguments": context.intent["arguments"],
                        "operation": {
                            k: operation[k]
                            for k in (
                                "capability_ref",
                                "version",
                                "operation",
                                "effect",
                                "resources",
                            )
                        },
                    },
                ),
            )
            call = await self.repo.add_call(
                {
                    "id": call_id,
                    "owner_actor_id": owner,
                    "session_id": session_id,
                    "task_id": task_id,
                    "attempt_id": attempt_id,
                    "parent_call_id": context.method_call["id"]
                    if context.method_call
                    else root["id"],
                    "kind": "tool",
                    "origin": "managed",
                    "call_key": key,
                    "registration_id": context.registration.tool_id,
                    "registration_digest": context.registration.descriptor_digest,
                    "source_asset_id": context.asset["id"],
                    "connection_id": context.asset["id"],
                    "interaction_id": opened["interaction_id"],
                    "digest": context.identity,
                    "status": "awaiting_approval",
                    "detail": {**context.intent, "approval_token": opened["token"]},
                }
            )
            if call["id"] != call_id:
                raise CapabilityError("Formal action was concurrently created")
            await self.repo.update_attempt(
                attempt_id,
                refs={
                    **(context.attempt.get("refs") or {}),
                    "capability_call_id": call_id,
                    **(
                        {
                            "formal_model_probe": {
                                **(context.attempt.get("refs") or {}).get(
                                    "formal_model_probe", {}
                                ),
                                "invocation_key": model["invocation"].key,
                                "call_id": call_id,
                            }
                        }
                        if model is not None
                        else {}
                    ),
                },
            )
            return public_call(call)

    @staticmethod
    def pending_invocation(task, call):
        intent = call["detail"]
        is_model = intent.get("execution_kind") == "formal_probe"
        host = intent.get("formal_probe" if is_model else "formal_host") or {}
        if (
            intent.get("execution_kind") not in ("formal_mcp", "formal_probe")
            or str(task["id"]) != host.get("task_id")
            or str(call["task_id"]) != host.get("task_id")
            or str(call["attempt_id"]) != host.get("attempt_id")
            or call["call_key"]
            != (
                model_call_key(host["task_id"])
                if is_model
                else host_call_key(host["task_id"], host.get("slot", "direct"))
            )
        ):
            raise Forbidden("Formal action has no trusted host identity")
        if (
            str(task.get("active_attempt_id")) != host["attempt_id"]
            or task["state"] != TaskState.WAITING
            or str(task.get("active_interaction_id")) != str(call["interaction_id"])
            or task.get("cancel_requested_at")
        ):
            raise CapabilityError("Formal approval no longer belongs to active work")
        return host

    async def approval_context(self, owner, call, host):
        is_model = call["detail"]["execution_kind"] == "formal_probe"
        context = await self.context(
            owner,
            host["task_id"],
            host["attempt_id"],
            caller="model" if is_model else "host",
        )
        if context.artifact_read and "artifact_read_digest" not in call["detail"]:
            # Pending actions created before resource consent require explicit
            # approval on the resource endpoint. Preserve their action identity.
            interaction = await self.repo.get_interaction(str(call["interaction_id"]))
            if "artifact_read" not in interaction["prompt"].get("subject", {}):
                context.intent.pop("artifact_read_digest", None)
        if is_model:
            from app.application.workbench.formal_model_calls import (
                FormalModelCalls,
                bind_model_intent,
            )

            model = await FormalModelCalls(self.capabilities).existing_model(
                owner, call
            )
            bind_model_intent(context, model)
        if (
            context.identity != call["digest"]
            or digest(context.intent)
            != digest(
                {k: v for k, v in call["detail"].items() if k != "approval_token"}
            )
            or context.intent["formal_probe" if is_model else "formal_host"] != host
            or call["source_asset_id"] != context.asset["id"]
            or call["connection_id"] != context.asset["id"]
            or call["registration_id"] != context.registration.tool_id
            or call["registration_digest"] != context.registration.descriptor_digest
        ):
            raise CapabilityError(
                "Formal action changed; existing approval cannot authorize it"
            )
        return context

    async def decide(self, owner, peek, body):
        async with self.repo.transaction():
            # The lock order agrees with request/cancel: Task before Attempt/call.
            task = await self.repo.get_task(
                str(peek["task_id"]), owner_actor_id=owner, for_update=True
            )
            call = await self.repo.capability_call(owner, peek["id"], lock=True)
            if call["status"] != "awaiting_approval":
                return public_call(call)
            if body.subject_digest != call["digest"]:
                raise Forbidden("Approval does not match the operation")
            host = self.pending_invocation(task, call)
            intent = call["detail"]
            context = None
            if body.decision == "accept":
                context = await self.approval_context(owner, call, host)
            await Ledger(self.repo).respond_interaction(
                call["interaction_id"],
                token=body.token,
                response={"decision": body.decision},
                owner_actor_id=owner,
                subject_digest=body.subject_digest,
            )
            if context:
                from app.application.workbench.artifact_read_authority import (
                    authorize_dispatch,
                )

                resource = await authorize_dispatch(
                    self.repo, task, call, context.artifact_read, context.scopes
                )
                intent = await self.authorize(owner, call, context, resource)
            await Ledger(self.repo).transition(
                host["task_id"],
                TaskState.RUNNING,
                reason={"resume_active_attempt": host["attempt_id"]},
            )
            status = "dispatching" if context else "declined"
            await self.repo.update_call(owner, call["id"], status, intent)
        if context:
            return await self.capabilities._dispatch_call(
                owner, call, context.asset, intent, context.knowbase_read
            )
        return public_call(await self.repo.capability_call(owner, call["id"]))

    async def authorize(self, owner, call, context, resource=None):
        """An authenticated owner approved this exact action in this transaction."""
        root = await root_call(self.repo, context.task)
        recipient = context.method_call["id"] if context.method_call else root["id"]
        if recipient != call["parent_call_id"]:
            raise Forbidden("Formal action parent changed")
        raw = await self.repo.method_authorization(owner, root["id"], lock=True)
        current = MethodAuthorization.model_validate(raw) if raw else None
        if current and (context.method_call is None or current.revoked):
            raise CapabilityError(
                "Formal action authority was already confirmed or revoked"
            )
        interaction = await self.repo.get_interaction(str(call["interaction_id"]))
        expiry = (
            min(interaction["expires_at"], context.scopes[0].expires_at)
            if context.scopes
            else interaction["expires_at"]
        )
        if expiry <= datetime.now(UTC):
            raise CapabilityError("Formal action approval expired")
        declared = context.access["requirement"]
        exact = AccessRequirement.model_validate(
            {
                **declared,
                "operations": [context.operation["operation"]],
                "resources": context.operation["resources"],
                "data_transfer": {
                    **declared["data_transfer"],
                    "argument_fields": sorted(context.intent["arguments"]),
                },
            }
        )
        host = context.intent[
            "formal_probe"
            if context.intent.get("execution_kind") == "formal_probe"
            else "formal_host"
        ]
        grant = PermissionGrant(
            grant_id=digest([call["id"], call["digest"]]),
            confirmation_context=host["snapshot_digest"],
            connection_id=context.asset["id"],
            connection_revision=context.asset["revision"],
            operation_digest=context.access["operation_digest"],
            requirement=exact,
            delegable=False,
            recipient_call_id=recipient,
            expires_at=expiry,
            method_ids=(host["method_id"],),
        )
        authorization = MethodAuthorization(
            call_id=root["id"],
            revision=current.revision + 1 if current else 1,
            parent_call_id=None,
            parent_revision=None,
            method_id=current.method_id
            if context.scopes and current
            else host.get("parent_method_id", host["method_id"]),
            revoked=False,
            expires_at=current.expires_at
            if context.scopes and current
            else max(current.expires_at, expiry)
            if current
            else expiry,
            grants=(*(current.grants if current else ()), grant),
        )
        await self.repo.save_method_authorization(
            owner,
            str(context.task["session_id"]),
            authorization.model_dump(mode="json"),
            previous=current.revision if current else 0,
        )
        parent_authorization = None
        if context.method_call is not None:
            from uuid import UUID

            if await self.repo.method_authorization(owner, recipient, lock=True):
                raise CapabilityError(
                    "Formal child already has an authorization history"
                )
            parent_authorization = authorization
            authorization = delegate(
                authorization,
                call_id=UUID(recipient),
                method_id=host["method_id"],
                requirements=(grant,),
            ).model_copy(update={"expires_at": expiry})
            await self.repo.save_method_authorization(
                owner,
                str(context.task["session_id"]),
                authorization.model_dump(mode="json"),
                previous=0,
            )
        scope_evidence = {}
        if context.scopes is not None:
            from app.application.workbench.formal_recursive_runtime import (
                execution_scopes,
            )

            snapshot = snapshot_for_task(context.task)
            scopes = await execution_scopes(self.repo, context.task, snapshot)
            scope_evidence = {
                "scope_authorizations": [s.model_dump(mode="json") for s in scopes],
                "call_path": host["path"],
                "authority_source": "owner_exact_leaf",
            }
        await self.repo.append_event(
            session_id=str(context.task["session_id"]),
            task_id=str(context.task["id"]),
            attempt_id=str(context.attempt["id"]),
            kind="task",
            event_type="method/permissionsConfirmed",
            payload={
                "authorization": authorization.model_dump(mode="json"),
                **scope_evidence,
                **({"artifact_read_digest": digest(resource)} if resource else {}),
                "context": host["snapshot_digest"],
                "interaction_id": str(call["interaction_id"]),
                "action_digest": call["digest"],
                **(
                    {
                        "parent_authorization": parent_authorization.model_dump(
                            mode="json"
                        )
                    }
                    if parent_authorization
                    else {}
                ),
            },
        )
        return {
            **call["detail"],
            "authorization": {
                "call_id": str(authorization.call_id),
                "revision": authorization.revision,
            },
        }
