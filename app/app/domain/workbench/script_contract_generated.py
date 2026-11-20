# Generated from sandbox-platform/provisioner/script_contract.py; do not edit.
"""Provider-owned v1 contract for isolated, credential-free package execution."""

from __future__ import annotations

import base64
import hashlib
from pathlib import PurePosixPath
from typing import Annotated, Literal
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field, model_validator


class Strict(BaseModel):
    model_config = ConfigDict(extra="forbid")


def package_path(value: str) -> str:
    if (
        not value
        or len(value) > 500
        or "\\" in value
        or ":" in value
        or any(ord(c) < 32 or ord(c) == 127 for c in value)
        or any(p in ("", ".", "..") for p in value.split("/"))
    ):
        raise ValueError("Expected a package-relative file path")
    return value


class ScriptFile(Strict):
    path: str
    data_base64: str = Field(max_length=699052, repr=False)
    sha256: str = Field(pattern=r"^[a-f0-9]{64}$")

    @model_validator(mode="after")
    def valid_file(self):
        package_path(self.path)
        raw = base64.b64decode(self.data_base64, validate=True)
        if len(raw) > 512 * 1024 or hashlib.sha256(raw).hexdigest() != self.sha256:
            raise ValueError("Invalid file size or digest")
        return self


InputName = Annotated[str, Field(pattern=r"^[A-Za-z0-9][A-Za-z0-9._-]{0,79}$")]
MAX_INPUT_BYTES = 2 * 1024 * 1024
MAX_FRAME_BYTES = 10 * 1024 * 1024


class MaterialInput(Strict):
    kind: Literal["material"]
    name: InputName
    material_id: UUID
    version_no: int = Field(ge=1, strict=True)
    reading: str = Field(min_length=1, max_length=100)


class StepInput(Strict):
    kind: Literal["step_output"]
    name: InputName
    task_id: UUID
    artifact: str = Field(min_length=1, max_length=200)
    version: int = Field(ge=1, strict=True)
    field: str = Field(min_length=1, max_length=128)


class ScriptCommand(Strict):
    path: str = Field(min_length=1, max_length=500)
    args: list[str] = Field(default_factory=list, max_length=64)
    stdin: str = Field(default="", max_length=32000, repr=False)

    @model_validator(mode="after")
    def valid_arguments(self):
        package_path(self.path)
        if PurePosixPath(self.path).suffix not in (".py", ".sh", ".js", ".mjs", ".cjs"):
            raise ValueError("Supported scripts: Python, Shell and Node.js")
        if any("\x00" in a or len(a) > 2000 for a in self.args):
            raise ValueError("Invalid script argument")
        return self


class ScriptArguments(ScriptCommand):
    inputs: list[MaterialInput | StepInput] = Field(default_factory=list, max_length=8)

    @model_validator(mode="after")
    def unique_inputs(self):
        if len({ref.name for ref in self.inputs}) != len(self.inputs):
            raise ValueError("Duplicate script input name")
        return self


class InputFile(ScriptFile):
    path: InputName


class ScriptFrame(Strict):
    sha256: str = Field(pattern=r"^[a-f0-9]{64}$")
    size: int = Field(ge=1, le=MAX_FRAME_BYTES, strict=True)


class ScriptExecution(ScriptCommand):
    inputs: list[InputFile] = Field(default_factory=list, max_length=8)
    files: list[ScriptFile] = Field(min_length=1, max_length=256)

    @model_validator(mode="after")
    def complete_package(self):
        if (
            len({f.path for f in self.inputs}) != len(self.inputs)
            or sum(len(base64.b64decode(f.data_base64)) for f in self.inputs)
            > MAX_INPUT_BYTES
        ):
            raise ValueError("Duplicate input name or business inputs exceed 2 MiB")
        paths = {f.path for f in self.files}
        if (
            len(paths) != len(self.files)
            or self.path not in paths
            or "SKILL.md" not in paths
        ):
            raise ValueError("Duplicate paths or incomplete Skill package")
        if (
            sum(len(base64.b64decode(f.data_base64)) for f in self.files)
            > 4 * 1024 * 1024
        ):
            raise ValueError("Package exceeds 4 MiB")
        for path in paths:
            if any(str(parent) in paths for parent in PurePosixPath(path).parents):
                raise ValueError("A package file cannot also be a directory")
        return self


def execution_frame(body: ScriptExecution) -> tuple[ScriptFrame, bytes]:
    raw = body.model_dump_json().encode()
    return ScriptFrame(sha256=hashlib.sha256(raw).hexdigest(), size=len(raw)), raw


class ScriptArtifact(ScriptFile):
    pass


class ScriptResult(Strict):
    exit_code: int | None
    stdout: str = Field(max_length=65536)
    stderr: str = Field(max_length=65536)
    timed_out: bool
    output_truncated: bool
    artifacts: list[ScriptArtifact] = Field(default_factory=list, max_length=32)
    artifact_error: str | None = None


class ScriptStatus(Strict):
    state: Literal["running", "finished", "failed", "absent"]
    result: ScriptResult | None = None
    error: str | None = None

    @model_validator(mode="after")
    def has_result(self):
        if self.state == "finished" and self.result is None:
            raise ValueError("Finished execution requires its result")
        return self


class ScriptStopped(Strict):
    stopped: Literal[True]
