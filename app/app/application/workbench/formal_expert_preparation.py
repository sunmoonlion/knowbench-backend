"""Editable bindings on a selected ordinary expert match. No grants or calls are issued."""

from decimal import Decimal
from typing import Annotated
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field

from app.application.workbench.formal_expert_intake import (
    REVISION,
    SELECTION,
    read_verified,
    selected_definition,
)
from app.application.workbench.method_access import check_requirement
from app.domain.workbench.capabilities import CapabilityError
from app.domain.workbench.errors import NotFound, StaleStateVersion
from app.domain.workbench.expert_match import digest
from app.domain.workbench.tool_registry import ToolRegistration, connection_source

PREPARATION = "task/formal_expert_preparation"
Digest = Annotated[str, Field(pattern=r"^sha256:[a-f0-9]{64}$")]


class Strict(BaseModel):
    model_config = ConfigDict(extra="forbid")


class MaterialPin(Strict):
    material_id: UUID
    version_no: int = Field(strict=True, gt=0)
    sha256: str = Field(pattern=r"^[a-f0-9]{64}$")


class AccountPin(Strict):
    asset_id: UUID
    revision: int = Field(strict=True, gt=0)
    policies_digest: Digest


class Preparation(Strict):
    review_digest: Digest
    expected_revision: int = Field(strict=True, ge=0)
    materials: dict[str, MaterialPin] = Field(max_length=64)
    accounts: dict[str, AccountPin] = Field(max_length=64)
    # Explicit null carries the user's existing uncapped-budget choice.
    budget_limit: Annotated[Decimal, Field(gt=0, le=100000, decimal_places=2)] | None

    def bindings(self):
        return {
            "materials": {
                k: v.model_dump(mode="json") for k, v in self.materials.items()
            },
            "accounts": {
                k: v.model_dump(mode="json") for k, v in self.accounts.items()
            },
            "budget": {
                "currency": "CNY",
                "limit": format(self.budget_limit, ".2f")
                if self.budget_limit is not None
                else None,
            },
        }


def connection_options(requirements, assets, operations):
    result = {r.requirement_id: [] for r in requirements}
    for asset in assets:
        if asset["kind"] != "connection" or asset["status"] != "active":
            continue
        detail = asset["detail"]
        policies = []
        for tool in detail.get("tools", []):
            try:
                policy = operations.require(
                    ToolRegistration.discover(
                        connection_source(asset), detail["url"], tool
                    )
                )
            except CapabilityError:
                continue
            policies.append(policy)
        for req in requirements:
            accepted = {}
            for policy in policies:
                try:
                    check_requirement(req, policy)
                except CapabilityError:
                    continue
                accepted.setdefault(policy.operation, []).append(policy.policy_digest)
            if accepted and set(accepted) == set(req.operations):
                result[req.requirement_id].append(
                    {
                        "pin": {
                            "asset_id": str(asset["id"]),
                            "revision": asset["revision"],
                            "policies_digest": digest(
                                {op: sorted(ds) for op, ds in accepted.items()}
                            ),
                        },
                        "name": asset["name"],
                        "account_label": detail.get("account_label") or "",
                    }
                )
    return result


async def check_materials(library, token, materials):
    requested = {}
    for pin in materials.values():
        requested.setdefault(pin["material_id"], set()).add(pin["version_no"])
    versions = {}
    names = {}
    for mid, numbers in requested.items():
        name, versions[mid] = await library.preparation_material(token, mid, numbers)
        if name is not None:
            names[mid] = name
    unavailable = [
        slot
        for slot, pin in materials.items()
        if (pin["version_no"], pin["sha256"]) not in versions[pin["material_id"]]
    ]
    return names, unavailable


async def prepare(service, owner, task_id, library, token, *, browse=False, body=None):
    repo = service.repo
    async with repo.transaction():
        task = await repo.get_task(task_id, owner_actor_id=owner)
        if task["profile_id"] != "FIND_EXPERT" or str(task["profile_version"]) != "5":
            raise NotFound("No such formal match")
        selection = await repo.list_task_events(task_id, types=(SELECTION,))
        if (
            task["state"] != "SUCCEEDED"
            or not selection
            or await repo.list_task_events(task_id, types=(REVISION,))
        ):
            raise StaleStateVersion("Select an expert in the current question first")
        artifact = await repo.get_artifact(task_id=task_id, name="find_expert")
        review = await read_verified(repo, task, artifact)
        saved = await repo.latest_task_payload(task_id, PREPARATION)
    registry = service.formal_registry
    selected, definition, _ = await selected_definition(
        registry, owner, review, selection
    )
    inputs = definition.interfaces()["inputs"]
    if body:
        if (
            body.review_digest != review["review_digest"]
            or not body.materials.keys() <= inputs.keys()
        ):
            raise StaleStateVersion(
                "Preparation differs from the reviewed question or inputs"
            )
        bindings = body.bindings()
    else:
        bindings = (
            saved["bindings"]
            if saved
            else {"materials": {}, "accounts": {}, "budget": None}
        )
    materials = bindings["materials"]
    user_token = await token() if materials or browse else None
    shelf = await library.preparation_shelf(user_token) if browse else []
    names, invalid_materials = await check_materials(library, user_token, materials)
    async with repo.transaction():
        await repo.get_task(task_id, owner_actor_id=owner, for_update=True)
        if await repo.list_task_events(task_id, types=(REVISION,)):
            raise StaleStateVersion("The question has been revised")
        current = await repo.latest_task_payload(task_id, PREPARATION)
        revision = current["revision"] if current else 0
        # Lock only assets the user is saving. Other owners' installations never enter the choices.
        if body:
            for asset_id in sorted({str(p.asset_id) for p in body.accounts.values()}):
                await repo.capability_asset(owner, asset_id, lock=True)
        options = connection_options(
            definition.access_requirements,
            await repo.capability_assets(owner),
            registry.methods.operations,
        )
        invalid_accounts = [
            k
            for k, p in bindings["accounts"].items()
            if not any(o["pin"] == p for o in options.get(k, []))
        ]
        if body:
            if invalid_materials or invalid_accounts:
                raise StaleStateVersion(
                    "A material version or execution account is unavailable",
                    materials=invalid_materials,
                    accounts=invalid_accounts,
                )
            same = current is not None and current["bindings"] == bindings
            if body.expected_revision != revision and not (
                same and body.expected_revision == revision - 1
            ):
                raise StaleStateVersion("Preparation was edited in another window")
            if not same:
                revision += 1
                await repo.append_event(
                    session_id=str(task["session_id"]),
                    task_id=task_id,
                    kind="task",
                    event_type=PREPARATION,
                    payload={
                        "revision": revision,
                        "review_digest": review["review_digest"],
                        "reference": selected,
                        "bindings": bindings,
                    },
                )
        elif saved != current:
            raise StaleStateVersion(
                "Preparation changed while its bindings were being checked"
            )
    return {
        "revision": revision,
        "bindings": bindings,
        "account_options": options,
        "materials": shelf,
        "material_names": names,
        "invalid_materials": invalid_materials,
        "invalid_accounts": invalid_accounts,
    }
