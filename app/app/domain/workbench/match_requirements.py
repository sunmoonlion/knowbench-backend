"""Requirements and canonical digests used in formal selection receipts."""

from __future__ import annotations

import hashlib
import json
from typing import Annotated, Any, Literal, Self

from pydantic import Field, model_validator

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
