"""Resolve a reviewer's planned identity and price without loading a key or calling a model."""

from dataclasses import dataclass
from typing import Literal

from pydantic import ValidationError

from app.domain.workbench.errors import StaleStateVersion
from app.domain.workbench.instruction_requests import InstructionRoute
from app.domain.workbench.pricing import PriceList
from app.domain.workbench.provider_url import validate_provider_base_url
from app.domain.workbench.review_routes import (
    ReviewGrant,
    ReviewRoute,
    ReviewRouteQuery,
    RouteKey,
    preview_routes,
)


@dataclass
class ReviewerChoices:
    routes: tuple[ReviewRoute, ...]
    grants: tuple[ReviewGrant, ...]
    tariffs: tuple[InstructionRoute, ...]
    prices: PriceList
    default_provider: str
    default_model: str
    default_url: str

    async def choices(
        self,
        repo,
        owner,
        requirement: Literal[
            "isolated_reviewer", "different_model_family"
        ] = "isolated_reviewer",
        *,
        sandbox=None,
    ):
        prefs = await repo.get_prefs(owner)
        preference = await repo.reviewer_preference(owner)
        try:
            default = RouteKey(provider=self.default_provider, model=self.default_model)
            producer = RouteKey(
                provider=default.provider, model=prefs.get("model") or default.model
            )
        except ValidationError:
            default = producer = None
        preview = preview_routes(
            ReviewRouteQuery(requirement=requirement, producer=producer),
            self.routes,
            default,
            self.prices,
        )
        grants = {
            (str(g.credential_id), g.provider, g.model, g.base_url) for g in self.grants
        }
        options = []
        credentials = await repo.list_credentials(owner)
        for route in preview.candidates:
            for cred in credentials:
                if cred["status"] != "active" or cred["provider"] != route.provider:
                    continue
                if (
                    sandbox
                    and cred["sandbox_id"]
                    and str(cred["sandbox_id"]) != str(sandbox)
                ):
                    continue
                url = cred["base_url"] or (
                    self.default_url if route.provider == self.default_provider else ""
                )
                try:
                    url = validate_provider_base_url(url)
                except ValueError:
                    continue
                pin = {
                    "credential_id": str(cred["id"]),
                    "provider": route.provider,
                    "model": route.model,
                }
                default_account = (route.provider, route.model, url) == (
                    self.default_provider,
                    self.default_model,
                    self.default_url.rstrip("/"),
                )
                if (
                    not default_account
                    and (pin["credential_id"], route.provider, route.model, url)
                    not in grants
                ):
                    continue
                tariff = next(
                    (
                        t
                        for t in self.tariffs
                        if (t.provider, t.model, t.base_url, t.price_key)
                        == (route.provider, route.model, url, route.price_key)
                    ),
                    None,
                )
                quote = tariff.quote(self.prices) if tariff else None
                options.append(
                    {
                        "pin": pin,
                        "family": route.family,
                        "destination": url + "/responses",
                        "price_key": route.price_key,
                        "price_as_of": preview.price_as_of,
                        "currency": preview.currency,
                        "quote": quote,
                    }
                )
        family = next(
            (
                r.family
                for r in self.routes
                if producer
                and (r.provider, r.model) == (producer.provider, producer.model)
            ),
            None,
        )
        return {
            "producer": {**producer.model_dump(), "family": family}
            if producer
            else None,
            "options": options,
            "preference": preference,
            "gap": preview.catalog_gap
            or (None if options else "connection_unavailable"),
        }

    async def save(self, repo, owner, choice):
        async with repo.transaction():
            view = await self.choices(repo, owner)
            pin = choice.model_dump(mode="json") if choice else None
            if pin and (
                not any(option["pin"] == pin for option in view["options"])
                or not await repo.lock_active_credential(owner, pin["credential_id"])
            ):
                raise StaleStateVersion(
                    "This review route is unavailable for this account"
                )
            await repo.save_reviewer_preference(owner, pin)
            return {**view, "preference": pin}
