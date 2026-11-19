"""Owner-selected fields from an accepted step of a private method trial."""

import json
from typing import Annotated
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field

from app.application.workbench.artifact_read_authority import (
    EVENT,
    check,
    planned_read,
)
from app.application.workbench.method_calls import root_call
from app.domain.workbench.capabilities import CapabilityError, digest
from app.domain.workbench.errors import WorkbenchError
from app.domain.workbench.formal_execution import (
    expanded_plan,
    recursive_plan,
    snapshot_for_task,
)
from app.domain.workbench.formal_method_execution_generated import bounded_json
from app.domain.workbench.formal_recursive import node_artifact
from app.domain.workbench.states import AttemptState, TaskState

TOOL = "read_task_output"


class OutputRef(BaseModel):
    model_config = ConfigDict(extra="forbid")
    task_id: UUID
    name: Annotated[str, Field(min_length=1, max_length=200)]
    version: Annotated[int, Field(strict=True, ge=1)]
    output: Annotated[str, Field(min_length=1, max_length=128)]


def descriptor():
    return {
        "name": TOOL,
        "description": "Read one owner-approved output field from an accepted private method trial step.",
        "inputSchema": OutputRef.model_json_schema(),
    }


async def selected_output(repo, owner, arguments):
    try:
        ref = OutputRef.model_validate(arguments)
        task = await repo.get_task(str(ref.task_id), owner_actor_id=owner)
        snapshot = snapshot_for_task(task)
        if not snapshot.dependencies or task["state"] != TaskState.SUCCEEDED:
            raise ValueError
        artifact = await repo.get_artifact(
            task_id=str(ref.task_id), name=ref.name, version=ref.version
        )
        if not artifact or artifact["kind"] != "step" or not artifact["attempt_id"]:
            raise ValueError
        attempt = await repo.get_attempt(str(artifact["attempt_id"]))
        refs = attempt["refs"]
        index = refs["step_index"]
        if type(index) is not int or index < 0:
            raise ValueError
        node = expanded_plan(snapshot).leaf_nodes[index]
        expected_name = (
            node_artifact(node.path)
            if recursive_plan(snapshot)
            else "formal-step:" + node.step.id
        )
        receipt = refs["formal_result"]
        pinned = {k: artifact[k] for k in ("name", "version", "digest")}
        pinned["id"] = str(artifact["id"])
        if (
            str(attempt["task_id"]) != str(ref.task_id)
            or attempt["status"] != AttemptState.COMPLETED
            or refs.get("kind") != "formal_orchestration_step"
            or refs.get("reference") != node.reference.model_dump(mode="json")
            or refs["snapshot_digest"] != task["pack_digest"]
            or artifact["version"] != ref.version
            or expected_name != ref.name
            or attempt["output_artifacts"] != [pinned]
            or receipt["outcome"] != "succeeded"
            or receipt["outputs"] != artifact["content"]
            or digest(artifact["content"]) != receipt["outputs_digest"]
            or ref.output not in node.step.outputs
        ):
            raise ValueError
        value = artifact["content"][ref.output]
        bounded_json(value)
        return {
            "task_id": str(ref.task_id),
            "artifact_id": pinned["id"],
            "name": ref.name,
            "version": ref.version,
            "digest": pinned["digest"],
            "producer_path": list(node.path),
            "output": ref.output,
        }, value
    except (
        IndexError,
        KeyError,
        TypeError,
        ValueError,
        WorkbenchError,
    ) as exc:
        raise CapabilityError("Selected private task output is unavailable") from exc


async def read_binding(
    repo, task, snapshot, arguments, transfer, *, child=None, attempt=None, result=None
):
    ref, value = await selected_output(repo, str(task["owner_actor_id"]), arguments)
    if result is not None:
        payload = {"source": ref, "value": value}
        expected_digest = digest(payload)
        content = result.get("content")
        if (
            result.get("isError") is not False
            or digest(result.get("structuredContent")) != expected_digest
            or not isinstance(content, list)
            or len(content) != 1
            or not isinstance(content[0], dict)
            or content[0].get("type") != "text"
            or digest(json.loads(content[0]["text"])) != expected_digest
        ):
            raise ValueError("artifact_read_result_mismatch")
    if child is not None:
        binding = await planned_read(
            repo,
            task,
            snapshot,
            child,
            attempt,
            arguments,
            transfer,
            include_empty=True,
        )
        assert binding is not None
        binding["fields"]["value"] = ref
        return {**binding, "return_to": snapshot.caller}
    root = await root_call(repo, task)
    return {
        "task_id": str(task["id"]),
        "owner_actor_id": str(task["owner_actor_id"]),
        "path": [
            {
                "call_id": str(root["id"]),
                "method_id": str(snapshot.method_id),
                "version": None,
                "definition_digest": snapshot.definition_digest,
            }
        ],
        "fields": {"value": ref},
        "purpose": transfer["purpose"],
        "destination": transfer["destination"],
        "return_to": snapshot.caller,
    }


async def read_approved(repo, owner, call, intent):
    if (
        intent.get("execution_kind") not in ("formal_mcp", "formal_probe")
        or intent["tool"]["name"] != TOOL
    ):
        raise CapabilityError("Task outputs require a verified method invocation")
    async with repo.transaction():
        row = await repo.capability_call(owner, str(call["id"]), lock=True)
        interaction = await repo.get_interaction(str(row["interaction_id"]))
        state = await repo.latest_call_event(row, EVENT)
        displayed = interaction["prompt"]["subject"].get("artifact_read")
        if (
            row["status"] != "dispatching"
            or interaction["status"] != "consumed"
            or not state
            or not displayed
            or digest(displayed) != intent.get("artifact_read_digest")
        ):
            raise CapabilityError("Task output read lacks current owner consent")
        try:
            check(state, row, displayed, interaction["expires_at"])
        except WorkbenchError as exc:
            raise CapabilityError("Task output consent is no longer valid") from exc
        ref, value = await selected_output(repo, owner, intent["arguments"])
        if displayed["fields"].get("value") != ref:
            raise CapabilityError("Selected task output changed")
    payload = {"value": value, "source": ref}
    try:
        bounded_json(payload)
    except ValueError as exc:
        raise CapabilityError(
            "Selected task output exceeds the method result limit"
        ) from exc
    return {
        "structuredContent": payload,
        "content": [{"type": "text", "text": json.dumps(payload, ensure_ascii=False)}],
        "isError": False,
    }
