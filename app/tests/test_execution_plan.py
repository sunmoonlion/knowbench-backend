import copy

import pytest
from method_plan_fixtures import catalog, proposal

from app.domain.workbench.execution_plan import project_plan
from app.domain.workbench.method_plan import AcceptedMethodPlan, MethodPlan, output_name


def receipt(plan=None):
    return AcceptedMethodPlan(
        task_id="task",
        attempt_id="planner",
        pack_digest="sha256:" + "a" * 64,
        version=1,
        proposal=plan or proposal(),
    )


def test_projection_uses_instances_and_preserves_author_contract():
    author = catalog()
    before = copy.deepcopy(author.model_dump())
    view = project_plan(author, receipt(), profile_id="expert")
    first, final = view.pack.workflow
    assert first.step_id == "inspect_1"
    assert first.output_artifact == output_name("inspect_1")
    assert final.input_refs == (first.output_artifact,)
    assert (
        next(r for r in final.acceptance if r.kind == "covers").source
        == first.output_artifact + ".rows"
    )
    assert final.back_to == "inspect_1"
    assert final.method_text == author.method_units[0].contract.method_text
    assert final.output_schema == author.method_units[0].contract.output_schema
    assert final.reserve == author.method_units[0].contract.reserve
    assert view.final_artifacts == (output_name("report_1"),)
    assert author.model_dump() == before


def test_repeated_method_has_distinct_output_and_explicit_upstream_target():
    raw = proposal().model_dump(mode="json")
    raw["steps"].insert(1, {**raw["steps"][0], "instance_id": "inspect_2", "tools": []})
    raw["steps"][-1]["inputs"][0]["instance_id"] = "inspect_2"
    view = project_plan(
        catalog(), receipt(MethodPlan.model_validate(raw)), profile_id="expert"
    )
    assert len(set(s.output_artifact for s in view.pack.workflow)) == 3
    assert view.pack.workflow[1].tools == ()
    assert view.pack.workflow[-1].back_to == "inspect_2"
    assert view.pack.workflow[-1].input_refs == (output_name("inspect_2"),)


def test_view_is_task_local_and_rejects_permission_drift():
    a = project_plan(catalog(), receipt(), profile_id="expert")
    b = project_plan(catalog(), receipt(proposal(review=True)), profile_id="expert")
    assert len(a.pack.workflow) == 2 and len(b.pack.workflow) == 3
    raw = proposal().model_dump(mode="json")
    raw["steps"][-1]["tools"] = ["read_input"]
    with pytest.raises(ValueError, match="tools"):
        project_plan(
            catalog(), receipt(MethodPlan.model_validate(raw)), profile_id="expert"
        )
