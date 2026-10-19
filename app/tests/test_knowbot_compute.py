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
        self.artifacts: list[dict[str, Any]] = []  # 上游交回物（covers 用）

    async def list_artifacts_with_content(self, task_id: str) -> list[dict[str, Any]]:
        return self.artifacts

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


async def test_a_computed_zero_is_a_number() -> None:
    """勾稽差额算出 0：判得过；交回物说 1 就是没对上，不是崩（K19 第 3 步）。"""
    records, tool = world()
    zero = await tool.compute(
        "t", "diff", "a - b", {"a": at("零", "2024-06-30"), "b": at("零", "2024-06-30")}
    )
    rules = (rule("computed", "diffs", tolerance=1),)
    ok = f'{{"diffs": [{{"compute_id": "{zero["id"]}", "value": "0"}}]}}'
    assert (await verdict_of(ok, rules, records)).ok
    off = f'{{"diffs": [{{"compute_id": "{zero["id"]}", "value": 5}}]}}'
    v = await verdict_of(off, rules, records)
    assert not v.ok and "says 5 but computed 0" in v.checks[-1]["detail"]


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


# ---------- 引文（SDD 0036） ----------

PAGES = {3: "报告期内营业收入同比增长15.7%，主要得益于系列酒销量增加"}


class QuoteSource(CellSource):
    """第 3 页有一句话；原话是它的一段就算找到（真的规整与跨页在 knowlab 那边测）。"""

    def __init__(self, fail: Exception | None = None, bump: str | None = None) -> None:
        super().__init__(bump)
        self.fail = fail
        self.quoted: list[list[dict[str, Any]]] = []

    async def quotes(self, purpose, quotes):  # type: ignore[no-untyped-def]
        if self.fail is not None:
            raise self.fail
        self.quoted.append(quotes)
        return [
            {
                "dataset_id": q["dataset_id"],
                "page": q["page"],
                "found": q["text"] in PAGES.get(q["page"], ""),
                "reason": None
                if q["text"] in PAGES.get(q["page"], "")
                else "not_found",
                "version": self.version,
                "sha256": BINDING["sha256"],
            }
            for q in quotes
        ]


def checks_of(*items: tuple[str, list[tuple[int, str]]]) -> str:
    import json

    return json.dumps(
        {
            "checks": [
                {
                    "conclusion": f"结论 {i}",
                    "verdict": verdict,
                    "quotes": [
                        {"dataset_id": DATASET, "page": p, "text": t} for p, t in quotes
                    ],
                }
                for i, (verdict, quotes) in enumerate(items)
            ]
        },
        ensure_ascii=False,
    )


SUPPORTED = (rule("quoted", "checks", field="verdict", equals="支持"),)


async def test_supported_conclusions_must_quote_text_that_is_really_there() -> None:
    records, _ = world()
    source = QuoteSource()
    good = checks_of(
        ("支持", [(3, "营业收入同比增长15.7%")]),
        ("不支持", []),  # 没提到：不引，不挡
    )
    v = await verdict_of(good, SUPPORTED, records, source)
    assert v.ok and v.checks[-1]["pass"] is True
    assert source.quoted == [
        [{"dataset_id": DATASET, "page": 3, "text": "营业收入同比增长15.7%"}]
    ]

    unquoted = checks_of(("支持", []), ("不支持", []))
    v = await verdict_of(unquoted, SUPPORTED, records, source)
    assert not v.ok and "#0 says 支持 but quotes nothing" in v.checks[-1]["detail"]

    wrong_page = checks_of(("支持", [(4, "营业收入同比增长15.7%")]))
    v = await verdict_of(wrong_page, SUPPORTED, records, source)
    assert not v.ok and "#0 page 4: not_found" in v.checks[-1]["detail"]
    assert "platform" not in v.checks[-1]


async def test_a_not_supported_conclusion_may_quote_but_not_make_things_up() -> None:
    records, _ = world()
    made_up = checks_of(("不支持", [(3, "毛利率大幅上升")]))
    v = await verdict_of(made_up, SUPPORTED, records, QuoteSource())
    assert not v.ok and "#0 page 3: not_found" in v.checks[-1]["detail"]
    all_no = checks_of(("不支持", []), ("不支持", []))
    assert (await verdict_of(all_no, SUPPORTED, records, QuoteSource())).ok


async def test_quotes_must_have_the_right_shape_and_bound_data() -> None:
    records, _ = world()
    bad_shape = (
        '{"checks": [{"verdict": "支持", "quotes": [{"page": "3", "text": "x"}]}]}'
    )
    v = await verdict_of(bad_shape, SUPPORTED, records, QuoteSource())
    assert not v.ok and "not {dataset_id, page, text}" in v.checks[-1]["detail"]
    other = (
        '{"checks": [{"verdict": "支持", "quotes": '
        '[{"dataset_id": "00000000-0000-4000-8000-000000000000", "page": 3, '
        '"text": "营业收入"}]}]}'
    )
    v = await verdict_of(other, SUPPORTED, records, QuoteSource())
    assert not v.ok and "does not bind" in v.checks[-1]["detail"]
    assert "platform" not in v.checks[-1]
    bumped = checks_of(("支持", [(3, "营业收入同比增长15.7%")]))
    v = await verdict_of(bumped, SUPPORTED, records, QuoteSource(bump="2"))
    assert not v.ok and "not the bound version" in v.checks[-1]["detail"]


async def test_a_badly_shaped_quote_is_the_reply_s_fault_and_never_sent() -> None:
    """页码 0、负数、空原文、太长、编号不像数据编号：交回物的错，不是平台的（审读 10-19 第 2 条）。"""
    records, _ = world()
    for page, text, why in (
        (0, "营业收入同比增长15.7%", "bad page number"),
        (-2, "营业收入同比增长15.7%", "bad page number"),
        (3, "   ", "quote text empty or too long"),
        (3, "字" * 4001, "quote text empty or too long"),
    ):
        source = QuoteSource()
        v = await verdict_of(
            checks_of(("支持", [(page, text)])), SUPPORTED, records, source
        )
        assert not v.ok and why in v.checks[-1]["detail"], why
        assert "platform" not in v.checks[-1] and source.quoted == []
    odd_id = '{"checks": [{"verdict": "支持", "quotes": [{"dataset_id": "x", "page": 3, "text": "营业收入同比增长"}]}]}'
    v = await verdict_of(odd_id, SUPPORTED, records, QuoteSource())
    assert not v.ok and "bad dataset id" in v.checks[-1]["detail"]
    assert "platform" not in v.checks[-1]


async def test_a_wrong_number_of_answers_from_knowlab_is_a_platform_error() -> None:
    class Short(QuoteSource):
        async def quotes(self, purpose, quotes):  # type: ignore[no-untyped-def]
            return (await super().quotes(purpose, quotes))[:-1]

    records, _ = world()
    text = checks_of(
        ("支持", [(3, "营业收入同比增长15.7%")]), ("支持", [(3, "系列酒销量增加")])
    )
    v = await verdict_of(text, SUPPORTED, records, Short())
    assert not v.ok and v.checks[-1]["platform"] is True


async def test_knowlab_trouble_while_checking_quotes_is_a_platform_error() -> None:
    from app.domain.workbench.errors import UserDataUnavailable

    records, _ = world()
    text = checks_of(("支持", [(3, "营业收入同比增长15.7%")]))
    down = QuoteSource(fail=UserDataUnavailable("the user's data is unavailable now"))
    v = await verdict_of(text, SUPPORTED, records, down)
    assert not v.ok and v.checks[-1]["platform"] is True
    v = await judge_numbers(
        judge(text, SUPPORTED),
        SUPPORTED,
        repo=records.repo,
        task={**records.task, "owner_actor_id": records.owner},
        source=None,
    )
    assert not v.ok and v.checks[-1]["platform"] is True


async def test_many_quotes_are_checked_in_batches() -> None:
    records, _ = world()
    source = QuoteSource()
    text = checks_of(*[("支持", [(3, "营业收入同比增长15.7%")])] * 150)
    v = await verdict_of(text, SUPPORTED, records, source)
    assert v.ok and [len(b) for b in source.quoted] == [100, 50]


def test_quoted_needs_a_path() -> None:
    with pytest.raises(ValueError, match="needs a path"):
        AcceptanceRule(kind="quoted")


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


def test_a_run_the_platform_stopped_is_an_error_and_others_say_why() -> None:
    """K19 第 2 稿 c1：推它的程序重启后收尾的委托记出错、写原因；别的失败也写原因。"""
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
    timed_out = {"rule": "in_time", "pass": False, "label": "在时限内交回"}
    restarted = {
        "error": "the runner driving this task restarted; the step did not finish",
        "attempt_id": None,
        "turn_id": None,
        "cost": "unknown",
        "platform": True,
    }
    settled = case_result(
        case=case,
        task={"state": "FAILED"},
        steps=[_step([timed_out])],
        artifacts={},
        reason=restarted,
    )
    assert settled["status"] == "error"
    assert settled["note"] == (
        "the platform stopped this run: "
        "the runner driving this task restarted; the step did not finish"
    )
    stopped = case_result(
        case=case,
        task={"state": "FAILED"},
        steps=[],
        artifacts={},
        reason={"interaction": "i-1", "by": "user stopped"},
    )
    assert (stopped["status"], stopped["note"]) == (
        "failed",
        "the run ended as failed: user stopped",
    )
    missing = case_result(
        case=case,
        task={"state": "FAILED"},
        steps=[],
        artifacts={},
        reason={"code": "missing_input", "step": "s2", "artifact": "scope"},
    )
    assert missing["note"] == "the run ended as failed: missing_input"
    bare = case_result(case=case, task={"state": "FAILED"}, steps=[], artifacts={})
    assert (bare["status"], bare["note"]) == ("failed", "the run ended as failed")


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


# ---------- 结构与对得上上游（审读 10-19：第 3 稿第 6 步空交也能过） ----------

SHAPE = (
    AcceptanceRule(
        kind="each_has",
        path="checks",
        keys=("finding", "verdict", "quotes"),
        field="verdict",
        values=("支持", "不支持"),
        message="结构不对",
    ),
)


def test_each_item_has_its_keys_and_one_of_the_allowed_values() -> None:
    import json

    def ok(checks: list) -> bool:
        return judge(json.dumps({"checks": checks}, ensure_ascii=False), SHAPE).ok

    good = {"finding": "a", "verdict": "支持", "quotes": []}
    assert ok([good, {**good, "verdict": "不支持"}])
    assert ok([])  # 空的一串算过；要非空另配 list_min
    assert not ok([{}])
    assert not ok([{**good, "verdict": "部分支持"}])
    assert not ok([{"finding": "a", "quotes": []}])
    v = judge('{"checks": {"finding": "a"}}', SHAPE)
    assert not v.ok and v.checks[-1]["detail"] == "not a list"


COVERS = (
    AcceptanceRule(
        kind="covers",
        path="checks",
        field="finding",
        source="indicators.findings",
        message="结论对不上第 5 步",
    ),
    AcceptanceRule(
        kind="covers",
        path="sections",
        also=("sections_missing",),
        field="dataset_id",
        source="scope.reports",
        source_field="dataset_id",
        message="年报没有逐份交代",
    ),
)


async def test_every_upstream_finding_and_report_is_covered_once() -> None:
    import json

    records, _ = world()
    records.repo.artifacts = [
        {"name": "indicators", "kind": "step", "content": {"findings": ["甲", "乙"]}},
        {
            "name": "scope",
            "kind": "step",
            "content": {"reports": [{"dataset_id": "r1"}, {"dataset_id": "r2"}]},
        },
    ]

    async def run(checks: list, sections: list, missing: list):  # type: ignore[no-untyped-def]
        text = json.dumps(
            {"checks": checks, "sections": sections, "sections_missing": missing},
            ensure_ascii=False,
        )
        return await verdict_of(text, COVERS, records)

    v = await run(
        [{"finding": "甲"}, {"finding": "乙"}],
        [{"dataset_id": "r1"}],
        [{"dataset_id": "r2"}],
    )
    assert v.ok, v.checks
    v = await run([{"finding": "甲"}], [{"dataset_id": "r1"}], [{"dataset_id": "r2"}])
    assert not v.ok and "missing '乙'" in v.checks[0]["detail"]
    v = await run(
        [{"finding": "甲"}, {"finding": "甲"}, {"finding": "乙"}, {"finding": "丙"}],
        [{"dataset_id": "r1"}, {"dataset_id": "r2"}],
        [{"dataset_id": "r2"}],
    )
    assert (
        "twice '甲'" in v.checks[0]["detail"]
        and "not upstream '丙'" in v.checks[0]["detail"]
    )
    assert "twice 'r2'" in v.checks[1]["detail"]
    # 没有这家公司：上游都是空的，这一步也空，算过
    records.repo.artifacts = [
        {"name": "indicators", "kind": "step", "content": {"findings": []}},
        {"name": "scope", "kind": "step", "content": {"reports": []}},
    ]
    assert (await run([], [], [])).ok
    records.repo.artifacts = []
    v = await run([], [], [])
    assert not v.ok and "upstream indicators is missing" in v.checks[0]["detail"]


def test_new_rules_check_their_own_settings() -> None:
    with pytest.raises(ValueError, match="each_has needs a path"):
        AcceptanceRule(kind="each_has")
    with pytest.raises(ValueError, match="limit values"):
        AcceptanceRule(kind="all_equal", path="x", field="v", values=("a",))
    with pytest.raises(ValueError, match="covers needs"):
        AcceptanceRule(kind="covers", path="x", field="f", source="nodot")
    with pytest.raises(ValueError, match="only covers"):
        AcceptanceRule(kind="list_min", path="x", source="a.b")
