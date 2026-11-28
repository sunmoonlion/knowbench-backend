"""Candidate-only MCP scope for one authenticated model incubation turn."""

import secrets
from datetime import UTC, datetime, timedelta

from app.application.workbench.formal_host_calls import FormalHostCalls
from app.application.workbench.tool_invocation import verify_tool_invocation
from app.domain.workbench.capabilities import CapabilityError, digest
from app.domain.workbench.errors import Forbidden
from app.domain.workbench.formal_execution import direct_procedure, snapshot_for_task
from app.domain.workbench.formal_model import (
    PROBE_SERVER,
    PROBE_TOOL,
    validate_model_call,
)
from app.domain.workbench.formal_slot_schema import slot_object_schema
from app.domain.workbench.states import ATTEMPT_TERMINAL, TaskState


def bind_model_intent(context, model):
    intent = context.intent
    candidate = intent.pop("formal_host")
    intent.update(
        execution_kind="formal_probe",
        formal_probe=candidate,
        invocation=model["invocation"].snapshot(),
        runtime=model["selected"]["runtime"]["token_hash"],
        selection_revision=model["selected"]["revision"],
    )


class FormalModelCalls:
    def __init__(self, capabilities):
        self.capabilities, self.repo = capabilities, capabilities.repo

    async def scope(self, owner, session, runtime, *, lock=False):
        bound = runtime.get("formal_probe") or {}
        if (
            not bound
            or session["wheel"] != "runtime"
            or str(session.get("active_task_id")) != bound.get("task_id")
        ):
            raise CapabilityError("Model probe no longer owns this session")
        task = await self.repo.get_task(
            bound["task_id"], owner_actor_id=owner, for_update=lock
        )
        attempt = await self.repo.get_attempt(bound["attempt_id"], for_update=lock)
        snapshot = snapshot_for_task(task)
        if (
            snapshot.caller != "model"
            or task["pack_digest"] != bound.get("snapshot_digest")
            or str(task["session_id"]) != str(session["id"])
            or str(task.get("active_attempt_id")) != bound["attempt_id"]
            or str(attempt["task_id"]) != str(task["id"])
            or task["state"] not in (TaskState.RUNNING, TaskState.WAITING)
            or task.get("cancel_requested_at")
            or attempt["status"] in ATTEMPT_TERMINAL
            or (attempt.get("refs") or {}).get("snapshot_digest") != task["pack_digest"]
            or not (attempt.get("refs") or {}).get("formal_model_probe")
        ):
            raise CapabilityError("Model probe identity or active attempt changed")
        if task["state"] == TaskState.WAITING:
            if not task.get("active_interaction_id"):
                raise CapabilityError("Probe is waiting outside its direct operation")
            interaction = await self.repo.get_interaction(
                str(task["active_interaction_id"])
            )
            if str(interaction.get("attempt_id")) != bound["attempt_id"] or not (
                (interaction.get("prompt") or {}).get("subject") or {}
            ).get("capability_call_id"):
                raise CapabilityError("Probe is waiting outside its direct operation")
        return task, attempt, snapshot

    async def prepare(self, owner, session_id):
        if not self.capabilities.runtime_url:
            raise CapabilityError("Candidate MCP gateway is unavailable")
        async with self.repo.transaction():
            session = await self.repo.get_session(session_id, owner_actor_id=owner)
            task = await self.repo.get_task(
                str(session["active_task_id"]), owner_actor_id=owner, for_update=True
            )
            attempt_id = str(task.get("active_attempt_id"))
            snapshot = snapshot_for_task(task)
            bound = {
                "task_id": str(task["id"]),
                "attempt_id": attempt_id,
                "snapshot_digest": task["pack_digest"],
            }
            _, attempt, _ = await self.scope(
                owner, session, {"formal_probe": bound}, lock=True
            )
            if (attempt.get("refs") or {}).get("runtime_turn_id"):
                raise CapabilityError("This model probe already started its only turn")
            context = await FormalHostCalls(self.capabilities).context(
                owner, str(task["id"]), attempt_id, caller="model"
            )
            procedure = direct_procedure(snapshot.draft, caller="model")
            tool = {
                "name": PROBE_TOOL,
                "description": "Call this exact candidate method once using the supplied test inputs. User approval is required. Its result, not model prose, is the method output.",
                "inputSchema": slot_object_schema(procedure.inputs),
            }
            selected = await self.repo.capability_selection(
                owner, session_id, lock=True
            )
            token = secrets.token_urlsafe(32)
            runtime = {
                "scope_id": secrets.token_hex(16),
                "token_hash": digest(token),
                "expires_at": (datetime.now(UTC) + timedelta(hours=1)).isoformat(),
                "selection_revision": selected["revision"],
                "status": "prepared",
                "formal_probe": bound,
                "tools": {
                    PROBE_TOOL: {
                        "execution_kind": "formal_probe",
                        "tool": tool,
                        "implementation_tool": context.intent["tool"],
                        "service_url": context.registration.service,
                        "operation_digest": context.access["operation_digest"],
                        "access": context.access,
                        "source_asset_id": context.intent["source_asset_id"],
                        **(
                            {"registered_source": context.intent["registered_source"]}
                            if "registered_source" in context.intent
                            else {}
                        ),
                        "revision": context.asset["revision"],
                    }
                },
                "packages": {},
                "skills": [],
                "method": None,
            }
            await self.repo.capability_runtime(owner, session_id, runtime)
        return {
            "scope_id": runtime["scope_id"],
            "revision": selected["revision"],
            "inputs": [],
            "config": {
                "mcp_servers.sunmoon_capabilities": {
                    "enabled": True,
                    "url": self.capabilities.runtime_url.rstrip("/") + "/" + session_id,
                    "http_headers": {"Authorization": "Bearer " + token},
                    "enabled_tools": [PROBE_TOOL],
                    "required": True,
                    "startup_timeout_sec": 15,
                    "tool_timeout_sec": 960,
                }
            },
        }

    async def request(self, session_id, token, key, arguments, *, meta):
        if key != PROBE_TOOL:
            raise Forbidden(
                "Only the pinned candidate is available to this model probe"
            )
        invocation = await self.capabilities.verify_invocation(
            session_id, token, key, arguments, meta
        )
        selected = await self.capabilities.runtime(session_id, token)
        owner = selected["owner_actor_id"]
        async with self.repo.transaction():
            session = await self.repo.get_session(session_id, owner_actor_id=owner)
            task, attempt, snapshot = await self.scope(
                owner, session, selected["runtime"], lock=True
            )
            await self.capabilities.verify_invocation(
                session_id, token, key, arguments, meta, observe_seconds=0
            )
            if digest(arguments) != digest(snapshot.arguments):
                raise Forbidden("Model probe arguments differ from its case input")
            probe = (attempt.get("refs") or {}).get("formal_model_probe") or {}
            if probe.get("invocation_key") not in (None, invocation.key):
                raise Forbidden("This model probe permits only one method invocation")
            call = await FormalHostCalls(self.capabilities).request(
                owner,
                str(task["id"]),
                str(attempt["id"]),
                model={"invocation": invocation, "selected": selected},
            )
        return {
            "call_id": call["id"],
            "status": call["status"],
            "result": call["detail"].get("result"),
        }

    async def existing_model(self, owner, call):
        selected = await self.repo.capability_selection(owner, call["session_id"])
        runtime = selected["runtime"]
        session = await self.repo.get_session(call["session_id"], owner_actor_id=owner)
        task, attempt, _ = await self.scope(owner, session, runtime, lock=True)
        validate_model_call(task, attempt, call)
        if (
            runtime.get("status") != "applied"
            or runtime.get("aborted")
            or runtime.get("token_hash") != call["detail"].get("runtime")
            or selected["revision"] != call["detail"].get("selection_revision")
            or runtime.get("selection_revision") != selected["revision"]
            or datetime.fromisoformat(
                runtime.get("expires_at", "1970-01-01T00:00:00+00:00")
            )
            <= datetime.now(UTC)
        ):
            raise CapabilityError("The model probe scope changed before approval")
        raw = call["detail"]["invocation"]
        invocation = await verify_tool_invocation(
            self.repo,
            owner=owner,
            sandbox=str(session["sandbox_id"]),
            server=PROBE_SERVER,
            name=PROBE_TOOL,
            arguments=call["detail"]["arguments"],
            meta={
                "threadId": raw["thread_id"],
                "callId": raw["call_id"],
                "x-codex-turn-metadata": {
                    "thread_id": raw["thread_id"],
                    "turn_id": raw["turn_id"],
                },
            },
            session_id=call["session_id"],
            observe_seconds=0,
        )
        if runtime.get("turn_id") != invocation.turn_id:
            raise CapabilityError("The model probe turn changed before approval")
        return {"invocation": invocation, "selected": selected}
