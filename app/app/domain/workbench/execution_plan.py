"""Task-local projection of frozen methods into existing step contracts.

No second author compiler: only instance names, input connections and selected
permissions change. The original catalog and accepted plan remain immutable.
"""

from __future__ import annotations

from dataclasses import dataclass

from app.domain.workbench.knowbots import V2_TOOLS
from app.domain.workbench.method_catalog import MethodCatalog
from app.domain.workbench.method_plan import (
    AcceptedMethodPlan,
    SlottedPlannedStep,
    output_name,
    validate_composition,
)
from app.domain.workbench.packs import ExpertPack, StepContract


@dataclass(frozen=True)
class ExecutionPlan:
    pack: ExpertPack
    receipt: AcceptedMethodPlan | None = None

    @property
    def dynamic(self) -> bool:
        return self.receipt is not None

    @property
    def final_artifacts(self) -> tuple[str, ...]:
        if self.receipt is None:
            return (
                (self.pack.workflow[-1].output_artifact,) if self.pack.workflow else ()
            )
        return tuple(
            output_name(i) for i in self.receipt.proposal.final_outputs.values()
        )


def project_plan(
    catalog: MethodCatalog, receipt: AcceptedMethodPlan, *, profile_id: str
) -> ExecutionPlan:
    validate_composition(catalog, receipt.proposal, available_tools=V2_TOOLS)
    units = {u.contract.step_id: u.contract for u in catalog.method_units}
    workflow = []
    for planned in receipt.proposal.steps:
        author = units[planned.method_id]
        bound = {b.slot: b.instance_id for b in planned.inputs}
        data = author.model_dump(mode="json")
        data.update(
            step_id=planned.instance_id,
            output_artifact=output_name(planned.instance_id),
            input_refs=[
                output_name(bound[slot]) for slot in author.input_refs if slot in bound
            ],
            tools=list(planned.tools),
            back_to=bound[author.back_to] if author.back_to else None,
        )
        data.pop("input_slots", None)
        checks = []
        for rule in data["acceptance"]:
            guard = rule.pop("when_input_present", None)
            if guard is not None and guard not in bound:
                continue  # Only the frozen author's explicit guard permits this.
            if rule["source"]:
                slot, path = rule["source"].split(".", 1)
                rule["source"] = output_name(bound[slot]) + "." + path
            checks.append(rule)
        data["acceptance"] = checks
        if isinstance(planned, SlottedPlannedStep) and planned.omitted_inputs:
            data["method_text"] += (
                "\n本题未选的输入槽（没有可读成果，不得从历史补造）："
                + ", ".join(planned.omitted_inputs)
            )
        workflow.append(StepContract.model_validate(data))
    pack = catalog_header(catalog, profile_id=profile_id).model_copy(
        update={"workflow": tuple(workflow)}
    )
    return ExecutionPlan(pack=pack, receipt=receipt)


def catalog_header(catalog: MethodCatalog, *, profile_id: str) -> ExpertPack:
    """Display identity before planning, with no invented method sequence."""
    return ExpertPack(
        pack_id=catalog.pack_id,
        version=catalog.version,
        profile_id=profile_id,
        title=catalog.title,
        name=catalog.name,
        tagline=catalog.tagline,
        scope=catalog.scope,
        workflow=(),
    )
