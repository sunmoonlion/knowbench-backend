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
from app.application.workbench.tool_registry import registered_call
from app.domain.workbench.capabilities import CapabilityError, digest
from app.domain.workbench.errors import Forbidden
from app.domain.workbench.formal_execution import case_key, snapshot_for_task
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
)
from app.domain.workbench.method_authorization import (
    MethodAuthorization,
    PermissionGrant,
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

    @property
    def identity(self):
        # The Attempt is evidence of who dispatched this logical action, not
        # an excuse to duplicate it after a worker restart or a new Attempt.
        return host_intent_digest(self.intent)


class FormalHostCalls:
    def __init__(self, capabilities):
        self.capabilities = capabilities
        self.repo = capabilities.repo

    async def context(self, owner, task_id, attempt_id) -> HostContext:
        """Caller owns a transaction; lock the authoritative Task before its work."""
        task = await self.repo.get_task(task_id, owner_actor_id=owner, for_update=True)
        try:
            snapshot = snapshot_for_task(task)
        except ValueError as exc:
            raise CapabilityError("Formal execution snapshot is invalid") from exc
        attempt = await self.repo.get_attempt(attempt_id, for_update=True)
        refs = attempt.get("refs") or {}
        if (
            str(attempt["task_id"]) != task_id
            or str(task.get("active_attempt_id")) != attempt_id
            or attempt["status"] not in (AttemptState.CREATED, AttemptState.RUNNING)
            or refs.get("kind") != "formal_method"
            or refs.get("snapshot_digest") != task["pack_digest"]
            or refs.get("slot") != "direct"
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
            or not 1 <= snapshot.repetition <= case.repetitions
            or digest(case.input.values) != digest(snapshot.arguments)
        ):
            raise CapabilityError("Formal Task is not its persisted case repetition")
        check_host_inputs(snapshot)
        source = snapshot.draft.source
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
            "requirement": snapshot.draft.definition.access_requirements[0].model_dump(
                mode="json"
            ),
            "operation_digest": policy.policy_digest,
        }
        decision = authorize_operation(
            registration,
            snapshot.arguments,
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
            "slot": "direct",
            "method_id": str(snapshot.method_id),
            "run_id": str(snapshot.run_id),
            "case_id": snapshot.case_id,
            "repetition": snapshot.repetition,
            "source_digest": source.fingerprint(),
        }
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
            "arguments": snapshot.arguments,
            "operation": decision.operation,
            "access": access,
        }
        return HostContext(
            task, attempt, asset, registration, access, decision.operation, intent
        )

    async def request(self, owner, task_id, attempt_id):
        async with self.repo.transaction():
            task = await self.repo.get_task(
                task_id, owner_actor_id=owner, for_update=True
            )
            attempt = await self.repo.get_attempt(attempt_id, for_update=True)
            refs = attempt.get("refs") or {}
            if (
                str(attempt["task_id"]) != task_id
                or str(task.get("active_attempt_id")) != attempt_id
                or refs.get("kind") != "formal_method"
                or refs.get("slot") != "direct"
                or refs.get("snapshot_digest") != task.get("pack_digest")
            ):
                raise CapabilityError(
                    "Formal host Attempt no longer owns the operation"
                )
            await self.repo.recover_calls(owner, str(task["session_id"]))
            existing = await self.repo.call_by_key(
                owner, str(task["session_id"]), host_call_key(task_id)
            )
            if existing:
                # Returning persisted facts does not require renewed authority.
                # It must never create another operation after source revocation,
                # expiry, an uncertain dispatch, or an Attempt replacement.
                try:
                    snapshot_for_task(task)
                except ValueError as exc:
                    raise CapabilityError("Formal execution snapshot changed") from exc
                host = existing["detail"].get("formal_host") or {}
                if (
                    existing["origin"] != "managed"
                    or existing["detail"].get("execution_kind") != "formal_mcp"
                    or str(existing.get("task_id")) != task_id
                    or host.get("task_id") != task_id
                    or host.get("slot") != "direct"
                    or host.get("snapshot_digest") != task["pack_digest"]
                ):
                    raise Forbidden("Host logical action was reused for different work")
                return public_call(existing)
            context = await self.context(owner, task_id, attempt_id)
            session_id = str(context.task["session_id"])
            await self.repo.recover_calls(owner, session_id)
            key = host_call_key(task_id)
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
            if await self.repo.method_authorization(owner, root["id"], lock=True):
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
                    question="请核对本次账号、用途、资源范围和完整参数。仅批准这一次操作，不授予委派或后续操作权限。",
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
                    "parent_call_id": root["id"],
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
                },
            )
            return public_call(call)

    async def decide(self, owner, peek, body):
        async with self.repo.transaction():
            # The lock order agrees with request/cancel: Task before Attempt/call.
            await self.repo.get_task(
                str(peek["task_id"]), owner_actor_id=owner, for_update=True
            )
            call = await self.repo.capability_call(owner, peek["id"], lock=True)
            if call["status"] != "awaiting_approval":
                return public_call(call)
            if body.subject_digest != call["digest"]:
                raise Forbidden("Approval does not match the operation")
            intent = call["detail"]
            host = intent.get("formal_host") or {}
            if (
                intent.get("execution_kind") != "formal_mcp"
                or str(call["task_id"]) != host.get("task_id")
                or str(call["attempt_id"]) != host.get("attempt_id")
                or call["call_key"] != host_call_key(host["task_id"])
            ):
                raise Forbidden("Formal action has no trusted host identity")
            task = await self.repo.get_task(host["task_id"])
            if (
                str(task.get("active_attempt_id")) != host["attempt_id"]
                or task["state"] != TaskState.WAITING
                or str(task.get("active_interaction_id")) != str(call["interaction_id"])
                or task.get("cancel_requested_at")
            ):
                raise CapabilityError(
                    "Formal approval no longer belongs to active work"
                )
            context = None
            if body.decision == "accept":
                context = await self.context(owner, host["task_id"], host["attempt_id"])
                if (
                    context.identity != call["digest"]
                    or digest(context.intent)
                    != digest(
                        {k: v for k, v in intent.items() if k != "approval_token"}
                    )
                    or context.intent["formal_host"] != host
                    or call["source_asset_id"] != context.asset["id"]
                    or call["connection_id"] != context.asset["id"]
                    or call["registration_id"] != context.registration.tool_id
                    or call["registration_digest"]
                    != context.registration.descriptor_digest
                ):
                    raise CapabilityError(
                        "Formal action changed; existing approval cannot authorize it"
                    )
            await Ledger(self.repo).respond_interaction(
                call["interaction_id"],
                token=body.token,
                response={"decision": body.decision},
                owner_actor_id=owner,
                subject_digest=body.subject_digest,
            )
            if context:
                intent = await self.authorize(owner, call, context)
            await Ledger(self.repo).transition(
                host["task_id"],
                TaskState.RUNNING,
                reason={"resume_active_attempt": host["attempt_id"]},
            )
            status = "dispatching" if context else "declined"
            await self.repo.update_call(owner, call["id"], status, intent)
        if context:
            return await self.capabilities._dispatch_call(
                owner, call, context.asset, intent
            )
        return public_call(await self.repo.capability_call(owner, call["id"]))

    async def authorize(self, owner, call, context):
        """An authenticated owner approved this exact action in this transaction."""
        root = await root_call(self.repo, context.task)
        if root["id"] != call["parent_call_id"]:
            raise Forbidden("Formal action parent changed")
        if await self.repo.method_authorization(owner, root["id"], lock=True):
            raise CapabilityError(
                "Formal action authority was already confirmed or revoked"
            )
        interaction = await self.repo.get_interaction(str(call["interaction_id"]))
        expiry = interaction["expires_at"]
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
        host = context.intent["formal_host"]
        grant = PermissionGrant(
            grant_id=digest([call["id"], call["digest"]]),
            confirmation_context=host["snapshot_digest"],
            connection_id=context.asset["id"],
            connection_revision=context.asset["revision"],
            operation_digest=context.access["operation_digest"],
            requirement=exact,
            delegable=False,
            recipient_call_id=root["id"],
            expires_at=expiry,
            method_ids=(host["method_id"],),
        )
        authorization = MethodAuthorization(
            call_id=root["id"],
            revision=1,
            parent_call_id=None,
            parent_revision=None,
            method_id=host["method_id"],
            revoked=False,
            expires_at=expiry,
            grants=(grant,),
        )
        await self.repo.save_method_authorization(
            owner,
            str(context.task["session_id"]),
            authorization.model_dump(mode="json"),
            previous=0,
        )
        await self.repo.append_event(
            session_id=str(context.task["session_id"]),
            task_id=str(context.task["id"]),
            attempt_id=str(context.attempt["id"]),
            kind="task",
            event_type="method/permissionsConfirmed",
            payload={
                "authorization": authorization.model_dump(mode="json"),
                "context": host["snapshot_digest"],
                "interaction_id": str(call["interaction_id"]),
                "action_digest": call["digest"],
            },
        )
        return {
            **call["detail"],
            "authorization": {"call_id": root["id"], "revision": 1},
        }
