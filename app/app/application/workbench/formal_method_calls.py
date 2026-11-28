"""Owner-requested formal tool calls in conversations, without expert Tasks.

The root is the actual frozen method invocation; its one child is the adapter
dispatch. Both refer to the same exact approval and never imply permission to
compose another method or to retry an uncertain external action.
"""

import secrets
from datetime import UTC, datetime, timedelta
from typing import Literal
from uuid import NAMESPACE_URL, uuid4, uuid5

from app.application.workbench.call_policy import authorize_operation
from app.application.workbench.formal_method_catalog import FormalMethodCatalog
from app.application.workbench.ledger import Ledger
from app.application.workbench.tool_invocation import verify_tool_invocation
from app.domain.workbench.capabilities import CapabilityError, digest
from app.domain.workbench.errors import Forbidden, IdempotencyConflict, NotFound
from app.domain.workbench.formal_execution import (
    direct_procedure,
    structured_outputs,
    validate_values,
)
from app.domain.workbench.formal_method_admission_generated import FrozenFormalMethod
from app.domain.workbench.formal_method_execution_generated import bounded_json
from app.domain.workbench.method_authorization import (
    MethodAuthorization,
    PermissionGrant,
)
from app.domain.workbench.method_capabilities import AccessRequirement
from app.domain.workbench.models import InteractionPrompt
from app.domain.workbench.skill_execution import public_call
from app.domain.workbench.tool_operations import McpOperation


def _root_key(request_id):
    return "formal-session:" + str(request_id)


def _intent_digest(detail):
    return digest(
        {
            k: v
            for k, v in detail.items()
            if k not in ("approval_token", "authorization", "result")
        }
    )


class FormalMethodCalls:
    def __init__(self, capabilities):
        self.capabilities = capabilities
        self.repo = capabilities.repo
        self._catalog = FormalMethodCatalog(
            self.repo, capabilities.formal_sources, capabilities.operations
        )

    async def catalog(
        self, owner, session_id, *, caller: Literal["host", "model"] = "host"
    ):
        async with self.repo.transaction():
            await self._session(owner, session_id, active=True, lock=False)
            references = await self.repo.formal_method_references(owner)
        eligible = await self.capabilities.eligible_formal_refs(owner, references)
        return {
            "items": await self._catalog.list_owned(
                owner, caller=caller, eligible=eligible
            )
        }

    async def _session(self, owner, session_id, *, active=False, lock=True):
        session = await self.repo.get_session(
            session_id, owner_actor_id=owner, for_update=lock
        )
        if active and (
            session.get("kind") not in ("chat", "work")
            or session.get("archived_at") is not None
            or session.get("wheel") != "user"
            or session.get("active_task_id") is not None
        ):
            raise CapabilityError(
                "Formal calls require an active user-controlled chat or work conversation"
            )
        return session

    async def _intent(self, owner, request, root_id, *, eligible, model=None):
        if request.reference not in eligible:
            raise Forbidden("This method version is not listed for the owner")
        caller = "model" if model else "host"
        resolved = await self._catalog.resolve_owned(
            owner, request.reference, caller=caller
        )
        frozen, asset, registration, policy = (
            resolved.frozen,
            resolved.asset,
            resolved.registration,
            resolved.policy,
        )
        procedure = direct_procedure(frozen.draft, caller=caller)
        try:
            validate_values(procedure.inputs, request.values)
        except ValueError as exc:
            raise CapabilityError(str(exc)) from exc
        if (
            len(frozen.draft.definition.access_requirements) != 1
            or not isinstance(policy, McpOperation)
            or policy.formal_execution_cost is None
        ):
            raise CapabilityError(
                "Formal calls require one reviewed permission and zero incremental provider cost"
            )
        access = {
            "requirement": frozen.draft.definition.access_requirements[0].model_dump(
                mode="json"
            ),
            "operation_digest": policy.policy_digest,
        }
        decision = authorize_operation(
            registration,
            request.values,
            operations=self.capabilities.operations,
            method=True,
            access=access,
            operation_digest=policy.policy_digest,
        )
        if not decision.requires_approval:
            raise CapabilityError("Formal external operations require exact approval")
        tool = registration.descriptor()
        intent = {
            "execution_kind": "formal_method",
            "formal_method": {
                "request_id": str(request.request_id),
                "method_call_id": root_id,
                "reference": frozen.reference.model_dump(mode="json"),
                "frozen_digest": digest(frozen.model_dump(mode="json", by_alias=True)),
            },
            "source_asset_id": asset["id"],
            "connection_id": asset["id"],
            "revision": asset["revision"],
            "connection_name": asset["name"],
            "account_label": asset["detail"].get("account_label", ""),
            "purpose": access["requirement"]["data_transfer"]["purpose"],
            "tool": tool,
            "arguments": request.values,
            "operation": decision.operation,
            "access": access,
        }
        if model:
            intent.update(
                execution_kind="formal_method_model",
                invocation=model["invocation"],
                runtime=model["runtime"],
                selection_revision=model["selection_revision"],
                reference=request.reference.model_dump(mode="json"),
                method_title=frozen.draft.definition.capability_declaration.title,
            )
            intent["formal_method"].update(
                caller="model",
                model_tool_key=model["tool_key"],
                scope_id=model["scope_id"],
            )
        return resolved, intent

    async def request(self, owner, session_id, body):
        return await self._request(owner, session_id, body)

    async def request_model(self, session_id, token, key, arguments, *, meta):
        from app.domain.workbench.formal_method_calls import FormalMethodCallRequest

        selected = await self.capabilities.runtime(session_id, token)
        owner = selected["owner_actor_id"]
        spec = selected["runtime"].get("tools", {}).get(key) or {}
        if spec.get("execution_kind") != "formal_method_model":
            raise Forbidden("Formal method is not selected for this model turn")
        invocation = await self.capabilities.verify_invocation(
            session_id, token, key, arguments, meta
        )
        # Bind to host-observed identity, not a model-chosen request UUID.
        body = FormalMethodCallRequest(
            request_id=uuid5(
                NAMESPACE_URL, "sunmoon/formal-session-model/" + invocation.key
            ),
            reference=spec["formal_method"]["reference"],
            values=arguments,
        )
        model = {
            "invocation": invocation.snapshot(),
            "runtime": selected["runtime"]["token_hash"],
            "scope_id": selected["runtime"]["scope_id"],
            "selection_revision": selected["revision"],
            "tool_key": key,
        }
        result = await self._request(owner, session_id, body, model=model)
        return {"call_id": str(result["call"]["id"]), "status": result["status"]}

    async def _check_model(self, owner, session_id, body, model, eligible):
        from app.application.workbench.formal_session_tools import FormalSessionTools

        session = await self._session(owner, session_id, active=True)
        selected = await self.repo.capability_selection(owner, session_id, lock=True)
        runtime = selected["runtime"]
        invocation = model["invocation"]
        if (
            runtime.get("status") != "applied"
            or runtime.get("aborted")
            or runtime.get("method")
            or runtime.get("formal_probe")
            or runtime.get("token_hash") != model["runtime"]
            or runtime.get("scope_id") != model["scope_id"]
            or selected["revision"] != model["selection_revision"]
            or runtime.get("selection_revision") != selected["revision"]
            or runtime.get("turn_id") != invocation["turn_id"]
            or session.get("thread_id") != invocation["thread_id"]
            or invocation["session_id"] != session_id
            or invocation["server"] != "sunmoon_capabilities"
            or datetime.fromisoformat(
                runtime.get("expires_at", "1970-01-01T00:00:00+00:00")
            )
            <= datetime.now(UTC)
            or str(body.request_id)
            != str(
                uuid5(
                    NAMESPACE_URL, "sunmoon/formal-session-model/" + invocation["key"]
                )
            )
            or not await self.repo.is_accepted_user_turn(
                session_id=session_id, turn_id=invocation["turn_id"]
            )
        ):
            raise Forbidden(
                "The model method call no longer belongs to this active user turn"
            )
        current = await FormalSessionTools(self.capabilities).resolve(
            owner, body.reference, eligible=eligible
        )
        if runtime.get("tools", {}).get(model["tool_key"]) != current:
            raise Forbidden("The selected method or its current authority changed")
        actual = await verify_tool_invocation(
            self.repo,
            owner=owner,
            sandbox=str(session["sandbox_id"]),
            server="sunmoon_capabilities",
            name=model["tool_key"],
            arguments=body.values,
            meta={
                "threadId": invocation["thread_id"],
                "callId": invocation["call_id"],
                "x-codex-turn-metadata": {
                    "thread_id": invocation["thread_id"],
                    "turn_id": invocation["turn_id"],
                },
            },
            session_id=session_id,
            observe_seconds=0,
        )
        if actual.snapshot() != invocation:
            raise Forbidden("The retained method invocation changed")

    async def _request(self, owner, session_id, body, *, model=None):
        document = body.model_dump(mode="json", by_alias=True)
        existing = await self.repo.call_by_key(
            owner, session_id, _root_key(body.request_id)
        )
        eligible = (
            await self.capabilities.eligible_formal_refs(owner, [document["reference"]])
            if model or not existing
            else set()
        )
        async with self.repo.transaction():
            await self._session(owner, session_id)
            if model:
                await self._check_model(owner, session_id, body, model, eligible)
            await self.repo.recover_calls(owner, session_id)
            root = await self.repo.call_by_key(
                owner, session_id, _root_key(body.request_id)
            )
            if root:
                if (
                    root["digest"] != digest(document)
                    or root["detail"].get("request") != document
                    or (model is not None and root["detail"].get("model") != model)
                ):
                    raise IdempotencyConflict(
                        "This request identity already names different method inputs"
                    )
                return await self._project(owner, session_id, root)
            await self._session(owner, session_id, active=True)
            root_id, child_id = str(uuid4()), str(uuid4())
            resolved, intent = await self._intent(
                owner, body, root_id, model=model, eligible=eligible
            )
            frozen = resolved.frozen
            identity = _intent_digest(intent)
            token = secrets.token_urlsafe(32)
            prompt = InteractionPrompt(
                title="确认调用正式工具方法",
                question="请核对方法版本、账号、用途、资源范围和完整输入。仅允许本次操作，不授予组合、派生或继续委派权限。",
                options=[
                    {"value": "accept", "label": "仅允许这次"},
                    {"value": "decline", "label": "拒绝"},
                ],
                subject={
                    "capability_call_id": child_id,
                    "method_call_id": root_id,
                    "reference": frozen.reference.model_dump(mode="json"),
                    "title": frozen.draft.definition.capability_declaration.title,
                    "connection": intent["connection_name"],
                    "account": intent["account_label"],
                    "purpose": intent["purpose"],
                    "tool": intent["tool"]["name"],
                    "arguments": body.values,
                    "operation": {
                        k: intent["operation"][k]
                        for k in (
                            "capability_ref",
                            "version",
                            "operation",
                            "effect",
                            "resources",
                        )
                    },
                },
            )
            interaction_id = await self.repo.insert_interaction(
                session_id=session_id,
                task_id=None,
                attempt_id=None,
                kind="tool_approval",
                prompt=prompt.model_dump(),
                subject_digest=identity,
                token=token,
                target_state_version=None,
                expires_at=datetime.now(UTC) + timedelta(minutes=15),
            )
            root = await self.repo.add_call(
                {
                    "id": root_id,
                    "owner_actor_id": owner,
                    "session_id": session_id,
                    "kind": "method",
                    "origin": "method",
                    "call_key": _root_key(body.request_id),
                    "interaction_id": interaction_id,
                    "digest": digest(document),
                    "status": "awaiting_approval",
                    "detail": {
                        "request": document,
                        "frozen": frozen.model_dump(mode="json", by_alias=True),
                        "child_call_id": child_id,
                        "intent_digest": identity,
                        **({"model": model} if model else {}),
                    },
                }
            )
            if root["id"] != root_id:
                raise IdempotencyConflict("Concurrent formal request changed")
            child = await self.repo.add_call(
                {
                    "id": child_id,
                    "owner_actor_id": owner,
                    "session_id": session_id,
                    "kind": "tool",
                    "origin": "managed",
                    "call_key": root_id + ":direct",
                    "parent_call_id": root_id,
                    "registration_id": resolved.registration.tool_id,
                    "registration_digest": resolved.registration.descriptor_digest,
                    "source_asset_id": intent["source_asset_id"],
                    "connection_id": intent["connection_id"],
                    "interaction_id": interaction_id,
                    "digest": identity,
                    "status": "awaiting_approval",
                    "detail": {**intent, "approval_token": token},
                }
            )
            if child["id"] != child_id:
                raise IdempotencyConflict("Concurrent formal dispatch changed")
            await self.repo.append_event(
                session_id=session_id,
                kind="approval",
                event_type="interaction/opened",
                payload={
                    "interaction_id": interaction_id,
                    "kind": "tool_approval",
                    "token": token,
                    "subject_digest": identity,
                    "subject": prompt.subject,
                    "prompt": prompt.model_dump(),
                },
            )
            return await self._project(owner, session_id, root)

    def _check_pair(self, owner, session_id, root, child):
        data = root["detail"]
        frozen = FrozenFormalMethod.model_validate(data["frozen"])
        request = data["request"]
        host = child["detail"].get("formal_method") or {}
        model = data.get("model")
        if (
            root["kind"] != "method"
            or root["origin"] != "method"
            or root.get("parent_call_id") is not None
            or root.get("task_id") is not None
            or root.get("attempt_id") is not None
            or root["owner_actor_id"] != owner
            or root["session_id"] != session_id
            or root["call_key"] != _root_key(request["request_id"])
            or root["digest"] != digest(request)
            or frozen.reference.model_dump(mode="json") != request["reference"]
            or child["kind"] != "tool"
            or child["origin"] != "managed"
            or child.get("parent_call_id") != root["id"]
            or child["id"] != data["child_call_id"]
            or child.get("task_id") is not None
            or child.get("attempt_id") is not None
            or child["owner_actor_id"] != owner
            or child["session_id"] != session_id
            or child["call_key"] != root["id"] + ":direct"
            or child["interaction_id"] != root["interaction_id"]
            or child["detail"].get("execution_kind")
            != ("formal_method_model" if model else "formal_method")
            or host
            != {
                "request_id": request["request_id"],
                "method_call_id": root["id"],
                "reference": request["reference"],
                "frozen_digest": digest(data["frozen"]),
                **(
                    {
                        "caller": "model",
                        "model_tool_key": model["tool_key"],
                        "scope_id": model["scope_id"],
                    }
                    if model
                    else {}
                ),
            }
            or (
                model is not None
                and any(
                    child["detail"].get(k) != model[k]
                    for k in ("invocation", "runtime", "selection_revision")
                )
            )
            or (
                model is not None
                and (
                    child["detail"].get("reference") != request["reference"]
                    or child["detail"].get("method_title")
                    != frozen.draft.definition.capability_declaration.title
                )
            )
            or child["detail"].get("arguments") != request["values"]
            or child["digest"] != data["intent_digest"]
            or _intent_digest(child["detail"]) != child["digest"]
        ):
            raise Forbidden("Formal call evidence belongs to different work")
        return frozen

    async def _project(self, owner, session_id, root):
        # Session lock serializes these roots; capability_call deliberately only
        # exposes managed children, never a method row through the legacy API.
        child = await self.repo.capability_call(
            owner, root["detail"]["child_call_id"], lock=True
        )
        frozen = self._check_pair(owner, session_id, root, child)
        state, outputs, detail = child["status"], None, ""
        if state == "succeeded":
            try:
                outputs = structured_outputs(
                    frozen.draft, child["detail"].get("result") or {}
                )
                bounded_json(outputs)
            except (ValueError, TypeError, RecursionError):
                state = "failed"
                outputs = None
                detail = "The external operation returned, but its output did not match the frozen method contract. Any external effects are not rolled back."
        elif state == "unknown":
            detail = "The external outcome is unknown. This request will not be sent again automatically."
        elif state in ("declined", "expired", "cancelled"):
            detail = "The action was not dispatched: " + state
        elif state == "failed":
            detail = "The external operation reported failure. Any external effects are not rolled back."
        projection = {"status": state, "outputs": outputs, "detail": detail}
        if root["status"] != state or root["detail"].get("projection") != projection:
            await self.repo.update_call(
                owner, root["id"], state, {**root["detail"], "projection": projection}
            )
        return {
            "request_id": root["detail"]["request"]["request_id"],
            "method_call_id": root["id"],
            "reference": frozen.reference.model_dump(mode="json"),
            "title": frozen.draft.definition.capability_declaration.title,
            "status": state,
            "call": public_call(
                {**child, "detail": {**child["detail"], "result": None}}
            ),
            "outputs": outputs,
            "detail": detail,
        }

    async def status(self, owner, session_id, request_id):
        async with self.repo.transaction():
            await self._session(owner, session_id)
            await self.repo.recover_calls(owner, session_id)
            root = await self.repo.call_by_key(owner, session_id, _root_key(request_id))
            if root is None:
                raise NotFound("Formal call not found")
            return await self._project(owner, session_id, root)

    async def list_calls(self, owner, session_id):
        async with self.repo.transaction():
            await self._session(owner, session_id)
            await self.repo.recover_calls(owner, session_id)
            roots = await self.repo.list_formal_session_calls(
                owner, session_id, limit=50
            )
            return [await self._project(owner, session_id, root) for root in roots]

    async def model_result(self, session_id, token, child_id):
        async with self.repo.transaction():
            selected = await self.capabilities.runtime(session_id, token)
            owner = selected["owner_actor_id"]
            session = await self._session(owner, session_id, active=True)
            await self.repo.capability_selection(owner, session_id, lock=True)
            selected = await self.capabilities.runtime(session_id, token)
            await self.repo.recover_calls(owner, session_id)
            child = await self.repo.capability_call(owner, child_id)
            if (
                child["session_id"] != session_id
                or child["detail"].get("execution_kind") != "formal_method_model"
            ):
                raise Forbidden(
                    "This is not a model method result for the conversation"
                )
            root = await self.repo.call_by_key(
                owner,
                session_id,
                _root_key(child["detail"]["formal_method"]["request_id"]),
            )
            if root is None:
                raise Forbidden("Model method has no retained invocation")
            self._check_pair(owner, session_id, root, child)
            model = root["detail"]["model"]
            if (
                model["runtime"] != selected["runtime"].get("token_hash")
                or model["scope_id"] != selected["runtime"].get("scope_id")
                or model["selection_revision"] != selected["revision"]
                or model["invocation"]["turn_id"] != selected["runtime"].get("turn_id")
                or model["invocation"]["thread_id"] != session.get("thread_id")
            ):
                raise Forbidden("Model result belongs to another execution scope")
            value = await self._project(owner, session_id, root)
            return {
                "call_id": child_id,
                "status": value["status"],
                "result": {
                    k: value[k]
                    for k in (
                        "method_call_id",
                        "reference",
                        "title",
                        "outputs",
                        "detail",
                    )
                },
            }

    async def decide(self, owner, child_call_id, body):
        if getattr(body, "remember_read", False):
            raise CapabilityError("This formal action requires an exact approval")
        peek = await self.repo.capability_call(owner, child_call_id)
        session_id = peek["session_id"]
        ref = (peek["detail"].get("formal_method") or {}).get("reference")
        eligible = (
            await self.capabilities.eligible_formal_refs(owner, [ref])
            if ref
            and peek["status"] == "awaiting_approval"
            and body.decision == "accept"
            else set()
        )
        async with self.repo.transaction():
            await self._session(owner, session_id)
            await self.repo.recover_calls(owner, session_id)
            if peek["detail"].get("execution_kind") not in (
                "formal_method",
                "formal_method_model",
            ) or not peek.get("parent_call_id"):
                raise Forbidden("This is not a formal conversation action")
            request_id = peek["detail"].get("formal_method", {}).get("request_id")
            root = await self.repo.call_by_key(owner, session_id, _root_key(request_id))
            if root is None or root["id"] != peek["parent_call_id"]:
                raise Forbidden("Formal action has no matching method invocation")
            child = await self.repo.capability_call(owner, child_call_id, lock=True)
            frozen = self._check_pair(owner, session_id, root, child)
            if child["status"] != "awaiting_approval":
                return await self._project(owner, session_id, root)
            if body.subject_digest != child["digest"]:
                raise Forbidden("Approval does not match this formal operation")
            resolved = None
            intent = child["detail"]
            if body.decision == "accept":
                await self._session(owner, session_id, active=True)
                from app.domain.workbench.formal_method_calls import (
                    FormalMethodCallRequest,
                )

                request = FormalMethodCallRequest.model_validate(
                    root["detail"]["request"]
                )
                model = root["detail"].get("model")
                if model:
                    await self._check_model(owner, session_id, request, model, eligible)
                resolved, current = await self._intent(
                    owner, request, root["id"], model=model, eligible=eligible
                )
                if (
                    resolved.frozen != frozen
                    or _intent_digest(current) != child["digest"]
                    or child["source_asset_id"] != resolved.asset["id"]
                    or child["connection_id"] != resolved.asset["id"]
                    or child["registration_id"] != resolved.registration.tool_id
                    or child["registration_digest"]
                    != resolved.registration.descriptor_digest
                ):
                    raise CapabilityError(
                        "Formal method or current authority changed; this approval cannot dispatch it"
                    )
            await Ledger(self.repo).respond_interaction(
                child["interaction_id"],
                token=body.token,
                response={"decision": body.decision},
                owner_actor_id=owner,
                subject_digest=body.subject_digest,
            )
            if resolved:
                intent = await self._authorize(owner, root, child, frozen)
            await self.repo.update_call(
                owner, child_call_id, "dispatching" if resolved else "declined", intent
            )
            await self._project(owner, session_id, root)
        if resolved:
            await self.capabilities._dispatch_call(owner, child, resolved.asset, intent)
        return await self.status(
            owner, session_id, root["detail"]["request"]["request_id"]
        )

    async def _authorize(self, owner, root, child, frozen):
        if await self.repo.method_authorization(owner, root["id"], lock=True):
            raise CapabilityError(
                "This exact method invocation already has an authorization history"
            )
        interaction = await self.repo.get_interaction(child["interaction_id"])
        intent, expiry = child["detail"], interaction["expires_at"]
        operation = intent["operation"]
        requirement = intent["access"]["requirement"]
        exact = AccessRequirement.model_validate(
            {
                **requirement,
                "operations": [operation["operation"]],
                "resources": operation["resources"],
                "data_transfer": {
                    **requirement["data_transfer"],
                    "argument_fields": sorted(intent["arguments"]),
                },
            }
        )
        grant = PermissionGrant(
            grant_id=digest([root["id"], child["digest"]]),
            confirmation_context=child["digest"],
            connection_id=child["connection_id"],
            connection_revision=intent["revision"],
            operation_digest=intent["access"]["operation_digest"],
            requirement=exact,
            delegable=False,
            recipient_call_id=root["id"],
            expires_at=expiry,
            method_ids=(str(frozen.reference.method_id),),
        )
        authorization = MethodAuthorization(
            call_id=root["id"],
            revision=1,
            parent_call_id=None,
            parent_revision=None,
            method_id=str(frozen.reference.method_id),
            revoked=False,
            expires_at=expiry,
            grants=(grant,),
        )
        await self.repo.save_method_authorization(
            owner, root["session_id"], authorization.model_dump(mode="json"), previous=0
        )
        await self.repo.append_event(
            session_id=root["session_id"],
            kind="method",
            event_type="method/permissionsConfirmed",
            payload={
                "authorization": authorization.model_dump(mode="json"),
                "context": child["digest"],
                "call_id": child["id"],
                "interaction_id": child["interaction_id"],
            },
        )
        return {**intent, "authorization": {"call_id": root["id"], "revision": 1}}
