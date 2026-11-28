"""Installed package entrypoints and public execution receipts."""

import base64
import hashlib
import json
from copy import deepcopy
from pathlib import PurePosixPath

from app.domain.workbench.script_contract_generated import ScriptArguments, ScriptResult


def unique_output_object(pairs):
    obj = dict(pairs)
    if len(obj) != len(pairs):
        raise ValueError("Ambiguous duplicate output field")
    return obj


def is_script_path(path: str) -> bool:
    return PurePosixPath(path).suffix in (".py", ".sh", ".js", ".mjs", ".cjs")


def script_tool(asset: dict) -> dict | None:
    paths = [
        f["path"] for f in asset["detail"].get("files", []) if is_script_path(f["path"])
    ]
    if not paths:
        return None
    schema = ScriptArguments.model_json_schema()
    schema["properties"]["path"]["enum"] = paths
    return {
        "name": "skill_run",
        "description": f"Run an installed script from {asset['name']} in an isolated cloud container. "
        "Python 3, Bash and Node.js are available; network, credentials and local project files are not. "
        "The working directory is the package root. Named inputs are files in SUNMOON_INPUT_DIR. Choose a material text rendition or an accepted step output field from this conversation; step fields are JSON files. Each file is at most 512 KiB, all business inputs at most 2 MiB. Write artifacts under outputs/. "
        "Execution is limited to 30 seconds, 256 MiB and 512 KiB of returned files. Do not assume extra dependencies are installed.",
        "inputSchema": schema,
    }


def execution_input(asset: dict, arguments: dict, resources: dict[str, bytes]) -> dict:
    detail = asset["detail"]
    return {
        **ScriptArguments.model_validate(arguments).model_dump(exclude={"inputs"}),
        "files": [
            {
                "path": f["path"],
                "sha256": f["sha256"],
                "data_base64": base64.b64encode(resources[f["path"]]).decode("ascii"),
            }
            for f in detail["files"]
        ],
    }


def public_result(result, call_id: str):
    result = deepcopy(result)
    if isinstance(result, dict):
        for index, artifact in enumerate(result.get("artifacts", [])):
            artifact.pop("data_base64", None)
            artifact["download_url"] = (
                f"/api/workbench/capability-calls/{call_id}/artifacts/{index}"
            )
    return result


def public_call(call: dict) -> dict:
    from app.domain.workbench.call_approval import approval_preview

    copy = deepcopy(call)
    preview = approval_preview(call)
    if preview:
        copy["detail"]["approval"] = preview
    for key in (
        "read_scope_key",
        "read_scope_until",
        "read_revoked_at",
        "origin_read_revoked_at",
    ):
        copy.pop(key, None)
    for key in (
        "script_cleanup_pending",
        "read_offer_key",
        "read_offer_until",
        "read_approval_id",
        "read_approval_until",
    ):
        copy["detail"].pop(key, None)
    copy["detail"].pop("function_arguments", None)
    copy["detail"].pop("invocation", None)  # Host identity is internal evidence.
    copy["detail"].pop("operation", None)  # Reviewed policy snapshot is internal.
    copy["detail"].pop("access", None)  # Frozen method permission is internal evidence.
    copy["detail"].pop(
        "formal_host", None
    )  # Persisted host context, never model provenance.
    copy["detail"].pop(
        "artifact_read_digest", None
    )  # Resource scope is shown on its approval.
    copy["detail"].pop("authorization", None)  # Exact-action permission evidence.
    copy["detail"].pop("formal_method", None)  # Private frozen method provenance.
    copy["detail"].pop("formal_probe", None)  # Verified candidate model provenance.
    for field in (
        "kind",
        "origin",
        "call_key",
        "parent_call_id",
        "task_id",
        "attempt_id",
        "registration_id",
        "registration_digest",
    ):
        copy.pop(field, None)
    copy["detail"]["source_asset_id"] = copy["source_asset_id"]
    copy["detail"]["connection_id"] = copy["connection_id"]
    copy["detail"]["result"] = public_result(
        copy["detail"].get("result"), str(copy["id"])
    )
    if copy["detail"].get("execution_kind") == "formal_instruction":
        quote = copy["detail"]["instruction_quote"]
        copy["detail"]["instruction_quote"] = {
            **{key: quote[key] for key in ("provider", "model", "currency", "maximum")},
            "destination": quote["base_url"] + "/responses",
        }
    if copy["detail"].get("execution_kind") == "registry_script":
        detail = copy["detail"]
        source = detail["source"]
        detail.update(
            revision=source["version"],
            connection_name=source["name"],
            account_label="",
            purpose="registry-source",
            arguments={"approval_digest": detail["approval_digest"]},
            tool={
                "name": source["name"],
                "description": "",
                "inputSchema": {"type": "object"},
            },
            approval_token="",  # Confirmation was bound to the task, not a pending tool interaction.
        )
    return copy


def method_script_input(asset, draft, arguments, resources, *, bound_source=None):
    from app.domain.workbench.formal_execution import direct_procedure, validate_values
    from app.domain.workbench.formal_method_execution_generated import bounded_json
    from app.domain.workbench.skill_package import skill_manifest_digest

    procedure = direct_procedure(draft)
    source = bound_source or draft.source
    if (
        source is None
        or source.kind != "skill_script"
        or (
            asset["kind"] != "skill"
            or asset["status"] != "active"
            or str(asset["id"]) != str(source.asset_id)
            or asset["revision"] != source.asset_revision
            or skill_manifest_digest(asset["detail"]["files"]) != source.digest
        )
    ):
        raise ValueError("The installed script package changed")
    bounded_json(arguments)
    validate_values(procedure.inputs, arguments)
    raw = json.dumps(
        arguments,
        sort_keys=True,
        ensure_ascii=False,
        allow_nan=False,
        separators=(",", ":"),
    ).encode("utf-8")
    payload = execution_input(asset, {"path": source.entry}, resources)
    payload["inputs"] = [
        {
            "path": "arguments.json",
            "data_base64": base64.b64encode(raw).decode("ascii"),
            "sha256": hashlib.sha256(raw).hexdigest(),
        }
    ]
    return payload


def method_script_outputs(receipt: dict) -> dict:
    """Only the installed script's complete result file supplies typed outputs."""
    from app.domain.workbench.formal_method_execution_generated import bounded_json

    result = ScriptResult.model_validate(
        {k: v for k, v in receipt.items() if k != "isError"}
    )
    artifacts = [item for item in result.artifacts if item.path == "result.json"]
    if (
        result.exit_code != 0
        or result.timed_out
        or result.artifact_error
        or len(artifacts) != 1
    ):
        raise ValueError(
            "A successful script must return exactly one complete result.json"
        )
    raw = base64.b64decode(artifacts[0].data_base64, validate=True)
    if len(raw) > 64000:
        raise ValueError("Script result exceeds the formal output limit")

    try:
        value = json.loads(raw.decode("utf-8"), object_pairs_hook=unique_output_object)
        bounded_json(value)
    except RecursionError as exc:
        raise ValueError("Script output exceeds the JSON nesting limit") from exc
    if not isinstance(value, dict):
        raise ValueError("Formal script outputs must be a JSON object")
    return value
