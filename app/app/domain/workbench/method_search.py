"""Bounded lexical recall for authorized catalogs. No term grants or excludes a method."""

import re
from itertools import zip_longest
from typing import Literal

from pydantic import Field, model_validator

from app.domain.workbench.formal_method_generated import (
    CapabilityDeclaration,
    MethodRef,
    Strict,
)


class IncubationQuery(Strict):
    goal: str | None = Field(default=None, min_length=1, max_length=2000, pattern=r"\S")
    declaration: CapabilityDeclaration | None = None
    references: list[MethodRef] | None = Field(default=None, max_length=100)


class DirectoryBrowse(Strict):
    kind: Literal["tool", "expert"] | None = None
    category: str | None = Field(default=None, max_length=64, pattern=r"^[a-z_.]+$")
    offset: int = Field(default=0, ge=0, le=2147483647)
    limit: int = Field(default=30, ge=1, le=100)
    question: str | None = Field(default=None, max_length=2000)


class DirectoryCategory(Strict):
    code: str
    group: str
    name: str
    group_name: str


class VersionCategory(Strict):
    reference: MethodRef
    kind: Literal["tool", "expert"]
    category: str


class DirectoryLabels(Strict):
    subject: str
    categories: dict[Literal["tool", "expert"], list[DirectoryCategory]]
    items: list[VersionCategory] = Field(max_length=100)


class PlanningQuery(Strict):
    stage: Literal["directory", "expert", "composition"]
    question: str | None = Field(
        default=None, min_length=1, max_length=2000, pattern=r"\S"
    )

    goal: str | None = Field(default=None, min_length=1, max_length=2000, pattern=r"\S")

    @model_validator(mode="after")
    def check_basis(self):
        if (self.stage == "directory") != (self.question is None):
            raise ValueError(
                "Expert and composition searches require the current question"
            )
        if self.goal and self.stage != "composition":
            raise ValueError("Only composition searches accept a subgoal")
        return self


def _words(text):
    # Preserve Latin terms and use overlapping character pairs in unsegmented Chinese.
    for word in re.findall(
        r"[\u3400-\u9fff]+|(?:(?![\u3400-\u9fff])[^\W_])+", text.lower()[:2000]
    ):
        if "\u3400" <= word[0] <= "\u9fff" and len(word) > 1:
            yield from (word[i : i + 2] for i in range(len(word) - 1))
        else:
            yield word[:64]


def search_terms(*fields):
    # Interleave title, purpose, applicability and deliverables so a long title
    # cannot consume the entire recall budget. Exclusions are never positive hits.
    terms = {}
    for group in zip_longest(*(_words(field) for field in fields)):
        for term in group:
            if term is not None:
                terms[term] = None
                if len(terms) == 32:
                    return list(terms)
    return list(terms)


def positive_fields(declaration):
    return (
        declaration.title,
        declaration.purpose,
        *declaration.promises.values(),
        *declaration.applies_when,
        *(item.when for item in declaration.conditions.values()),
    )
