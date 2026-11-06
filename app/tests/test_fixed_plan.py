"""固定专家的计划（SDD 0050 第二节）：按作者顺序生成、只进回执、模型交不了。"""

import pytest
from pydantic import ValidationError

from app.domain.workbench.fixed_plan import (
    FIXED_PLAN_CONTRACT,
    fixed_plan,
    validate_fixed_plan,
)
from app.domain.workbench.method_plan import AcceptedMethodPlan, parse_plan
from app.domain.workbench.packs import BUILTIN_PACKS, SMOKE, find_pack


def _with(pack, index, **changes):
    steps = list(pack.workflow)
    steps[index] = steps[index].model_copy(update=changes)
    return pack.model_copy(update={"workflow": tuple(steps)})


def test_smoke_follows_the_author_order_and_keeps_the_names():
    plan = fixed_plan(SMOKE)
    assert plan is not None
    assert plan.contract == FIXED_PLAN_CONTRACT
    assert plan.parent_version == 0
    assert [(s.instance_id, s.method_id) for s in plan.steps] == [
        ("plan", "plan"),
        ("answer", "answer"),
    ]
    [binding] = plan.steps[1].inputs
    assert (binding.slot, binding.instance_id, binding.version) == (
        "plan",
        "plan",
        None,
    )
    assert plan.final_outputs == {"answer": "answer"}
    assert plan.steps[0].tools == SMOKE.workflow[0].tools


@pytest.mark.parametrize(
    "pack",
    [*BUILTIN_PACKS.values(), find_pack("FIND_EXPERT", "4")],
    ids=lambda p: f"{p.profile_id}-{p.version}",
)
def test_every_builtin_pack_has_a_fixed_plan(pack):
    plan = fixed_plan(pack)
    assert plan is not None
    validate_fixed_plan(pack, plan)


def test_the_plan_must_match_the_pinned_pack():
    plan = fixed_plan(SMOKE)
    assert plan is not None
    other = plan.model_copy(update={"final_outputs": {"plan": "plan"}})
    with pytest.raises(ValueError):
        validate_fixed_plan(SMOKE, other)
    with pytest.raises(ValueError):
        validate_fixed_plan(_with(SMOKE, 1, title="改过的标题", tools=("x",)), plan)


def test_a_planner_cannot_submit_a_fixed_plan():
    plan = fixed_plan(SMOKE)
    assert plan is not None
    with pytest.raises(ValidationError):
        parse_plan(plan.model_dump(mode="json"))


def test_the_receipt_carries_a_fixed_plan():
    plan = fixed_plan(SMOKE)
    assert plan is not None
    receipt = AcceptedMethodPlan(
        task_id="t1",
        attempt_id="a1",
        pack_digest="sha256:" + "0" * 64,
        version=1,
        proposal=plan,
    )
    again = AcceptedMethodPlan.model_validate(receipt.model_dump(mode="json"))
    assert again.proposal == plan
    assert again.proposal.digest() == plan.digest()


@pytest.mark.parametrize(
    "index,changes",
    [
        (1, {"input_refs": ("nobody_makes_this",)}),  # 前面没有哪一步产出它
        (1, {"input_refs": ("answer",)}),  # 只能接前面的步骤
        (0, {"mode": "compete"}),  # 竞争第一期不开
        (1, {"on_reject": "back", "back_to": "elsewhere"}),  # 退回不指本步输入
        (1, {"step_id": "plan"}),  # 步骤 id 重复
        (0, {"step_id": "定范围"}),  # 不合方法 id 的格式
    ],
)
def test_packs_the_fixed_plan_cannot_express_stay_on_the_old_path(index, changes):
    assert fixed_plan(_with(SMOKE, index, **changes)) is None


def test_a_back_to_an_input_is_kept_in_the_pack():
    pack = _with(SMOKE, 1, on_reject="back", back_to="plan")
    plan = fixed_plan(pack)
    assert plan is not None
    assert plan.steps[1].inputs[0].instance_id == "plan"
