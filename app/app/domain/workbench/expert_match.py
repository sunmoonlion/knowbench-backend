"""Question-grounded expert matching against public author declarations only.

These checks establish identity, completeness and provenance, not semantic
truth. Whether a statement really covers a question still needs match evals.
"""

from __future__ import annotations

import hashlib
import json
from typing import Annotated, Any, Literal, Self

from pydantic import Field, model_validator

from app.domain.workbench.method_catalog import PublicCapability
from app.domain.workbench.packs import Strict

Identifier = Annotated[str, Field(pattern=r"^[A-Za-z][A-Za-z0-9_-]{0,47}$")]
Text = Annotated[str, Field(min_length=1, max_length=2000)]
Digest = Annotated[str, Field(pattern=r"^sha256:[0-9a-f]{64}$")]


def digest(value: Any) -> str:
    encoded = json.dumps(
        value, ensure_ascii=False, sort_keys=True, separators=(",", ":")
    )
    return "sha256:" + hashlib.sha256(encoded.encode()).hexdigest()


class Requirement(Strict):
    id: Identifier
    kind: Literal["result", "constraint", "explicit_method"]
    statement: Text
    question_quote: Text
    required: bool = Field(strict=True)

    @model_validator(mode="after")
    def _nonblank(self) -> Self:
        if any(not value.strip() for value in (self.statement, self.question_quote)):
            raise ValueError(
                "requirements need nonblank statements and question quotes"
            )
        if self.kind == "explicit_method" and not self.required:
            raise ValueError("explicitly requested methods cannot become optional")
        return self


class DirectoryScope(Strict):
    """目录里公开的接什么、不接什么。照提供方 knowmart `expert-directory/v2`：三项都可不写。

    不复用专家包的 `Scope`（那里「接什么」必填）：调用方比提供方严，会拒掉合法的目录条目
    （SDD 0052 第一节逐字段比对查出）。
    """

    does: str = ""
    does_not: str = ""
    elsewhere: str = ""


class PublicExpert(Strict):
    knowbot_id: str = Field(min_length=1, max_length=64)
    version: str = Field(pattern=r"^[1-9][0-9]{0,5}$")
    digest: Digest
    name: str = Field(min_length=1, max_length=80)
    tagline: str = Field(max_length=200)
    scope: DirectoryScope
    declaration: Literal["scope/v1", "capabilities/v1"]
    runtime_contract: Literal[
        "knowbench/fixed/v1",
        "knowbench/dynamic-methods/v1",
        "knowbench/dynamic-methods/v2",
        "knowbench/dynamic-methods/v3",
    ]
    public_capabilities: tuple[PublicCapability, ...] = Field(max_length=30)
    free: bool = Field(strict=True)
    price: str | None = Field(pattern=r"^[0-9]+(\.[0-9]+)?$", max_length=32)

    @model_validator(mode="after")
    def _declaration(self) -> Self:
        ids = {cap.id for cap in self.public_capabilities}
        if len(ids) != len(self.public_capabilities):
            raise ValueError("duplicate public capability")
        if (self.declaration == "capabilities/v1") != bool(ids):
            raise ValueError("declaration format differs from its public capabilities")
        if self.runtime_contract.endswith("/v3") and not ids:
            raise ValueError("capability runtime requires public declarations")
        return self


class DirectorySnapshot(Strict):
    contract: Literal["knowmart/expert-directory/v2"]
    complete: bool = Field(strict=True)
    experts: tuple[PublicExpert, ...] = Field(max_length=1000)

    @model_validator(mode="after")
    def _unique(self) -> Self:
        keys = [(entry.knowbot_id, entry.version) for entry in self.experts]
        if len(set(keys)) != len(keys):
            raise ValueError("directory versions must be unique")
        return self
