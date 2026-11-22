"""Editable bindings on a selected ordinary expert match. No grants or calls are issued."""

from decimal import Decimal
from typing import Annotated
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field, JsonValue, field_validator

from app.application.workbench.formal_expert_intake import (
    ENDED,
    REVISION,
    SELECTION,
    read_verified,
    selected_definition,
)
from app.application.workbench.method_access import check_requirement
from app.domain.workbench.capabilities import CapabilityError
from app.domain.workbench.errors import NotFound, StaleStateVersion
from app.domain.workbench.expert_match import digest
from app.domain.workbench.ordinary_expert import CONFIRMED, MaterialPin
from app.domain.workbench.tool_registry import ToolRegistration, connection_source

PREPARATION = "task/formal_expert_preparation"
Digest = Annotated[str, Field(pattern=r"^sha256:[a-f0-9]{64}$")]


class Strict(BaseModel):
    model_config = ConfigDict(extra="forbid")


class ExecutionConfirmation(Strict):
    revision: int = Field(strict=True, gt=0)
    preview_digest: Digest


class AccountPin(Strict):
    asset_id: UUID
    revision: int = Field(strict=True, gt=0)
    policies_digest: Digest


class Preparation(Strict):
    review_digest: Digest
    expected_revision: int = Field(strict=True, ge=0)
    materials: dict[str, MaterialPin] = Field(max_length=64)
    accounts: dict[str, AccountPin] = Field(max_length=64)
    values: dict[str, JsonValue] = Field(default_factory=dict, max_length=100)

    @field_validator("values")
    @classmethod
    def bounded_values(cls, values):
        from app.domain.workbench.formal_method_execution_generated import bounded_json

        bounded_json(values)
        pending = [values]
        while pending:
            item = pending.pop()
            if isinstance(item, dict):
                pending.extend(item.keys())
                pending.extend(item.values())
            elif isinstance(item, list):
                pending.extend(item)
            elif isinstance(item, str) and chr(0) in item:
                raise ValueError("Method inputs cannot contain NUL")
        return values

    # Explicit null carries the user's existing uncapped-budget choice.
    budget_limit: Annotated[Decimal, Field(gt=0, le=100000, decimal_places=2)] | None

    def bindings(self):
        return {
            "values": self.values,
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


async def prepare(
    service,
    owner,
    task_id,
    library,
    token,
    *,
    browse=False,
    body=None,
    preview_revision=None,
    submission=None,
):
    from app.application.workbench.ordinary_expert_execution import (
        confirm_execution,
        execution_sources,
        replay,
    )
    from app.application.workbench.temporary_planning import PLAN
    from app.domain.workbench.formal_method_generated import FormalMethodDefinition

    repo = service.repo
    if submission:
        preview_revision = submission.revision
    async with repo.transaction():
        task = await repo.get_task(task_id, owner_actor_id=owner)
        if task["profile_id"] != "FIND_EXPERT" or str(task["profile_version"]) != "5":
            raise NotFound("No such formal match")
        receipt = await repo.latest_task_payload(task_id, CONFIRMED)
        if receipt and submission:
            return replay(receipt, submission)
        if receipt and body:
            raise StaleStateVersion(
                "This preparation already has a confirmed execution"
            )
        selection = await repo.list_task_events(task_id, types=(SELECTION,))
        temporary = await repo.latest_task_payload(task_id, PLAN)
        if (
            task["state"] != "SUCCEEDED"
            or not (selection or temporary)
            or await repo.latest_task_payload(task_id, ENDED)
            or await repo.list_task_events(task_id, types=(REVISION,))
        ):
            raise StaleStateVersion("Select an expert in the current question first")
        artifact = await repo.get_artifact(task_id=task_id, name="find_expert")
        review = await read_verified(repo, task, artifact)
        saved = await repo.latest_task_payload(task_id, PREPARATION)
    registry = service.formal_registry
    if temporary:
        from app.domain.workbench.planning_capabilities import capabilities

        if temporary["runtime"] != capabilities(service):
            raise StaleStateVersion(
                "Runner capabilities changed; rebuild this temporary sketch"
            )
        selected = temporary["reference"]
        definition = FormalMethodDefinition.model_validate(temporary["definition"])
        if (
            definition.digest() != selected["digest"]
            or temporary["approval"]["review_digest"] != review["review_digest"]
        ):
            raise StaleStateVersion("Temporary root differs from its reviewed problem")
    else:
        selected, definition, _ = await selected_definition(
            registry, owner, review, selection
        )
    inputs = definition.interfaces()["inputs"]
    from app.domain.workbench.formal_method_generated import MethodRef, ValueSlot

    slots = {
        name: ValueSlot.model_validate(item["slot"]) for name, item in inputs.items()
    }
    if body:
        if any(
            name not in slots or not slots[name].accepts(value)
            for name, value in body.values.items()
        ):
            raise StaleStateVersion("An input differs from its frozen slot contract")
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
    if "values" not in bindings:
        bindings = {**bindings, "values": {}}
    if preview_revision is not None:
        if not saved or saved["revision"] != preview_revision:
            raise StaleStateVersion("Save and review the current preparation first")
        from app.application.workbench.formal_expert_preview import fixed_graph

        graph, graph_issue = await fixed_graph(
            registry, owner, selected, definition, bindings["values"]
        )
    materials = bindings["materials"]
    user_token = await token() if materials or browse else None
    shelf = await library.preparation_shelf(user_token) if browse else []
    followup = task["original_input"]["input"].get("followup") or {}
    if (
        followup.get("kind") == "duty"
        and (slot := followup.get("material_slot"))
        and (body or saved)
    ):
        # KnowBase output-zone objects become executable inputs only after the
        # owner converts one to data. Verify the immutable conversion provenance.
        from app.application.workbench.expert_duties import check_released_output

        async with repo.transaction():
            subject = await repo.subject_of_actor(owner)
        await check_released_output(library, subject, followup["released"])
        selected_pin = materials.get(slot)
        if not selected_pin or not await library.is_converted_output(
            user_token, selected_pin, followup["released"]["material"]
        ):
            raise StaleStateVersion(
                "Choose the first data version converted from the released output"
            )
    names, invalid_materials = await check_materials(library, user_token, materials)
    async with repo.transaction():
        await repo.get_task(task_id, owner_actor_id=owner, for_update=True)
        if await repo.list_task_events(task_id, types=(REVISION, ENDED)):
            raise StaleStateVersion("The question has been revised")
        receipt = await repo.latest_task_payload(task_id, CONFIRMED)
        if receipt and submission:
            return replay(receipt, submission)
        if receipt and body:
            raise StaleStateVersion(
                "This preparation already has a confirmed execution"
            )
        current = await repo.latest_task_payload(task_id, PREPARATION)
        revision = current["revision"] if current else 0
        # Lock only assets the user is saving. Other owners' installations never enter the choices.
        if body or submission:
            for asset_id in sorted(
                {p["asset_id"] for p in bindings["accounts"].values()}
            ):
                await repo.capability_asset(owner, asset_id, lock=True)
        assets = await repo.capability_assets(owner)
        options = connection_options(
            definition.access_requirements,
            assets,
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
        if preview_revision is not None:
            from app.application.workbench.formal_expert_preview import (
                execution_preview,
            )

            subject = await repo.subject_of_actor(owner)
            preview, local_sources = await execution_preview(
                task,
                revision,
                selected,
                review["review_digest"],
                definition,
                slots,
                bindings,
                graph,
                graph_issue,
                invalid_materials,
                invalid_accounts,
                assets,
                service,
                owner,
                subject,
            )
            sources, unavailable = execution_sources(
                graph, preview, subject, assets, registry.methods, local_sources
            )
            if (
                any(s.kind == "service_function" for s in local_sources.values())
                and not service.material_read_enabled
            ):
                unavailable = "driver_unavailable"
            preview["dispatch_gap"] = unavailable
            preview["preview_digest"] = digest(
                [preview, [s.fingerprint() for s in sources.values()]]
            )
            if submission:
                if (
                    graph is None
                    or unavailable
                    or submission.preview_digest != preview["preview_digest"]
                ):
                    raise StaleStateVersion("Execution scope changed or is incomplete")
                expected = {node.reference: node.definition for node in graph.nodes}
                if not temporary or not temporary["local_root"]:
                    expected[MethodRef.model_validate(selected)] = definition
                if await registry.local_definitions(owner, set(expected)) != expected:
                    raise StaleStateVersion("Method registration changed since preview")
                return await confirm_execution(
                    repo,
                    owner,
                    task,
                    selected,
                    definition,
                    graph,
                    preview,
                    sources,
                    temporary=bool(temporary and temporary["local_root"]),
                )
            return preview
    return {
        "execution": receipt,
        "input_types": {
            name: slot.schema_.get("type")
            if isinstance(slot.schema_.get("type"), str)
            else "json"
            for name, slot in slots.items()
        },
        "revision": revision,
        "bindings": bindings,
        "account_options": options,
        "materials": shelf,
        "material_names": names,
        "invalid_materials": invalid_materials,
        "invalid_accounts": invalid_accounts,
    }
