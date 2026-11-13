"""Shared user capability management and execution. No credential leaves this boundary."""

from __future__ import annotations

import json
import secrets
import uuid
from datetime import UTC, datetime, timedelta
from typing import Any, Protocol

from jsonschema import Draft202012Validator
from jsonschema.exceptions import SchemaError
from referencing import Registry
from referencing.exceptions import Unresolvable

from app.application.workbench.execution_plan import display_for_task
from app.application.workbench.ledger import Ledger
from app.domain.workbench.capabilities import (
    CapabilityError,
    digest,
    disabled_mcp_config,
    public_asset,
    tool_key,
)
from app.domain.workbench.errors import Forbidden
from app.domain.workbench.method_plan import PLAN_STEP
from app.domain.workbench.models import InteractionPrompt
from app.domain.workbench.skill_package import (
    SkillRead,
    resource_result,
    skill_files,
    unpack_skill,
)


def schema_validator(schema):
    # Never allow a JSON Schema resolver to fetch arbitrary network/file resources.
    pending, count = [(schema, 0)], 0
    while pending:
        value, depth = pending.pop()
        count += 1
        if count > 5000 or depth > 32:
            raise CapabilityError("Tool schema exceeds the complexity limit")
        if isinstance(value, dict):
            for key, child in value.items():
                if key == "$id":
                    raise CapabilityError(
                        "Tool schemas cannot change their resolution base"
                    )
                if key in ("$ref", "$dynamicRef") and (
                    not isinstance(child, str) or not child.startswith("#")
                ):
                    raise CapabilityError(
                        "Only document-local schema references are supported"
                    )
                pending.append((child, depth + 1))
        elif isinstance(value, list):
            pending.extend((child, depth + 1) for child in value)
    try:
        Draft202012Validator.check_schema(schema)
    except (SchemaError, RecursionError) as exc:
        raise CapabilityError("Invalid tool input schema") from exc
    return Draft202012Validator(schema, registry=Registry())


class McpPort(Protocol):
    allowed_urls: tuple[str, ...]

    async def discover(self, url: str, token: str | None) -> list[dict]: ...
    async def call(
        self, url: str, token: str | None, name: str, arguments: dict
    ) -> dict: ...


class Capabilities:
    def __init__(
        self, repo: Any, remote: McpPort, cipher: Any = None, *, runtime_url: str = ""
    ):
        self.repo, self.remote, self.cipher, self.runtime_url = (
            repo,
            remote,
            cipher,
            runtime_url,
        )

    def secret(self, asset):
        if not asset.get("secret"):
            return None
        if self.cipher is None:
            raise CapabilityError("Credential storage is unavailable")
        return self.cipher.decrypt(asset["secret"].encode()).decode()

    async def assets(self, owner):
        return [public_asset(a) for a in await self.repo.capability_assets(owner)]

    async def create_connection(self, owner, body):
        if body.token and self.cipher is None:
            raise CapabilityError("Credential storage is unavailable")
        tools = await self.remote.discover(body.url, body.token)
        for tool in tools:
            schema_validator(tool["inputSchema"])
        asset = {
            "id": str(uuid.uuid4()),
            "owner_actor_id": owner,
            "kind": "connection",
            "name": body.name.strip(),
            "status": "active",
            "secret": self.cipher.encrypt(body.token.encode()).decode()
            if body.token
            else None,
            "request_id": body.request_id,
            "request_digest": digest(body.model_dump()),
            "detail": {
                "url": body.url,
                "account_label": body.account_label,
                "auth": "bearer" if body.token else "none",
                "tools": tools,
                "checked_at": datetime.now(UTC).isoformat(),
            },
        }
        return public_asset(await self.repo.capability_insert(asset))

    async def create_skill(self, owner, body, *, package=False):
        available = {
            tool_key(a["id"], t["name"])
            for a in await self.repo.capability_assets(owner)
            if a["kind"] == "connection" and a["status"] == "active"
            for t in a["detail"].get("tools", [])
        }
        if not set(body.dependencies) <= available:
            raise CapabilityError("Skill dependencies must refer to your active tools")
        material = (
            unpack_skill(body.archive_base64)
            if package
            else {
                "content": body.content,
                "digest": digest(body.content),
                "format": "instruction-markdown",
            }
        )
        asset = {
            "id": str(uuid.uuid4()),
            "owner_actor_id": owner,
            "kind": "skill",
            "name": body.name,
            "status": "active",
            "secret": None,
            "request_id": body.request_id,
            "request_digest": digest(body.model_dump()),
            "detail": {
                "description": body.description,
                "version": body.version,
                "dependencies": body.dependencies,
                **material,
            },
        }
        return public_asset(await self.repo.capability_insert(asset))

    async def renew(self, owner, asset_id, body):
        asset = await self.repo.capability_asset(owner, asset_id)
        if asset["kind"] != "connection":
            raise CapabilityError("Only connections have credentials")
        if body.token and self.cipher is None:
            raise CapabilityError("Credential storage is unavailable")
        tools = await self.remote.discover(asset["detail"]["url"], body.token)
        for tool in tools:
            schema_validator(tool["inputSchema"])
        detail = {
            **asset["detail"],
            "tools": tools,
            "auth": "bearer" if body.token else "none",
            "checked_at": datetime.now(UTC).isoformat(),
        }
        detail.pop("revocation", None)
        async with self.repo.transaction():
            updated = await self.repo.capability_update(
                owner,
                asset_id,
                body.revision,
                status="active",
                default_selected=asset["default_selected"],
                detail=detail,
                secret=self.cipher.encrypt(body.token.encode()).decode()
                if body.token
                else None,
            )
        return public_asset(updated)

    async def action(self, owner, asset_id, body):
        if body.action in ("default_on", "default_off"):
            async with self.repo.transaction():
                asset = await self.repo.capability_asset(owner, asset_id, lock=True)
                enabled = body.action == "default_on"
                if enabled and asset["status"] != "active":
                    raise CapabilityError(
                        "Only active capabilities can be selected by default"
                    )
                return public_asset(
                    await self.repo.capability_default(
                        owner, asset_id, body.revision, enabled
                    )
                )
        asset = await self.repo.capability_asset(owner, asset_id)
        detail = dict(asset["detail"])
        status, default, secret = (
            asset["status"],
            asset["default_selected"],
            asset["secret"],
        )
        if body.action == "refresh":
            if asset["kind"] != "connection" or status != "active":
                raise CapabilityError("Only active connections can be checked")
            detail["tools"] = await self.remote.discover(
                detail["url"], self.secret(asset)
            )
            for tool in detail["tools"]:
                schema_validator(tool["inputSchema"])
            detail["checked_at"] = datetime.now(UTC).isoformat()
        elif body.action == "disconnect":
            status, default, secret = "disconnected", False, None
            detail["revocation"] = (
                "local_access_removed; revoke bearer credential at provider if required"
            )
        elif body.action in ("enable", "disable"):
            if status == "disconnected":
                raise CapabilityError(
                    "Disconnected assets must be reconnected as a new connection"
                )
            status = "active" if body.action == "enable" else "disabled"
        else:
            default = body.action == "default_on"
            if default and status != "active":
                raise CapabilityError(
                    "Only active capabilities can be selected by default"
                )
        async with self.repo.transaction():
            updated = await self.repo.capability_update(
                owner,
                asset_id,
                body.revision,
                status=status,
                default_selected=default,
                detail=detail,
                secret=secret,
            )
        return public_asset(updated)

    async def selection(self, owner, session_id):
        async with self.repo.transaction():
            selected = await self.repo.capability_selection(
                owner, session_id, lock=True
            )
            if not selected["detail"]:
                # Pre-existing conversations did not consent to defaults added later.
                selected = await self.repo.capability_save_selection(
                    owner,
                    session_id,
                    selected["revision"],
                    {"tools": [], "skills": [], "purpose": "", "bindings": {}},
                )
        return await self.view(owner, session_id, selected)

    async def view(self, owner, session_id, selected):
        session = await self.repo.get_session(session_id, owner_actor_id=owner)
        assets = await self.repo.capability_assets(owner)
        detail = selected["detail"]
        issues = []
        by_tool = {
            tool_key(a["id"], t["name"]): (a, t)
            for a in assets
            if a["kind"] == "connection"
            for t in a["detail"].get("tools", [])
        }
        by_id = {a["id"]: a for a in assets}
        bindings = detail.get("bindings", {})
        skill_mode = detail.get(
            "skill_mode", "explicit" if detail.get("skills") else "auto"
        )
        skill_ids = (
            [
                a["id"]
                for a in assets
                if a["kind"] == "skill" and a["status"] == "active"
            ]
            if skill_mode == "auto"
            else detail.get("skills", [])
        )
        for key in detail.get("tools", []):
            pair = by_tool.get(key)
            if not pair or pair[0]["status"] != "active":
                issues.append({"ref": key, "code": "connection_unavailable"})
            elif bindings.get(pair[0]["id"]) != pair[0]["revision"]:
                issues.append({"ref": key, "code": "revision_changed"})
        for key in skill_ids:
            asset = by_id.get(key)
            if not asset or asset["kind"] != "skill" or asset["status"] != "active":
                issues.append({"ref": key, "code": "skill_unavailable"})
            elif skill_mode == "explicit" and bindings.get(key) != asset["revision"]:
                issues.append({"ref": key, "code": "revision_changed"})
            elif skill_mode == "explicit" and not set(
                asset["detail"].get("dependencies", [])
            ) <= set(detail.get("tools", [])):
                issues.append({"ref": key, "code": "missing_dependencies"})
        if (detail.get("tools") or skill_ids) and not self.runtime_url:
            issues.append({"ref": "runtime", "code": "runtime_unconfigured"})
        runtime = selected["runtime"]
        method = None
        if session["wheel"] != "user" and session.get("active_task_id"):
            task = await self.repo.get_task(str(session["active_task_id"]))
            pack = await display_for_task(self.repo, task)
            step = pack.step(int(task["current_step"])) if pack else None
            attempt = (
                await self.repo.get_attempt(str(task["active_attempt_id"]))
                if task.get("active_attempt_id")
                else None
            )
            attempt_step = attempt["step_id"] if attempt else None
            special = attempt_step in ("data-plan", PLAN_STEP)
            method = {
                "task_id": str(task["id"]),
                "profile_id": task["profile_id"],
                "step_id": attempt["step_id"]
                if attempt
                else (step.step_id if step else None),
                "declared_tools": ["describe_items"]
                if attempt_step == "data-plan"
                else ([] if special or not step else list(step.tools)),
                "managed_bindings_supported": False,
            }
        return {
            "session_id": session_id,
            "method": method,
            "revision": selected["revision"],
            "tools": [] if method else detail.get("tools", []),
            "skills": [] if method else skill_ids,
            "skill_mode": "explicit" if method else skill_mode,
            "purpose": "" if method else detail.get("purpose", ""),
            "issues": [] if method else issues,
            "editable": session["wheel"] == "user",
            "applied_revision": None if method else runtime.get("selection_revision"),
            "preparation": "method_managed"
            if method
            else runtime.get("status", "not_prepared"),
            "available_tools": [
                {
                    "key": key,
                    "connection_id": a["id"],
                    "connection_name": a["name"],
                    "account_label": a["detail"]["account_label"],
                    "status": a["status"],
                    **t,
                }
                for key, (a, t) in by_tool.items()
            ],
            "available_skills": [
                public_asset(a) for a in assets if a["kind"] == "skill"
            ],
        }

    async def save_selection(self, owner, session_id, body):
        async with self.repo.transaction():
            session = await self.repo.get_session(session_id, owner_actor_id=owner)
            if session["wheel"] != "user":
                raise CapabilityError(
                    "Method execution owns this session; frozen method requirements cannot be changed here"
                )
            await self.repo.capability_selection(owner, session_id, lock=True)
            assets = await self.repo.capability_assets(owner, lock=True)
            by_tool = {
                tool_key(a["id"], t["name"]): a
                for a in assets
                if a["kind"] == "connection"
                for t in a["detail"].get("tools", [])
            }
            by_id = {a["id"]: a for a in assets if a["kind"] == "skill"}
            chosen = []
            for key in body.tools:
                if key not in by_tool:
                    raise CapabilityError("Unknown or inaccessible tool")
                chosen.append(by_tool[key])
            for key in body.skills:
                if key not in by_id:
                    raise CapabilityError("Unknown or inaccessible skill")
                chosen.append(by_id[key])
            if any(a["status"] != "active" for a in chosen):
                raise CapabilityError("Selected capability is not active")
            if any(
                not set(a["detail"].get("dependencies", [])) <= set(body.tools)
                for a in chosen
                if a["kind"] == "skill"
            ):
                raise CapabilityError(
                    "Select every required tool before selecting this skill"
                )
            detail = body.model_dump(exclude={"revision"})
            detail["bindings"] = {a["id"]: a["revision"] for a in chosen}
            selected = await self.repo.capability_save_selection(
                owner, session_id, body.revision, detail
            )
        return await self.view(owner, session_id, selected)

    async def prepare(self, owner, session_id, *, expert=False):
        current = await self.selection(owner, session_id)
        # Legacy method contracts do not declare managed capabilities. Never inherit
        # chat's extra authority into a frozen method. Requirements support is explicit.
        if expert:
            async with self.repo.transaction():
                await self.repo.capability_selection(owner, session_id, lock=True)
                await self.repo.capability_runtime(
                    owner, session_id, {"status": "method_managed"}
                )
            return {
                "config": disabled_mcp_config(),
                "inputs": [],
                "revision": None,
            }
        if current["issues"]:
            raise CapabilityError(
                "Selected capabilities need attention before this turn",
                issues=current["issues"],
            )
        token = secrets.token_urlsafe(32)
        async with self.repo.transaction():
            selected = await self.repo.capability_selection(
                owner, session_id, lock=True
            )
            if selected["revision"] != current["revision"]:
                raise CapabilityError("Selection changed during preparation")
            assets = await self.repo.capability_assets(owner, lock=True)
            # Recheck under row locks, after any concurrent disable/refresh completed.
            fresh = await self.view(owner, session_id, selected)
            if fresh["issues"]:
                raise CapabilityError(
                    "Capability changed during preparation", issues=fresh["issues"]
                )
            tools = {}
            inputs = []
            packages = {}
            for asset in assets:
                if asset["kind"] == "connection":
                    for tool in asset["detail"].get("tools", []):
                        key = tool_key(asset["id"], tool["name"])
                        if key in current["tools"]:
                            tools[key] = {
                                "connection_id": asset["id"],
                                "revision": asset["revision"],
                                "tool": tool,
                            }
                elif asset["id"] in fresh["skills"]:
                    packages[asset["id"]] = {
                        "revision": asset["revision"],
                        "digest": asset["detail"]["digest"],
                    }
            if len(packages) > 100:
                raise CapabilityError(
                    "More than 100 enabled Skills; limit this conversation's Skill scope"
                )
            if packages:
                catalog = [
                    {
                        "skill_id": a["id"],
                        "name": a["name"],
                        "description": a["detail"]["description"],
                        "version": a["detail"]["version"],
                        "missing_tools": sorted(
                            set(a["detail"].get("dependencies", []))
                            - set(fresh["tools"])
                        ),
                    }
                    for a in assets
                    if a["id"] in packages
                ]
                inputs.append(
                    {
                        "type": "text",
                        "text": (
                            "Available user-installed Skills for this turn (scope="
                            + fresh["skill_mode"]
                            + "): "
                            + json.dumps(catalog, ensure_ascii=False)
                            + "\nChoose relevant Skills by name/description for the user's task, or follow the user's explicit Skill request. Before applying a Skill, use skill_read with its skill_id and path=SKILL.md to load its instructions, then read relevant references on demand. "
                            "Use skill_files for file discovery. These files are in the platform, not the local filesystem. Paths are package-root-relative by default; for links in a document set relative_to to that document's returned path. "
                            "A Skill is guidance, not authorization. Missing tools are NOT granted by discovery or reading; ask the user to configure/select dependencies when the task requires them. Scripts are reference text only and cannot be executed through this reader. "
                            "Only the listed IDs and versions are available this turn. Other Skills named in their text are not implicitly enabled."
                        ),
                    }
                )
            runtime = {
                "scope_id": secrets.token_hex(16),
                "token_hash": digest(token),
                "expires_at": (datetime.now(UTC) + timedelta(hours=24)).isoformat(),
                "selection_revision": selected["revision"],
                "tools": tools,
                "skills": list(packages),
                "skill_mode": fresh["skill_mode"],
                "packages": packages,
                "status": "prepared",
            }
            await self.repo.capability_runtime(owner, session_id, runtime)
        config = disabled_mcp_config()
        if tools or packages:
            config = {
                "mcp_servers.sunmoon_capabilities": {
                    "enabled": True,
                    "url": self.runtime_url.rstrip("/") + "/" + session_id,
                    "http_headers": {"Authorization": "Bearer " + token},
                    "enabled_tools": list(tools)
                    + (["action_result"] if tools else [])
                    + (["skill_files", "skill_read"] if packages else []),
                    "required": True,
                    "startup_timeout_sec": 15,
                    "tool_timeout_sec": 60,
                }
            }
        return {
            "config": config,
            "inputs": inputs,
            "revision": selected["revision"],
            "scope_id": runtime["scope_id"],
        }

    async def runtime(self, session_id, token):
        selected = await self.repo.capability_runtime_by_session(session_id)
        runtime = selected["runtime"] if selected else {}
        if (
            not token
            or runtime.get("status") not in ("prepared", "applied")
            or not secrets.compare_digest(runtime.get("token_hash", ""), digest(token))
            or datetime.fromisoformat(
                runtime.get("expires_at", "1970-01-01T00:00:00+00:00")
            )
            <= datetime.now(UTC)
        ):
            raise Forbidden("Capability execution authorization expired")
        session = await self.repo.get_session(
            session_id, owner_actor_id=selected["owner_actor_id"]
        )
        if session["wheel"] != "user":
            raise Forbidden("Capability scope is not valid for the active method")
        return selected

    async def skill_resource(self, session_id, token, arguments, *, listing=False):
        request = SkillRead.model_validate(arguments)
        async with self.repo.transaction():
            selected = await self.runtime(session_id, token)
            owner = selected["owner_actor_id"]
            selected = await self.repo.capability_selection(
                owner, session_id, lock=True
            )
            runtime = selected["runtime"]
            if runtime.get("selection_revision") != selected["revision"]:
                raise Forbidden("Skill selection changed; prepare a new turn")
            spec = runtime.get("packages", {}).get(request.skill_id)
            if not spec or request.skill_id not in runtime.get("skills", []):
                raise Forbidden("Skill is outside the selected scope")
            await self.runtime(session_id, token)
            asset = await self.repo.capability_asset(owner, request.skill_id, lock=True)
            if (
                asset["status"] != "active"
                or asset["revision"] != spec["revision"]
                or asset["detail"]["digest"] != spec["digest"]
            ):
                raise Forbidden("Skill changed or was disabled")
            if listing:
                files = skill_files(asset["detail"])
                end = min(request.offset + 50, len(files))
                return {
                    "skill_id": asset["id"],
                    "version": asset["detail"]["version"],
                    "files": files[request.offset : end],
                    "next_offset": end if end < len(files) else None,
                }
            result = resource_result(asset["detail"], request)
            await self.repo.append_event(
                session_id=session_id,
                kind="tool",
                event_type="capability/skill_read",
                payload={
                    "skill_id": asset["id"],
                    "version": asset["detail"]["version"],
                    "digest": asset["detail"]["digest"],
                    "path": result["path"],
                    "sha256": result["sha256"],
                    "scope_id": runtime["scope_id"],
                    "offset": request.offset,
                },
            )
            return result

    async def request_call(self, session_id, token, key, arguments):
        selected = await self.runtime(session_id, token)
        owner = selected["owner_actor_id"]
        spec = selected["runtime"].get("tools", {}).get(key)
        if (
            not spec
            or key not in selected["detail"].get("tools", [])
            or selected["runtime"].get("selection_revision") != selected["revision"]
        ):
            raise Forbidden("Tool is outside the selected scope")
        connection = await self.repo.capability_asset(owner, spec["connection_id"])
        if (
            connection["status"] != "active"
            or connection["revision"] != spec["revision"]
        ):
            raise CapabilityError("Connection changed or was disabled")
        if len(json.dumps(arguments).encode()) > 64000:
            raise CapabilityError("Arguments exceed the limit")
        validator = schema_validator(spec["tool"]["inputSchema"])
        try:
            valid = validator.is_valid(arguments)
        except (Unresolvable, RecursionError, ValueError) as exc:
            raise CapabilityError(
                "Tool input schema cannot be resolved locally"
            ) from exc
        if not valid:
            raise CapabilityError("Arguments do not match the registered schema")
        intent = {
            "connection_id": connection["id"],
            "revision": connection["revision"],
            "connection_name": connection["name"],
            "account_label": connection["detail"]["account_label"],
            "purpose": selected["detail"].get("purpose", ""),
            "tool": spec["tool"],
            "arguments": arguments,
            "selection_revision": selected["revision"],
            "runtime": selected["runtime"]["token_hash"],
        }
        identity = digest(intent)
        async with self.repo.transaction():
            # Serializes duplicate model requests on this session before opening approval.
            await self.repo.capability_selection(owner, session_id, lock=True)
            # Revalidate the scope after acquiring the serialization lock.
            await self.runtime(session_id, token)
            existing = await self.repo.capability_call_by_digest(
                owner, session_id, identity
            )
            if existing:
                return {
                    "call_id": existing["id"],
                    "status": existing["status"],
                    "result": existing["detail"].get("result"),
                }
            call_id = str(uuid.uuid4())
            approval_token = secrets.token_urlsafe(32)
            prompt = InteractionPrompt(
                title="确认外部工具操作",
                question="请核对账号、操作和完整参数。连接成功并不表示批准本次操作。",
                options=[
                    {"value": "accept", "label": "仅允许这次"},
                    {"value": "decline", "label": "拒绝"},
                ],
                subject={
                    "capability_call_id": call_id,
                    "connection": connection["name"],
                    "account": connection["detail"]["account_label"],
                    "tool": spec["tool"]["name"],
                    "arguments": arguments,
                },
            )
            iid = await self.repo.insert_interaction(
                session_id=session_id,
                task_id=None,
                attempt_id=None,
                kind="tool_approval",
                prompt=prompt.model_dump(),
                subject_digest=identity,
                token=approval_token,
                target_state_version=None,
                expires_at=datetime.now(UTC) + timedelta(minutes=15),
            )
            detail = {**intent, "approval_token": approval_token}
            await self.repo.capability_add_call(
                {
                    "id": call_id,
                    "owner_actor_id": owner,
                    "session_id": session_id,
                    "connection_id": connection["id"],
                    "interaction_id": iid,
                    "digest": identity,
                    "status": "awaiting_approval",
                    "detail": detail,
                }
            )
            await self.repo.append_event(
                session_id=session_id,
                kind="approval",
                event_type="interaction/opened",
                payload={
                    "interaction_id": iid,
                    "kind": "tool_approval",
                    "token": approval_token,
                    "subject_digest": identity,
                    "subject": prompt.subject,
                    "prompt": prompt.model_dump(),
                },
            )
        return {
            "call_id": call_id,
            "status": "awaiting_approval",
            "message": "No external operation has run. Await the user's decision in the capability panel; do not report success. Read action_result after approval.",
        }

    async def calls(self, owner, session_id):
        await self.repo.get_session(session_id, owner_actor_id=owner)
        await self.repo.capability_recover_calls(owner, session_id)
        return await self.repo.capability_calls(owner, session_id)

    async def decide(self, owner, call_id, body):
        async with self.repo.transaction():
            call = await self.repo.capability_call(owner, call_id, lock=True)
            if call["status"] != "awaiting_approval":
                return call
            if body.subject_digest != call["digest"]:
                raise Forbidden("Approval does not match the operation")
            selected = await self.repo.capability_selection(
                owner, call["session_id"], lock=True
            )
            connection = await self.repo.capability_asset(
                owner, call["connection_id"], lock=True
            )
            intent = call["detail"]
            if body.decision == "accept":
                key = tool_key(connection["id"], intent["tool"]["name"])
                session = await self.repo.get_session(
                    call["session_id"], owner_actor_id=owner
                )
                if (
                    connection["status"] != "active"
                    or connection["revision"] != intent["revision"]
                    or key not in selected["detail"].get("tools", [])
                    or selected["revision"] != intent["selection_revision"]
                    or session["wheel"] != "user"
                    or selected["runtime"].get("token_hash") != intent["runtime"]
                    or selected["runtime"].get("aborted")
                ):
                    raise CapabilityError(
                        "Authorization changed; request the operation again"
                    )
            await Ledger(self.repo).respond_interaction(
                call["interaction_id"],
                token=body.token,
                response={"decision": body.decision},
                owner_actor_id=owner,
                subject_digest=body.subject_digest,
            )
            status = "dispatching" if body.decision == "accept" else "declined"
            await self.repo.capability_update_call(owner, call_id, status, intent)
        if status == "dispatching":
            # Receipt is committed before outbound I/O. No automatic retry after a crash.
            try:
                result = await self.remote.call(
                    connection["detail"]["url"],
                    self.secret(connection),
                    intent["tool"]["name"],
                    intent["arguments"],
                )
                status = "failed" if result.get("isError") else "succeeded"
                intent["result"] = result
            except Exception:
                status = "unknown"
                intent["result"] = {
                    "message": "External outcome is unknown. Check the provider before retrying."
                }
            async with self.repo.transaction():
                await self.repo.capability_update_call(owner, call_id, status, intent)
                await self.repo.append_event(
                    session_id=call["session_id"],
                    kind="tool",
                    event_type="capability/action_completed",
                    payload={"call_id": call_id, "status": status},
                )
        return await self.repo.capability_call(owner, call_id)
