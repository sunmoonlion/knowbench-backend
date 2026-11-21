"""Bounded lexical recall for authorized catalogs. No term grants or excludes a method."""

import re
from itertools import zip_longest
from typing import Literal

from pydantic import Field, model_validator

from app.domain.workbench.formal_method_generated import CapabilityDeclaration, Strict


class IncubationQuery(Strict):
    declaration: CapabilityDeclaration | None = None


class PlanningQuery(Strict):
    stage: Literal["directory", "expert", "composition"]
    question: str | None = Field(
        default=None, min_length=1, max_length=2000, pattern=r"\S"
    )

    @model_validator(mode="after")
    def check_basis(self):
        if (self.stage == "directory") != (self.question is None):
            raise ValueError(
                "Expert and composition searches require the current question"
            )
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
    )
