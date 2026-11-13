"""Shared user capability management and execution. No credential leaves this boundary."""

from __future__ import annotations

import asyncio
import json
import secrets
import uuid
from datetime import UTC, datetime, timedelta
from typing import Any, Protocol

from jsonschema import Draft202012Validator
from jsonschema.exceptions import SchemaError
from referencing import Registry
from referencing.exceptions import Unresolvable

from app.application.workbench.ledger import Ledger
from app.application.workbench.method_capabilities import (
    check_runtime_method,
    method_scope,
    selection_issues,
)
from app.domain.workbench.capabilities import (
    CapabilityError,
    digest,
    disabled_mcp_config,
    public_asset,
    tool_key,
)
from app.domain.workbench.errors import Forbidden
from app.domain.workbench.method_capabilities import matches_skill, matches_tool
from app.domain.workbench.models import InteractionPrompt
from app.domain.workbench.projects import ExecutionScene
from app.domain.workbench.skill_dependencies import (
    missing_skill_dependencies,
    public_skill_asset,
    skill_dependencies,
)
from app.domain.workbench.skill_execution import (
    execution_input,
    public_call,
    public_result,
    script_tool,
)
from app.domain.workbench.skill_installation import repository_packages
from app.domain.workbench.skill_package import (
    SkillPackageCreate,
    SkillRead,
    resource_result,
    skill_files,
    unpack_skill,
)
from app.domain.workbench.states import TaskState, WaitingReason


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
        self,
        repo: Any,
        remote: McpPort,
        cipher: Any = None,
        *,
        runtime_url: str = "",
        skill_source: Any = None,
        script_executor: Any = None,
    ):
        self.repo, self.remote, self.cipher, self.runtime_url = (
            repo,
            remote,
            cipher,
            runtime_url,
        )
        self.skill_source = skill_source
        self.script_executor = script_executor

    def secret(self, asset):
        if not asset.get("secret"):
            return None
        if self.cipher is None:
            raise CapabilityError("Credential storage is unavailable")
        return self.cipher.decrypt(asset["secret"].encode()).decode()

    async def assets(self, owner):
        assets = await self.repo.capability_assets(owner)
        return [public_skill_asset(a, assets) for a in assets]

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

    async def preview_skill_source(self, body):
        if self.skill_source is None:
            raise CapabilityError("Repository installation is not configured")
        source = await self.skill_source.fetch(body.url)
        candidates = await asyncio.to_thread(
            repository_packages, source["archive"], source["prefix"]
        )
        return {
            "repository": source["repository"],
            "commit": source["commit"],
            "candidates": [c for c, _ in candidates],
        }

    async def install_skill(self, owner, body):
        if self.skill_source is None:
            raise CapabilityError("Repository installation is not configured")
        source = await self.skill_source.fetch(body.url, body.commit)
        packages = await asyncio.to_thread(
            repository_packages, source["archive"], source["prefix"], only=body.path
        )
        if len(packages) != 1 or packages[0][0]["digest"] != body.digest:
            raise CapabilityError(
                "Skill content differs from the preview; review its source again"
            )
        candidate, encoded = packages[0]
        return await self.create_skill(
            owner,
            SkillPackageCreate(
                request_id=body.request_id,
                name=candidate["name"],
                description=candidate["description"],
                version=source["commit"],
                archive_base64=encoded,
                dependencies=body.dependencies,
                tool_requirements=body.tool_requirements,
            ),
            package=True,
            source={
                "repository": source["repository"],
                "commit": source["commit"],
                "path": body.path,
            },
        )

    async def create_skill(self, owner, body, *, package=False, source=None):
        if len(body.dependencies) + len(body.tool_requirements) > 30:
            raise CapabilityError("A Skill may declare at most 30 tool dependencies")
        if len({(r.url, r.name) for r in body.tool_requirements}) != len(
            body.tool_requirements
        ):
            raise CapabilityError("Duplicate Skill tool requirements")
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
            "request_digest": digest(
                {**body.model_dump(), **({"source": source} if source else {})}
            ),
            "detail": {
                "description": body.description,
                "version": body.version,
                "dependencies": body.dependencies,
                **(
                    {
                        "tool_requirements": [
                            r.model_dump() for r in body.tool_requirements
                        ]
                    }
                    if body.tool_requirements
                    else {}
                ),
                **material,
                **({"source": source} if source else {}),
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
            if asset["kind"] != "connection":
                raise CapabilityError("Use remove to uninstall a Skill")
            status, default, secret = "disconnected", False, None
            detail["revocation"] = (
                "local_access_removed; revoke bearer credential at provider if required"
            )
        elif body.action == "remove":
            if asset["kind"] != "skill":
                raise CapabilityError("Use disconnect for an MCP connection")
            # Preserve immutable content and past receipts; revoke further use.
            status, default, secret = "disconnected", False, None
        elif body.action in ("enable", "disable"):
            if status == "disconnected":
                raise CapabilityError(
                    "Removed assets must be installed or connected again"
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
        scope = await method_scope(self.repo, owner, session)
        if scope:
            detail = detail.get("method_selections", {}).get(
                scope["context"],
                {"tools": [], "skills": [], "skill_mode": "explicit", "bindings": {}},
            )
        issues = selection_issues(scope, detail, assets) if scope else []
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
            elif skill_mode == "explicit" and missing_skill_dependencies(
                asset, assets, detail.get("tools", [])
            ):
                issues.append({"ref": key, "code": "missing_dependencies"})
        if (detail.get("tools") or skill_ids) and not self.runtime_url:
            issues.append({"ref": "runtime", "code": "runtime_unconfigured"})
        runtime = selected["runtime"]
        method = None
        if scope:
            task, step, attempt = scope["task"], scope["step"], scope["attempt"]
            attempt_step = attempt["step_id"] if attempt else None
            special = scope["special"]
            method = {
                "task_id": str(task["id"]),
                "profile_id": task["profile_id"],
                "step_id": attempt["step_id"]
                if attempt and special
                else (step.step_id if step else None),
                "declared_tools": ["describe_items"]
                if special and attempt_step == "data-plan"
                else ([] if special or not step else list(step.tools)),
                "managed_bindings_supported": True,
                "context": scope["context"],
                "requirements": scope["requirements"].model_dump(mode="json"),
            }
        return {
            "session_id": session_id,
            "method": method,
            "revision": selected["revision"],
            "tools": detail.get("tools", []),
            "skills": skill_ids,
            "skill_mode": "explicit" if method else skill_mode,
            "purpose": detail.get("purpose", ""),
            "issues": issues,
            "editable": scope["editable"] if scope else session["wheel"] == "user",
            "applied_revision": runtime.get("selection_revision"),
            "preparation": "method_managed"
            if scope
            and (runtime.get("method") or {}).get("context") != scope["context"]
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
                if not scope or matches_tool(scope["requirements"], a, t)
            ],
            "available_skills": [
                public_skill_asset(a, assets, detail.get("tools", []))
                for a in assets
                if a["kind"] == "skill"
                and (not scope or matches_skill(scope["requirements"], a, assets))
            ],
        }

    async def save_selection(self, owner, session_id, body):
        async with self.repo.transaction():
            selected = await self.repo.capability_selection(
                owner, session_id, lock=True
            )
            session = await self.repo.get_session(session_id, owner_actor_id=owner)
            scope = await method_scope(self.repo, owner, session, lock=True)
            if scope:
                if not scope["editable"] or body.method_context != scope["context"]:
                    raise CapabilityError(
                        "Method changed or is executing; reload at an idle step boundary"
                    )
                if body.skill_mode != "explicit":
                    raise CapabilityError("Method Skill scope must be explicit")
            elif session["wheel"] != "user" or body.method_context is not None:
                raise CapabilityError(
                    "Method execution owns this session; frozen method requirements cannot be changed here"
                )
            assets = await self.repo.capability_assets(owner, lock=True)
            if scope and selection_issues(scope, body.model_dump(), assets):
                raise CapabilityError(
                    "Bind exactly the declared method capabilities",
                    issues=selection_issues(scope, body.model_dump(), assets),
                )
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
                missing_skill_dependencies(a, assets, body.tools)
                for a in chosen
                if a["kind"] == "skill"
            ):
                raise CapabilityError(
                    "Select every required tool before selecting this skill"
                )
            detail = body.model_dump(exclude={"revision", "method_context"})
            detail["bindings"] = {a["id"]: a["revision"] for a in chosen}
            if scope:
                detail = {
                    **selected["detail"],
                    "method_selections": {
                        **selected["detail"].get("method_selections", {}),
                        scope["context"]: detail,
                    },
                }
            elif selected["detail"].get("method_selections"):
                detail["method_selections"] = selected["detail"]["method_selections"]
            selected = await self.repo.capability_save_selection(
                owner, session_id, body.revision, detail
            )
        return await self.view(owner, session_id, selected)

    async def hold_method(self, owner, session_id):
        """Pause before opening an attempt or reserving model budget for missing bindings."""
        async with self.repo.transaction():
            selected = await self.repo.capability_selection(
                owner, session_id, lock=True
            )
            session = await self.repo.get_session(session_id, owner_actor_id=owner)
            scope = await method_scope(self.repo, owner, session, lock=True)
            if not scope:
                raise CapabilityError("Method task is no longer active")
            view = await self.view(owner, session_id, selected)
            if not view["issues"]:
                return False
            await Ledger(self.repo).open_interaction(
                str(scope["task"]["id"]),
                kind="question",
                waiting_reason=WaitingReason.INPUT,
                prompt=InteractionPrompt(
                    title="配置本步骤的工具与技能",
                    question="请打开「本次能力」，选择本步骤所需的连接和技能并保存；完成后继续。尚未启动本步骤的模型调用。",
                    options=[{"value": "continue", "label": "已配置，继续"}],
                    subject={
                        "capability_binding": scope["context"],
                        "step_id": view["method"]["step_id"],
                    },
                ),
            )
            return True

    async def prepare(
        self, owner, session_id, *, scene: ExecutionScene = ExecutionScene.CONVERSATION
    ):
        scene = ExecutionScene(scene)
        current = await self.selection(owner, session_id)
        if scene == ExecutionScene.CONVERSATION and current["method"]:
            raise CapabilityError("Active method tasks require method execution scope")
        # Legacy method contracts do not declare managed capabilities. Never inherit
        # chat's extra authority into a frozen method. Requirements support is explicit.
        if scene == ExecutionScene.METHOD and not current["method"]:
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
            method_binding = None
            if scene == ExecutionScene.METHOD and fresh["method"]:
                session = await self.repo.get_session(session_id, owner_actor_id=owner)
                scope = await method_scope(self.repo, owner, session, lock=True)
                if (
                    scope
                    and not scope["special"]
                    and (fresh["tools"] or fresh["skills"])
                ):
                    if not scope["attempt"]:
                        raise CapabilityError("Method has no active attempt")
                    method_binding = {
                        "context": scope["context"],
                        "task_id": str(scope["task"]["id"]),
                        "attempt_id": str(scope["attempt"]["id"]),
                        "step_id": scope["step"].step_id,
                    }
            for asset in assets:
                if asset["kind"] == "connection":
                    for tool in asset["detail"].get("tools", []):
                        key = tool_key(asset["id"], tool["name"])
                        if key in fresh["tools"]:
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
                    tool = script_tool(asset) if self.script_executor else None
                    if tool:
                        tools[tool_key(asset["id"], "skill_run")] = {
                            "connection_id": asset["id"],
                            "revision": asset["revision"],
                            "tool": tool,
                            "execution_kind": "skill_script",
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
                        "script_tool": tool_key(a["id"], "skill_run")
                        if tool_key(a["id"], "skill_run") in tools
                        else None,
                        "missing_tools": sorted(
                            set(a["detail"].get("dependencies", []))
                            - set(fresh["tools"])
                        ),
                        "connection_dependency_count": len(
                            a["detail"].get("dependencies", [])
                        )
                        + len(a["detail"].get("tool_requirements", [])),
                        "unresolved_connection_dependencies": sum(
                            d["status"] != "selected"
                            for d in skill_dependencies(a, assets, fresh["tools"])
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
                            "A Skill is guidance, not authorization. Missing tools are NOT granted by discovery or reading; ask the user to configure/select dependencies when the task requires them. When script_tool is present, use that tool to run a package script in an isolated cloud container after approval; it has no network, credentials or local project files. Never substitute cloud execution for an unavailable local project environment. Without script_tool, scripts are reference text only. "
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
                "method": method_binding,
                "purpose": fresh["purpose"],
            }
            if method_binding:
                await check_runtime_method(self.repo, owner, session, runtime)
                inputs.append(
                    {
                        "type": "text",
                        "text": "This method step may use only the listed managed capabilities. "
                        "External tool calls wait for user approval and return the actual operation result. "
                        "A refusal, expired approval or unknown outcome is not a successful operation. "
                        "Do not retry an unknown external write automatically.",
                    }
                )
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
                    "tool_timeout_sec": 960
                    if method_binding
                    or any(
                        spec.get("execution_kind") == "skill_script"
                        for spec in tools.values()
                    )
                    else 60,
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
            or not selected
            or runtime.get("selection_revision") != selected["revision"]
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
        try:
            await check_runtime_method(
                self.repo, selected["owner_actor_id"], session, runtime
            )
        except CapabilityError as exc:
            raise Forbidden(exc.message) from exc
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
            if result["path"] == "SKILL.md" and request.offset == 0:
                dependencies = skill_dependencies(
                    asset,
                    await self.repo.capability_assets(owner),
                    runtime.get("tools", {}),
                )
                result["connection_dependencies"] = [
                    {
                        "service_url": d["service_url"],
                        "tool_name": d["tool_name"],
                        "status": d["status"],
                        "selected_tools": [
                            c["key"]
                            for c in d["candidates"]
                            if c["key"] in runtime.get("tools", {})
                            and c["status"] == "active"
                        ],
                    }
                    for d in dependencies
                ]
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
            or (
                not selected["runtime"].get("method")
                and spec.get("execution_kind") != "skill_script"
                and key not in selected["detail"].get("tools", [])
            )
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
            "account_label": connection["detail"].get(
                "account_label", "隔离云端容器（无联网与本地文件访问）"
            ),
            "purpose": selected["runtime"].get(
                "purpose", selected["detail"].get("purpose", "")
            ),
            "tool": spec["tool"],
            "arguments": arguments,
            "selection_revision": selected["revision"],
            "runtime": selected["runtime"]["token_hash"],
            "method": selected["runtime"].get("method"),
            **(
                {"execution_kind": "skill_script"}
                if spec.get("execution_kind") == "skill_script"
                else {}
            ),
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
                    "result": public_result(
                        existing["detail"].get("result"), existing["id"]
                    ),
                }
            binding = selected["runtime"].get("method")
            if binding:
                session = await self.repo.get_session(session_id, owner_actor_id=owner)
                await check_runtime_method(
                    self.repo, owner, session, selected["runtime"], lock=True
                )
                task = await self.repo.get_task(binding["task_id"])
                if task["state"] != TaskState.RUNNING or task.get(
                    "active_interaction_id"
                ):
                    raise CapabilityError(
                        "Resolve the current method approval before another operation"
                    )
            call_id = str(uuid.uuid4())
            approval_token = secrets.token_urlsafe(32)
            prompt = InteractionPrompt(
                title="确认运行技能脚本"
                if intent.get("execution_kind") == "skill_script"
                else "确认外部工具操作",
                question="请核对技能、脚本和输入。本次在隔离云端容器执行，不读取本地项目、不连接网络。"
                if intent.get("execution_kind") == "skill_script"
                else "请核对账号、操作和完整参数。连接成功并不表示批准本次操作。",
                options=[
                    {"value": "accept", "label": "仅允许这次"},
                    {"value": "decline", "label": "拒绝"},
                ],
                subject={
                    "capability_call_id": call_id,
                    "connection": connection["name"],
                    "account": intent["account_label"],
                    "tool": spec["tool"]["name"],
                    "arguments": arguments,
                },
            )
            if binding:
                opened = await Ledger(self.repo).open_interaction(
                    binding["task_id"],
                    attempt_id=binding["attempt_id"],
                    kind="tool_approval",
                    prompt=prompt,
                    subject_digest=identity,
                    waiting_reason=WaitingReason.APPROVAL,
                    ttl=timedelta(minutes=15),
                )
                iid, approval_token = opened["interaction_id"], opened["token"]
            else:
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
            if not binding:
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
        return [
            public_call(c) for c in await self.repo.capability_calls(owner, session_id)
        ]

    async def method_call_result(self, session_id, token, call_id):
        """Short polling transaction; no connection is held while the HTTP stream sleeps."""
        async with self.repo.transaction():
            selected = await self.runtime(session_id, token)
            owner = selected["owner_actor_id"]
            await self.repo.capability_recover_calls(owner, session_id)
            call = await self.repo.capability_call(owner, call_id)
            if (
                call["session_id"] != session_id
                or call["detail"].get("method") != selected["runtime"].get("method")
                or call["detail"]["runtime"] != selected["runtime"]["token_hash"]
            ):
                raise Forbidden("Operation belongs to another method execution")
            if call["status"] == "expired":
                binding = call["detail"].get("method")
                if binding:
                    task = await self.repo.get_task(binding["task_id"], for_update=True)
                    if task["state"] == TaskState.WAITING and str(
                        task.get("active_interaction_id")
                    ) == str(call["interaction_id"]):
                        await self.repo.cas_task(
                            str(task["id"]),
                            expected_version=task["state_version"],
                            active_interaction_id=None,
                        )
                        await Ledger(self.repo).transition(
                            str(task["id"]),
                            TaskState.QUEUED,
                            reason={"capability_approval_expired": call_id},
                        )
                        await Ledger(self.repo).transition(
                            str(task["id"]),
                            TaskState.RUNNING,
                            reason={"resume_active_attempt": binding["attempt_id"]},
                        )
            return {
                "call_id": call_id,
                "status": call["status"],
                "result": public_result(call["detail"].get("result"), call["id"]),
            }

    async def decide(self, owner, call_id, body):
        async with self.repo.transaction():
            call = await self.repo.capability_call(owner, call_id, lock=True)
            if call["status"] != "awaiting_approval":
                return public_call(call)
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
                    or key not in selected["runtime"].get("tools", {})
                    or selected["revision"] != intent["selection_revision"]
                    or selected["runtime"].get("token_hash") != intent["runtime"]
                    or selected["runtime"].get("aborted")
                    or (
                        intent.get("method")
                        and selected["runtime"].get("status")
                        not in ("prepared", "applied")
                    )
                ):
                    raise CapabilityError(
                        "Authorization changed; request the operation again"
                    )
                await check_runtime_method(
                    self.repo, owner, session, selected["runtime"], lock=True
                )
                if intent.get("method") != selected["runtime"].get("method"):
                    raise CapabilityError(
                        "Method approval belongs to another execution"
                    )
            await Ledger(self.repo).respond_interaction(
                call["interaction_id"],
                token=body.token,
                response={"decision": body.decision},
                owner_actor_id=owner,
                subject_digest=body.subject_digest,
            )
            if intent.get("method"):
                await Ledger(self.repo).transition(
                    intent["method"]["task_id"],
                    TaskState.RUNNING,
                    reason={"resume_active_attempt": intent["method"]["attempt_id"]},
                )
            status = "dispatching" if body.decision == "accept" else "declined"
            await self.repo.capability_update_call(owner, call_id, status, intent)
        if status == "dispatching":
            # Receipt is committed before outbound I/O. No automatic retry after a crash.
            try:
                if intent.get("execution_kind") == "skill_script":
                    if self.script_executor is None:
                        raise CapabilityError("Script execution is unavailable")
                    await self.script_executor.start(
                        owner, call_id, execution_input(connection, intent["arguments"])
                    )
                    # The runner reconciles the asynchronous Job into this same call.
                    return public_call(await self.repo.capability_call(owner, call_id))
                else:
                    result = await self.remote.call(
                        connection["detail"]["url"],
                        self.secret(connection),
                        intent["tool"]["name"],
                        intent["arguments"],
                    )
                    status = "failed" if result.get("isError") else "succeeded"
                    intent["result"] = result
            except CapabilityError as exc:
                status = "failed"
                intent["result"] = {"isError": True, "message": exc.message}
            except Exception:
                status = "unknown"
                intent["result"] = {
                    "message": "Script submission outcome is unknown; it was not retried. Any created container is bounded by its execution deadline."
                    if intent.get("execution_kind") == "skill_script"
                    else "External outcome is unknown. Check the provider before retrying."
                }
            async with self.repo.transaction():
                await self.repo.capability_update_call(owner, call_id, status, intent)
                await self.repo.append_event(
                    session_id=call["session_id"],
                    kind="tool",
                    event_type="capability/action_completed",
                    payload={"call_id": call_id, "status": status},
                )
        return public_call(await self.repo.capability_call(owner, call_id))

    async def cancel_script(self, owner, call_id):
        call = await self.repo.capability_call(owner, call_id)
        if call["detail"].get("execution_kind") != "skill_script":
            raise CapabilityError("Only script executions can be stopped here")
        if call["status"] != "dispatching":
            return public_call(call)
        if self.script_executor is None:
            raise CapabilityError(
                "Script executor is unavailable; its execution deadline remains in force"
            )
        await self.script_executor.cancel(owner, call_id)
        async with self.repo.transaction():
            current = await self.repo.capability_call(owner, call_id, lock=True)
            if current["status"] == "dispatching":
                await self.repo.capability_update_call(
                    owner,
                    call_id,
                    "failed",
                    {
                        **current["detail"],
                        "result": {
                            "isError": True,
                            "message": "Stopped at the user's request",
                        },
                    },
                )
                await self.repo.append_event(
                    session_id=call["session_id"],
                    kind="tool",
                    event_type="capability/action_completed",
                    payload={"call_id": call_id, "status": "failed"},
                )
        return public_call(await self.repo.capability_call(owner, call_id))

    async def reconcile_scripts(self):
        """Runner-owned receipt collection, cancellation and recovery; never retry execution."""
        if self.script_executor is None:
            return
        for call in await self.repo.capability_pending_scripts():
            owner, call_id, intent = call["owner_actor_id"], call["id"], call["detail"]
            status, result = None, None
            async with self.repo.transaction():
                selected = await self.repo.capability_selection(
                    owner, call["session_id"]
                )
                asset = await self.repo.capability_asset(owner, call["connection_id"])
                runtime = selected["runtime"]
                valid = (
                    asset["status"] == "active"
                    and asset["revision"] == intent["revision"]
                    and runtime.get("token_hash") == intent["runtime"]
                    and selected["revision"] == intent["selection_revision"]
                    and not runtime.get("aborted")
                    and datetime.fromisoformat(
                        runtime.get("expires_at", "1970-01-01T00:00:00+00:00")
                    )
                    > datetime.now(UTC)
                )
                if valid and intent.get("method"):
                    session = await self.repo.get_session(
                        call["session_id"], owner_actor_id=owner
                    )
                    try:
                        await check_runtime_method(self.repo, owner, session, runtime)
                    except CapabilityError:
                        valid = False
            try:
                if not valid:
                    await self.script_executor.cancel(owner, call_id)
                    status, result = (
                        "failed",
                        {
                            "isError": True,
                            "message": "Script stopped: execution authorization was revoked or the task ended",
                        },
                    )
                else:
                    receipt = await self.script_executor.status(owner, call_id)
                    if receipt["state"] == "finished":
                        result = receipt["result"]
                        result["isError"] = (
                            result["exit_code"] != 0 or result["timed_out"]
                        )
                        status = "failed" if result["isError"] else "succeeded"
                    elif receipt["state"] == "failed":
                        status, result = (
                            "failed",
                            {"isError": True, "message": receipt.get("error")},
                        )
                    elif receipt["state"] == "absent" and datetime.now(UTC) - call[
                        "updated_at"
                    ] > timedelta(seconds=30):
                        status, result = (
                            "unknown",
                            {
                                "isError": True,
                                "message": "Script receipt is unavailable; execution was not retried",
                            },
                        )
                    if not status and datetime.now(UTC) - call[
                        "updated_at"
                    ] > timedelta(minutes=2):
                        await self.script_executor.cancel(owner, call_id)
                        status, result = (
                            "unknown",
                            {
                                "isError": True,
                                "message": "Script execution deadline exceeded; execution was not retried",
                            },
                        )
            except Exception:
                # A temporary provider outage does not grant execution or restart work.
                continue
            if not status:
                continue
            async with self.repo.transaction():
                current = await self.repo.capability_call(owner, call_id, lock=True)
                if current["status"] != "dispatching":
                    continue
                await self.repo.capability_update_call(
                    owner, call_id, status, {**intent, "result": result}
                )
                await self.repo.append_event(
                    session_id=call["session_id"],
                    kind="tool",
                    event_type="capability/action_completed",
                    payload={"call_id": call_id, "status": status},
                )
            # The immutable result is durable before removal of temporary input/output.
            try:
                await self.script_executor.cancel(owner, call_id)
            except Exception:
                pass  # Job TTL removes leftovers; no user data outside this run is touched.
