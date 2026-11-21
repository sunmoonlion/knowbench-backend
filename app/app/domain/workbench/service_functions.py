"""Versioned descriptions of selectable service operations."""

import json
from typing import Annotated, Any, Literal

from pydantic import Field, field_validator, model_validator

from app.domain.workbench.capabilities import CapabilityError, Strict, digest
from app.domain.workbench.tool_schema import schema_validator

IDENTIFIER = r"^[a-zA-Z][a-zA-Z0-9_.-]{0,95}$"
Identifier = Annotated[str, Field(pattern=IDENTIFIER)]
Scene = Literal["native", "incubation", "planning", "step"]


class ServiceFunction(Strict):
    description: str = Field(min_length=1, max_length=1024, pattern=r"\S")
    # Provider-owned logical function. The provider selects its internal implementation.
    operation: Identifier
    scenes: list[Scene] = Field(min_length=1, max_length=4)
    required_permissions: list[Identifier] = Field(max_length=32)
    input_schema: dict[str, Any]
    output_schema: dict[str, Any]

    @field_validator("scenes", "required_permissions")
    @classmethod
    def canonical_set(cls, values):
        if len(set(values)) != len(values):
            raise ValueError("Duplicate entry")
        return sorted(values)

    @model_validator(mode="after")
    def validate_schemas(self):
        for schema in (self.input_schema, self.output_schema):
            try:
                schema_validator(schema)
            except CapabilityError as exc:
                raise ValueError(str(exc)) from exc
        encoded = json.dumps(self.model_dump(), ensure_ascii=False, allow_nan=False)
        if len(encoded.encode()) > 65536:
            raise ValueError("Function contract exceeds 64 KiB")
        # JSONB cannot store embedded NUL characters, including in schema defaults.
        fields = [self.description, self.input_schema, self.output_schema]
        while fields:
            value = fields.pop()
            if isinstance(value, str) and "\x00" in value:
                raise ValueError("Function contract contains NUL")
            if isinstance(value, dict):
                fields.extend(value.keys())
                fields.extend(value.values())
            elif isinstance(value, list):
                fields.extend(value)
        return self


class FunctionRevision(Strict):
    provider: str
    name: Identifier
    version: int
    digest: str
    contract: ServiceFunction

    @classmethod
    def build(cls, provider: str, name: str, version: int, contract: ServiceFunction):
        identity = {"provider": provider, "name": name, "version": version}
        fingerprint = digest({**identity, "contract": contract.model_dump()})
        return cls(
            provider=provider,
            name=name,
            version=version,
            contract=contract,
            digest="sha256:" + fingerprint,
        )
