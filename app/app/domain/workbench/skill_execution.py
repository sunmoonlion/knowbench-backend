"""Installed package entrypoints and public execution receipts."""

from copy import deepcopy
from pathlib import PurePosixPath

from app.domain.workbench.script_contract_generated import ScriptArguments


def script_tool(asset: dict) -> dict | None:
    paths = [
        f["path"]
        for f in asset["detail"].get("files", [])
        if PurePosixPath(f["path"]).suffix in (".py", ".sh", ".js", ".mjs", ".cjs")
    ]
    if not paths:
        return None
    schema = ScriptArguments.model_json_schema()
    schema["properties"]["path"]["enum"] = paths
    return {
        "name": "skill_run",
        "description": f"Run an installed script from {asset['name']} in an isolated cloud container. "
        "Python 3, Bash and Node.js are available; network, credentials and local project files are not. "
        "The working directory is the package root. Pass small input via stdin or args; write artifacts under outputs/. "
        "Execution is limited to 30 seconds, 256 MiB and 512 KiB of returned files. Do not assume extra dependencies are installed.",
        "inputSchema": schema,
    }


def execution_input(asset: dict, arguments: dict) -> dict:
    detail = asset["detail"]
    return {
        **ScriptArguments.model_validate(arguments).model_dump(),
        "files": [
            {
                "path": f["path"],
                "sha256": f["sha256"],
                "data_base64": detail["resources"][f["path"]],
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
    copy = deepcopy(call)
    copy["detail"].pop("invocation", None)  # Host identity is internal evidence.
    copy["detail"].pop("operation", None)  # Reviewed policy snapshot is internal.
    copy["detail"].pop("access", None)  # Frozen method permission is internal evidence.
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
    return copy
