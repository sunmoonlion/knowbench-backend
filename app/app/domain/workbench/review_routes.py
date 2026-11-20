"""Deployment-level route discovery. User grants and observed execution belong to the attempt."""

from typing import Annotated, Literal

from pydantic import BaseModel, ConfigDict, Field

from app.domain.workbench.pricing import PriceList

Identifier = Annotated[
    str, Field(min_length=1, max_length=128, pattern=r"^[A-Za-z0-9][A-Za-z0-9._:/-]*$")
]


class RouteKey(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)
    provider: Identifier
    model: Identifier


class ReviewRoute(RouteKey):
    family: (
        Annotated[str, Field(max_length=128, pattern=r"^[a-z0-9][a-z0-9._-]*$")] | None
    ) = None
    price_key: Identifier | None = None


class ReviewRouteQuery(BaseModel):
    model_config = ConfigDict(extra="forbid")
    requirement: Literal["isolated_reviewer", "different_model_family"]
    producer: RouteKey | None = None
    reviewer: RouteKey | None = None


class ReviewRoutePreview(BaseModel):
    contract: Literal["knowbench/review-route-preview/v1"] = (
        "knowbench/review-route-preview/v1"
    )
    producer: RouteKey | None
    candidates: list[ReviewRoute]
    price_as_of: str
    currency: str
    catalog_gap: (
        Literal[
            "producer_unknown",
            "reviewer_unknown",
            "family_unverified",
            "different_family_unavailable",
            "price_missing",
        ]
        | None
    )
    isolated_execution_available: Literal[False] = False


def preview_routes(
    query: ReviewRouteQuery,
    routes: tuple[ReviewRoute, ...],
    default: RouteKey | None,
    prices: PriceList,
) -> ReviewRoutePreview:
    catalog = {(r.provider, r.model): r for r in routes}
    if default:
        catalog.setdefault(
            (default.provider, default.model), ReviewRoute(**default.model_dump())
        )
    producer = query.producer or default
    reply = ReviewRoutePreview(
        producer=producer,
        candidates=[],
        catalog_gap=None,
        price_as_of=prices.as_of,
        currency=prices.currency,
    )
    primary = catalog.get((producer.provider, producer.model)) if producer else None
    if not primary:
        return reply.model_copy(update={"catalog_gap": "producer_unknown"})
    if query.reviewer:
        selected = catalog.get((query.reviewer.provider, query.reviewer.model))
        if not selected:
            return reply.model_copy(update={"catalog_gap": "reviewer_unknown"})
        options = [selected]
    else:
        options = [primary, *(route for route in catalog.values() if route != primary)]
    if query.requirement == "different_model_family":
        eligible = [
            r
            for r in options
            if primary.family and r.family and r.family != primary.family
        ]
        if not eligible:
            unknown = not primary.family or any(r.family is None for r in options)
            return reply.model_copy(
                update={
                    "catalog_gap": "family_unverified"
                    if unknown
                    else "different_family_unavailable"
                }
            )
        options = eligible
    providers_by_model: dict[str, set[str]] = {}
    for route in catalog.values():
        providers_by_model.setdefault(route.model, set()).add(route.provider)
    priced = [
        r.model_copy(update={"price_key": r.price_key or r.model})
        for r in options
        if (r.price_key or len(providers_by_model[r.model]) == 1)
        and prices.of(r.price_key or r.model) is not None
    ]
    return reply.model_copy(
        update={
            "candidates": priced,
            "catalog_gap": None if priced else "price_missing",
        }
    )
