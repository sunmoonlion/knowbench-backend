"""Resolve exact, approved inputs outside the script container."""

import base64
import hashlib
import json

from app.application.workbench.data_function import DataFunction
from app.application.workbench.data_relay import forward_arguments
from app.application.workbench.records import ProjectRecords
from app.domain.workbench.capabilities import CapabilityError
from app.domain.workbench.script_contract_generated import (
    MAX_INPUT_BYTES,
    MaterialInput,
    ScriptArguments,
)
from app.domain.workbench.states import AttemptState, TaskState

FILE_LIMIT = 512 * 1024
# KnowBase read_input slices Python Unicode characters (currently 60,000/page).
# Allow pages down to 1 Ki characters at the full ASCII byte limit; always
# count the actual UTF-8 bytes and return only after the provider reports EOF.
MAX_MATERIAL_PAGES = FILE_LIMIT // 1024


async def material_text(read_page, ref, *, recheck=False):
    data = bytearray()
    offset = 0
    for _ in range(MAX_MATERIAL_PAGES):
        record = await read_page(
            {
                "material_id": str(ref.material_id),
                "version_no": ref.version_no,
                "what": "text",
                "offset": offset,
                "existing_only": recheck or offset > 0,
            }
        )
        source = record.get("source") or {}
        part = record.get("text")
        if (
            source.get("material_id") != str(ref.material_id)
            or source.get("version_no") != ref.version_no
            or source.get("reading") != ref.reading
            or record.get("offset") != offset
            or not isinstance(part, str)
            or type(record.get("more")) is not bool
            or (record["more"] and not part)
        ):
            raise CapabilityError(
                "The selected material version or text rendition is unavailable"
            )
        data.extend(part.encode())
        if len(data) > FILE_LIMIT:
            raise CapabilityError("Script input exceeds 512 KiB")
        if not record["more"]:
            return bytes(data)
        offset += len(part)
    raise CapabilityError(
        "Material reader exceeded the page limit before EOF; no partial input was delivered"
    )


async def step_field(repo, owner, call, ref):
    task_id = str(ref.task_id)
    task = await repo.get_task(task_id, owner_actor_id=owner)
    binding = call["detail"].get("method")
    if str(task["session_id"]) != str(call["session_id"]) or (
        (task_id != binding["task_id"] or task["state"] != TaskState.RUNNING)
        if binding
        else task["state"] != TaskState.SUCCEEDED
    ):
        raise CapabilityError("Step output is outside the approved execution")
    item = await repo.get_artifact(
        task_id=task_id, name=ref.artifact, version=ref.version
    )
    if not item or item["kind"] != "step" or not item["attempt_id"]:
        raise CapabilityError("Requested step output is unavailable")
    attempt = await repo.get_attempt(str(item["attempt_id"]))
    pinned = {
        "name": ref.artifact,
        "version": ref.version,
        "digest": item["digest"],
        "id": str(item["id"]),
    }

    def matches(entry):
        return (
            all(entry.get(k) == pinned[k] for k in ("name", "version", "id"))
            and entry.get("digest", pinned["digest"]) == pinned["digest"]
        )

    if binding:
        consumer = await repo.get_attempt(binding["attempt_id"])
        if str(consumer["task_id"]) != task_id or not any(
            entry.get("name") == ref.artifact
            and entry.get("version") == ref.version
            and entry.get("id", pinned["id"]) == pinned["id"]
            and entry.get("digest", pinned["digest"]) == pinned["digest"]
            for entry in consumer.get("input_artifact_versions") or []
        ):
            raise CapabilityError("Step output was not bound to the consuming attempt")
    if (
        attempt["status"] != AttemptState.COMPLETED
        or str(attempt["task_id"]) != task_id
        or (binding and str(attempt["id"]) == binding["attempt_id"])
        or not any(matches(entry) for entry in (attempt.get("output_artifacts") or []))
        or not isinstance(item["content"], dict)
        or ref.field not in item["content"]
    ):
        raise CapabilityError("Selected field is not an accepted step output")
    return json.dumps(
        item["content"][ref.field],
        ensure_ascii=False,
        sort_keys=True,
        allow_nan=False,
        separators=(",", ":"),
    ).encode()


async def resolve_inputs(repo, owner, call, source, *, recheck=False):
    refs = ScriptArguments.model_validate(call["detail"]["arguments"]).inputs
    if not refs:
        return []
    binding = call["detail"].get("method")
    scope = (
        {"task": binding["task_id"]}
        if binding
        else {"conversation": call["session_id"]}
    )
    records = ProjectRecords(repo, owner_actor_id=owner)
    reader = DataFunction(*source) if source else None

    async def read_page(arguments):
        if reader is None:
            raise CapabilityError("The authorized material reader is unavailable")
        task, forwarded = await forward_arguments(
            records, "read_input", {**scope, **arguments}
        )
        if binding and (
            task is None or str(task["active_attempt_id"]) != str(binding["attempt_id"])
        ):
            raise CapabilityError(
                "The consuming attempt changed before its material read"
            )
        parent = await repo.capability_call(owner, call["id"])
        if not parent or parent["status"] != "dispatching":
            raise CapabilityError("The script call is no longer active")
        return await reader.host_read(
            repo,
            "read_input",
            {"scope": "run", **forwarded},
            owner=owner,
            session_id=str(call["session_id"]),
            task=task,
            parent_call_id=call["id"],
        )

    result = []
    total = 0
    for ref in refs:
        raw = (
            await material_text(read_page, ref, recheck=recheck)
            if isinstance(ref, MaterialInput)
            else await step_field(repo, owner, call, ref)
        )
        total += len(raw)
        if len(raw) > FILE_LIMIT or total > MAX_INPUT_BYTES:
            raise CapabilityError("Script business inputs exceed the delivery limit")
        result.append(
            {
                "path": ref.name,
                "sha256": hashlib.sha256(raw).hexdigest(),
                "data_base64": base64.b64encode(raw).decode(),
            }
        )
    return result


def input_manifest(files):
    """Permanent receipt for the exact files made ready; file contents stay transient."""
    return [
        {
            "name": f["path"],
            "bytes": len(base64.b64decode(f["data_base64"])),
            "sha256": f["sha256"],
        }
        for f in files
    ]
