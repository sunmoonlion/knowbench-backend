"""分身的计算工具与数值验收（SDD 0030 §3.2、§3.3）：求值白名单、按地址取数、计算记录、三种数值验收。"""

from __future__ import annotations

import contextlib
from decimal import Decimal
from typing import Any

import pytest
from test_workbench_knowbot_data import BINDING, DATASET, Records, _pin, _task

from app.application.workbench.acceptance import judge
from app.application.workbench.acceptance_numbers import judge_numbers
from app.application.workbench.knowbot_compute import COMPUTE_EVENT, KnowbotCompute
from app.domain.workbench import compute as calc
from app.domain.workbench.errors import ComputeRefused, NotFound, RecordsRefused
from app.domain.workbench.knowbots import KNOWBOT_TOOLS
from app.domain.workbench.packs import AcceptanceRule

SHEET = "利润表"
CELLS = {
    ("营业收入", "2024-03-31"): ("100.10", "100.10"),
    ("营业收入", "2024-06-30"): ("250.25", "250.25"),
    ("营业成本", "2024-06-30"): ("(150)", "-150"),
    ("备注", "2024-06-30"): ("未审计", None),
    ("零", "2024-06-30"): ("0", "0"),
}


def at(row: str, column: str, dataset: str = DATASET) -> str:
    return calc.address(dataset, SHEET, row, column)


class EventRepo:
    """钉表（同读数据的替身）加会话事件：计算记录就记在这里。"""

    def __init__(self, base: Any) -> None:
        self.base = base
        self.events: list[dict[str, Any]] = []

    async def get_knowbot_pin(self, **kw: Any) -> Any:
        return await self.base.get_knowbot_pin(**kw)

    async def test_run_for_task(self, task_id: str) -> Any:
        return None

    @contextlib.asynccontextmanager
    async def transaction(self):  # type: ignore[no-untyped-def]
        yield

    async def append_event(self, **kw: Any) -> dict[str, Any]:
        event = {"cursor": len(self.events) + 1, "type": kw["event_type"], **kw}
        self.events.append(event)
        return event

    async def list_task_events(self, task_id: str, *, types: tuple[str, ...]):
        return [
            e for e in self.events if e["task_id"] == task_id and e["type"] in types
        ]


class CellSource:
    def __init__(self, bump: str | None = None) -> None:
        self.asked: list[list[dict[str, str]]] = []
        self.version = bump or BINDING["version"]

    async def cells(self, purpose, cells):  # type: ignore[no-untyped-def]
        self.asked.append(cells)
        out = []
        for ref in cells:
            hit = (
                CELLS.get((ref["row"], ref["column"]))
                if ref["sheet"] == SHEET
                else None
            )
            out.append(
                {
                    **ref,
                    "found": hit is not None,
                    "raw": hit[0] if hit else None,
                    "number": hit[1] if hit else None,
                    "version": self.version,
                    "sha256": BINDING["sha256"],
                }
            )
        return out


def world(source: CellSource | None = None, task: dict | None = None):  # type: ignore[no-untyped-def]
    records = Records(task or _task(session_id="s1"), _pin())
    records.repo = EventRepo(records.repo)
    return records, KnowbotCompute(records, source or CellSource())  # type: ignore[arg-type]


# ---------- 求值 ----------


@pytest.mark.parametrize(
    ("expression", "values", "expected"),
    [
        ("h1 - q1", {"h1": Decimal("250.25"), "q1": Decimal("100.10")}, "150.15"),
        ("(a - b) / a * 100", {"a": Decimal(200), "b": Decimal(50)}, "75"),
        ("0.1 + 0.2", {}, "0.3"),
        ("-x + +x", {"x": Decimal(3)}, "0"),
        (
            "median(xs)",
            {"xs": [Decimal(3), Decimal(1), Decimal(2), Decimal(10)]},
            "2.5",
        ),
        (
            "sum(xs) + max(xs, y)",
            {"xs": [Decimal(1), Decimal(2)], "y": Decimal(9)},
            "12",
        ),
        ("avg(xs)", {"xs": [Decimal(1), Decimal(2)]}, "1.5"),
        ("abs(min(xs))", {"xs": [Decimal(-4), Decimal(2)]}, "4"),
    ],
)
def test_expressions_compute_exactly_in_decimal(expression, values, expected) -> None:
    assert calc.text_of(calc.evaluate(expression, values)) == expected


@pytest.mark.parametrize(
    ("expression", "values", "message"),
    [
        ("a / b", {"a": Decimal(1), "b": Decimal(0)}, calc.ZERO_DIVISOR),
        ("__import__('os')", {}, "not allowed"),
        ("a.real", {"a": Decimal(1)}, "only"),
        ("a ** 2", {"a": Decimal(1)}, "only"),
        ("a[0]", {"a": [Decimal(1)]}, "only"),
        ("'1' + 1", {}, "only numbers"),
        ("True + 1", {}, "only numbers"),
        ("b + 1", {}, "unknown name"),
        ("xs + 1", {"xs": [Decimal(1)]}, "needs sum"),
        ("sum()", {}, "no numbers"),
        ("abs(a, a)", {"a": Decimal(1)}, "abs takes one"),
        ("max(a, key=a)", {"a": Decimal(1)}, "only"),
        ("lambda: 1", {}, "only"),
        ("1 +", {}, "not valid"),
        ("", {}, "empty"),
        ("1" + "+1" * 300, {}, "too long"),
        ("-" * 30 + "1", {}, "too long or too deep"),
    ],
)
def test_anything_outside_the_whitelist_is_refused(expression, values, message) -> None:
    with pytest.raises(ComputeRefused, match=message):
        calc.evaluate(expression, values)


def test_addresses_split_at_the_first_bang_and_the_last_at() -> None:
    assert calc.parse_address(f"{DATASET}:利润表!其中：利息@收入@2024-06-30") == (
        DATASET,
        "利润表",
        "其中：利息@收入",
        "2024-06-30",
    )
    for bad in ["d1:利润表!营业收入@2024", f"{DATASET}:利润表营业收入@x", 12, None]:
        with pytest.raises(ComputeRefused, match="not a cell address"):
            calc.parse_address(bad)


def test_rounding_is_half_up_and_expressions_compare_by_tree() -> None:
    assert calc.text_of(calc.rounded(Decimal("2.345"), 2)) == "2.35"
    assert calc.text_of(calc.rounded(Decimal("-2.345"), 2)) == "-2.35"
    assert calc.same_expression("(rev - cost) / rev", "(rev-cost)/rev")
    assert not calc.same_expression("(rev - cost) / rev", "rev - cost / rev")
    assert not calc.same_expression("x", "1 +")


def test_a_knowbot_may_use_the_table_and_compute_tools() -> None:
    assert {"read_my_table", "compute"} <= KNOWBOT_TOOLS


# ---------- 计算工具 ----------


async def test_compute_takes_cells_by_address_and_records_the_computation() -> None:
    records, tool = world()
    first = await tool.compute(
        "t",
        "2024Q2 单季营业收入",
        "h1 - q1",
        {"h1": at("营业收入", "2024-06-30"), "q1": at("营业收入", "2024-03-31")},
    )
    assert (first["id"], first["value"], first["formula"]) == (
        "c1",
        "150.15",
        "h1 - q1",
    )
    assert first["inputs"][0] == {
        "name": "h1",
        "ref": at("营业收入", "2024-06-30"),
        "value": "250.25",
    }
    margin = await tool.compute(
        "t",
        "毛利率",
        "(rev + cost) / rev * 100",
        {"rev": at("营业收入", "2024-06-30"), "cost": at("营业成本", "2024-06-30")},
        2,
    )
    assert margin["value"] == "40.06"
    chained = await tool.compute(
        "t", "合计", "sum(xs)", {"xs": [first["id"], margin["id"]]}
    )
    assert chained["value"] == "190.21"
    [event, *_] = records.repo.events
    assert (
        event["event_type"] == COMPUTE_EVENT and event["task_id"] == records.task["id"]
    )
    assert event["payload"]["value"] == "150.15" and event["kind"] == "records"


@pytest.mark.parametrize(
    ("inputs", "expression", "message"),
    [
        ({"a": "123"}, "a", "not a cell address"),
        ({"a": 123}, "a", "must be a cell address"),
        ({"A": "x"}, "A", "lower-case"),
        ({}, "1", "inputs must be"),
        ({"a": at("没有这行", "2024-06-30")}, "a", "no such cell"),
        ({"a": at("备注", "2024-06-30")}, "a", "not a number"),
        (
            {"a": at("营业收入", "2024-06-30"), "z": at("零", "2024-06-30")},
            "a / z",
            "denominator is zero",
        ),
        ({"a": "c99"}, "a", "no earlier computation c99"),
    ],
)
async def test_compute_refuses_what_it_cannot_stand_behind(
    inputs, expression, message
) -> None:
    records, tool = world()
    with pytest.raises(ComputeRefused, match=message):
        await tool.compute("t", "x", expression, inputs)
    assert records.repo.events == []


async def test_compute_reads_only_the_bound_data_of_this_version() -> None:
    other = "0b9b8c52-3f0e-4c34-9d0b-2b7f4d3c1a11"
    records, tool = world()
    with pytest.raises(NotFound):
        await tool.compute("t", "x", "a", {"a": at("营业收入", "2024-06-30", other)})
    _, changed = world(CellSource(bump="2"))
    with pytest.raises(NotFound):
        await changed.compute("t", "x", "a", {"a": at("营业收入", "2024-06-30")})
    _, builtin = world(
        task=_task(pack_source="builtin", profile_id="fin", session_id="s1")
    )
    with pytest.raises(RecordsRefused):
        await builtin.compute("t", "x", "a", {"a": at("营业收入", "2024-06-30")})


async def test_compute_limits_round_and_input_count() -> None:
    _, tool = world()
    with pytest.raises(ComputeRefused, match="round"):
        await tool.compute("t", "x", "a", {"a": at("营业收入", "2024-06-30")}, 11)
    many = [at("营业收入", "2024-06-30")] * (calc.MAX_INPUTS + 1)
    with pytest.raises(ComputeRefused, match="at most"):
        await tool.compute("t", "x", "sum(xs)", {"xs": many})


# ---------- 数值验收 ----------


def rule(kind: str, path: str, **kw: Any) -> AcceptanceRule:
    return AcceptanceRule(kind=kind, path=path, message=f"{kind} 不过", **kw)  # type: ignore[arg-type]


async def verdict_of(
    text: str, rules: tuple[AcceptanceRule, ...], records, source=None
):  # type: ignore[no-untyped-def]
    return await judge_numbers(
        judge(text, rules),
        rules,
        repo=records.repo,
        task={**records.task, "owner_actor_id": records.owner},
        source=source if source is not None else CellSource(),
    )


async def test_computed_numbers_must_be_in_the_record_and_match() -> None:
    records, tool = world()
    q2 = await tool.compute(
        "t",
        "Q2",
        "h1 - q1",
        {"h1": at("营业收入", "2024-06-30"), "q1": at("营业收入", "2024-03-31")},
    )
    rules = (rule("computed", "figures", tolerance=0.01, expression="h1-q1"),)
    good = f'{{"figures": [{{"compute_id": "{q2["id"]}", "value": 150.15}}]}}'
    assert (await verdict_of(good, rules, records)).ok
    for figures, reason in [
        (f'[{{"compute_id": "{q2["id"]}", "value": 150.2}}]', "computed 150.15"),
        ('[{"compute_id": "c42", "value": 1}]', "not computed in this task"),
        (f'[{{"compute_id": "{q2["id"]}"}}]', "has no number"),
        ('"x"', "not a list"),
    ]:
        v = await verdict_of(f'{{"figures": {figures}}}', rules, records)
        assert not v.ok and reason in v.checks[-1]["detail"], figures
        assert v.failures[-1].startswith("computed(figures): computed 不过 — ")
    strict = (rule("computed", "figures", expression="(h1 - q1) / q1"),)
    v = await verdict_of(good, strict, records)
    assert not v.ok and "used 'h1 - q1'" in v.checks[-1]["detail"]


async def test_identity_and_sourced() -> None:
    records, tool = world()
    a = await tool.compute("t", "a", "x * 2", {"x": at("营业收入", "2024-03-31")})
    b = await tool.compute("t", "b", "x + x", {"x": at("营业收入", "2024-03-31")})
    c = await tool.compute("t", "c", "x", {"x": at("营业收入", "2024-06-30")})
    rules = (rule("identity", "checks"), rule("sourced", "raw"))
    text = (
        f'{{"checks": [{{"left_id": "{a["id"]}", "right_id": "{b["id"]}"}}], '
        f'"raw": [{{"address": "{at("营业成本", "2024-06-30")}", "value": "-150"}}]}}'
    )
    v = await verdict_of(text, rules, records)
    assert v.ok and [c["pass"] for c in v.checks] == [True, True]
    text = (
        f'{{"checks": [{{"left_id": "{a["id"]}", "right_id": "{c["id"]}"}}], '
        f'"raw": [{{"address": "{at("营业成本", "2024-06-30")}", "value": 150}}]}}'
    )
    v = await verdict_of(text, rules, records)
    assert [c["pass"] for c in v.checks] == [False, False]
    assert "data has -150" in v.checks[1]["detail"]
    missing = (
        f'{{"checks": [], "raw": [{{"address": "{at("没有", "2024")}", "value": 1}}]}}'
    )
    v = await verdict_of(missing, rules, records)
    assert v.checks[0]["pass"] and "no such cell" in v.checks[1]["detail"]


async def test_numeric_rules_are_not_checked_when_the_output_is_not_json() -> None:
    records, _ = world()
    rules = (rule("computed", "figures"),)
    v = await verdict_of("not json", rules, records)
    assert not v.ok
    assert [(c["rule"], c["pass"]) for c in v.checks] == [
        ("json_object", False),
        ("computed", None),
    ]


async def test_structural_judge_leaves_numeric_rules_to_the_application() -> None:
    rules = (
        rule("computed", "figures"),
        AcceptanceRule(kind="non_empty", path="figures"),
    )
    v = judge('{"figures": [1]}', rules)
    assert v.ok and [c["rule"] for c in v.checks] == ["non_empty"]


def test_numeric_rules_need_a_path_and_only_computed_takes_an_expression() -> None:
    with pytest.raises(ValueError, match="needs a path"):
        AcceptanceRule(kind="sourced")
    with pytest.raises(ValueError, match="only a computed"):
        AcceptanceRule(kind="identity", path="x", expression="a")
    with pytest.raises(ValueError):
        AcceptanceRule(kind="computed", path="x", tolerance=-1)


async def test_sourced_without_a_data_source_does_not_pass() -> None:
    records, _ = world()
    rules = (rule("sourced", "raw"),)
    text = f'{{"raw": [{{"address": "{at("营业收入", "2024-06-30")}", "value": 1}}]}}'
    v = await judge_numbers(
        judge(text, rules),
        rules,
        repo=records.repo,
        task={**records.task, "owner_actor_id": records.owner},
        source=None,
    )
    assert not v.ok and "cannot be read here" in v.checks[-1]["detail"]
    # 取不到数是平台的事，不是数对不上（审读第 3 条）
    assert v.checks[-1]["platform"] is True


async def test_a_wrong_number_is_not_a_platform_error() -> None:
    records, tool = world()
    rules = (rule("sourced", "raw"),)
    text = f'{{"raw": [{{"address": "{at("营业成本", "2024-06-30")}", "value": 150}}]}}'
    v = await verdict_of(text, rules, records)
    assert not v.ok and "platform" not in v.checks[-1]


def _step(checks: list[dict[str, Any]]) -> dict[str, Any]:
    return {
        "step_id": "answer",
        "status": "rejected",
        "attempts": [{"outcome": "rejected", "checks": checks}],
    }


def test_a_case_says_why_a_step_failed_and_platform_trouble_is_an_error() -> None:
    from app.domain.workbench.test_runs import RunCase, case_result

    case = RunCase.model_validate(
        {
            "case_id": "c1",
            "input": {"question": "q"},
            "expectations": [
                {"artifact": "a", "path": "x", "kind": "equals", "value": 1}
            ],
        }
    )
    wrong = {
        "rule": "sourced",
        "label": "取出的数与表里一致",
        "pass": False,
        "detail": "#0 d!data!营收@2024 says 150 but the data has -150",
    }
    failed = case_result(
        case=case, task={"state": "FAILED"}, steps=[_step([wrong])], artifacts={}
    )
    assert failed["status"] == "failed"
    assert failed["steps"][0]["failed_checks"] == ["取出的数与表里一致"]
    assert failed["steps"][0]["details"] == [
        "取出的数与表里一致: #0 d!data!营收@2024 says 150 but the data has -150"
    ]
    unread = wrong | {
        "detail": "user_data_unavailable: reading the user's data is not configured",
        "platform": True,
    }
    broken = case_result(
        case=case, task={"state": "FAILED"}, steps=[_step([unread])], artifacts={}
    )
    assert broken["status"] == "error"
    assert "could not check a step" in broken["note"]
    assert "not configured" in broken["note"]
    long = wrong | {"detail": "长" * 500}
    cut = case_result(
        case=case, task={"state": "FAILED"}, steps=[_step([long])], artifacts={}
    )
    assert len(cut["steps"][0]["details"][0]) == 200


@pytest.mark.parametrize(
    ("kind", "want", "value", "met"),
    [
        ("equals", 12, "12%", False),  # 带百分号的不当数（与 knowlab 一致）
        ("equals", "12%", "12%", True),  # 要比就写成字符串
        ("approx", 0.12, "12%", False),
        ("approx", 0.12, "0.1201", True),
        ("equals", True, "true", True),  # 交回物写成字符串也算
        ("equals", True, True, True),
        ("equals", False, "true", False),
    ],
)
def test_percent_signs_and_booleans_compare_the_same_way(kind, want, value, met):
    from app.domain.workbench.test_runs import Expectation, expectation_met

    expectation = Expectation(
        artifact="a",
        path="x",
        kind=kind,
        value=want,
        tolerance=0.01 if kind == "approx" else None,
    )
    assert expectation_met(expectation, {"a": {"x": value}}) is met
