"""Owner-approved, immutable workbench tasks for standalone source revisions."""

import base64
import hashlib
import json
from decimal import Decimal
from typing import Any, Literal
from uuid import UUID

from pydantic import Field, field_validator

from app.domain.workbench.capabilities import CapabilityError, digest
from app.domain.workbench.formal_method_execution_generated import bounded_json
from app.domain.workbench.formal_method_generated import Digest, Strict
from app.domain.workbench.registry_sources import RegisteredSource
from app.domain.workbench.script_contract_generated import ScriptExecution

PACK_SOURCE = "registry-source"


class SourceTaskInput(Strict):
    source_id: UUID
    version: int = Field(ge=1, le=2147483647, strict=True)
    digest: Digest
    arguments: dict[str, Any]
    budget_limit: Decimal = Field(gt=0, max_digits=12, decimal_places=6)

    @field_validator("arguments")
    @classmethod
    def bounded_arguments(cls, value):
        bounded_json(value)
        return value


class SourceTaskConfirmation(SourceTaskInput):
    approval_digest: str = Field(pattern=r"^[a-f0-9]{64}$")
    idempotency_key: str = Field(min_length=8, max_length=128)


class SourceTask(Strict):
    runtime_contract: Literal["knowbench/registry-source-task/v1"] = (
        "knowbench/registry-source-task/v1"
    )
    source: RegisteredSource
    arguments: dict[str, Any]
    budget_limit: Decimal
    quote: dict[str, Any]
    owner: UUID
    session_id: UUID

    @property
    def version(self):
        return str(self.source.version)

    def task_columns(self):
        document = self.model_dump(mode="json")
        return {
            "pack_source": PACK_SOURCE,
            "pack_snapshot": document,
            "pack_digest": digest(document),
        }

    @classmethod
    def load(cls, task):
        if (
            task["pack_source"] != PACK_SOURCE
            or digest(task["pack_snapshot"]) != task["pack_digest"]
        ):
            raise CapabilityError("Registered source task snapshot changed")
        snap = cls.model_validate(task["pack_snapshot"])
        if str(snap.owner) != str(task["owner_actor_id"]) or str(
            snap.session_id
        ) != str(task["session_id"]):
            raise CapabilityError("Source task belongs to another owner or session")
        return snap


def script_input(source, content, arguments):
    def file(path, raw):
        return {
            "path": path,
            "data_base64": base64.b64encode(raw).decode("ascii"),
            "sha256": hashlib.sha256(raw).hexdigest(),
        }

    values = json.dumps(
        arguments, ensure_ascii=False, sort_keys=True, allow_nan=False
    ).encode()
    return ScriptExecution.model_validate(
        {
            "path": source.name,
            "files": [file(source.name, content.encode())],
            "inputs": [file("arguments.json", values)],
        }
    ).model_dump(mode="json")


def instruction_input(content, arguments):
    return {
        "input": {"instructions": content, "arguments": arguments},
        "output_schema": {
            "type": "object",
            "properties": {"result": {"type": "string"}},
            "required": ["result"],
            "additionalProperties": False,
        },
    }
