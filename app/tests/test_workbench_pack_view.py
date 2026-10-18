"""专家包给用户看的那一面（PRD/apps/investment-expert.md 第四、五、十节，AT-INV-17 至 19）。"""

from __future__ import annotations

import json
from decimal import Decimal

import pytest

from app.domain.workbench.pack_view import after_rejection, pack_view
from app.domain.workbench.packs import (
    BUILTIN_PACKS,
    DATA_QUERY,
    FIN_REVIEW,
    SMOKE,
    listed_packs,
)
from app.domain.workbench.step_view import budget_view, money, reason_text

LISTED = [p.profile_id for p in listed_packs()]


def test_the_smoke_pack_is_ours_and_not_listed():
    assert SMOKE.internal
    # 2026-10-18 内置专家全部撤下（待办账 111，数据拓扑）：给用户看的清单是空的
    assert LISTED == []


@pytest.mark.parametrize("pack_id", LISTED)
def test_every_listed_pack_can_be_explained_to_a_user(pack_id):
    pack = BUILTIN_PACKS[pack_id]
    assert pack.name and pack.tagline
    for question in (
        "解决什么",
        "不解决什么",
        "输入",
        "输出",
        "谁做判断",
        "失败怎么办",
    ):
        assert pack.answers.get(question), question
    for step in pack.workflow:
        assert step.summary, step.step_id
        for rule in step.acceptance:
            assert rule.label, (step.step_id, rule.kind)
            assert rule.message, (step.step_id, rule.kind)


@pytest.mark.parametrize("pack_id", list(BUILTIN_PACKS))
def test_the_method_never_leaves(pack_id):
    """AT-INV-19"""
    pack = BUILTIN_PACKS[pack_id]
    shown = json.dumps(pack_view(pack), ensure_ascii=False)
    for step in pack.workflow:
        assert step.method_text not in shown
        # 方法里成句的话也不在：取每个方法开头的一段
        assert step.method_text[:60] not in shown
    assert "method" not in shown
    assert "output_schema" not in shown


@pytest.mark.parametrize("pack_id", LISTED)
def test_words_shown_to_users_carry_no_code_names(pack_id):
    """AT-INV-17：任何地方都没有代号。编号只在 id 里，页面用它发请求，不显示。"""
    view = pack_view(BUILTIN_PACKS[pack_id])
    words = [
        view[k]
        for k in ("name", "tagline", "solves", "does_not_solve", "input", "output")
    ]
    for step in view["steps"]:
        words += [step["title"], step["summary"], step["after_rejection"]["text"]]
        words += [c["label"] for c in step["checks"]]
    text = "\n".join(words)
    for code in (
        "FIN_REVIEW",
        "DATA_QUERY",
        "FIN_FORECAST",
        "SMOKE",
        "F-POS",
        "json",
        "JSON",
    ):
        assert code not in text, code


def test_the_view_of_the_financial_review():
    view = pack_view(FIN_REVIEW)
    assert view["id"] == "FIN_REVIEW"
    assert view["name"] == "财报体检"
    assert view["uses_our_data"] is True
    assert [s["title"] for s in view["steps"]] == [
        "定范围",
        "数据摸底",
        "勾稽",
        "取数",
        "算指标",
        "对照官方",
        "成稿",
    ]
    assert [s["index"] for s in view["steps"]] == [1, 2, 3, 4, 5, 6, 7]
    reconcile = view["steps"][2]
    assert reconcile["uses"] == [
        {"artifact": "scope", "step": 1, "title": "定范围"},
        {"artifact": "profile", "step": 2, "title": "数据摸底"},
    ]
    assert [c["label"] for c in reconcile["checks"]] == [
        "交回的格式对",
        "有勾稽结果、跨期连续性、未解释的断点",
        "做了勾稽",
        "每条勾稽规则都平",
        "跨期的断点都有解释",
    ]
    assert view["least_budget"] == "0.35"


def test_what_happens_after_a_rejection():
    """三种说法：重做、退回、停下来问你。说的是专家包里写的，不多不少。"""
    said = {
        s.step_id: after_rejection(s, FIN_REVIEW)["text"] for s in FIN_REVIEW.workflow
    }
    assert said == {
        "scope": "重做，最多 1 次；仍不过，停下来问你",
        "profile": "重做，最多 1 次；仍不过，停下来问你",
        "reconcile": "停下来问你",  # 勾稽不平不重做：重做等于催它把数改平
        "extract": "重做，最多 1 次；仍不过，退回第 2 步，最多 1 次；仍不过，停下来问你",
        "metrics": "重做，最多 2 次；仍不过，停下来问你",
        "crosscheck": "停下来问你",
        "note": "重做，最多 1 次；仍不过，停下来问你",
    }
    execute = next(s for s in DATA_QUERY.workflow if s.step_id == "sql_execute")
    assert after_rejection(execute, DATA_QUERY) == {
        "reworks": 1,
        "then": "back",
        "back_to": 2,
        "backs": 1,
        "text": "重做，最多 1 次；仍不过，退回第 2 步，最多 1 次；仍不过，停下来问你",
    }


@pytest.mark.parametrize("pack_id", LISTED)
def test_the_dossier_layout_points_at_things_the_steps_return(pack_id):
    pack = BUILTIN_PACKS[pack_id]
    returned = {s.output_artifact: s for s in pack.workflow}
    assert pack.dossier
    keys = [b.key for b in pack.dossier]
    assert len(keys) == len(set(keys))
    for block in pack.dossier:
        assert block.artifact in returned, block.key
        shape = returned[block.artifact].output_schema
        for path in [block.path, *block.also.values()]:
            if path:
                assert path in shape, (block.key, path)
        if block.columns:
            # 登记的列，是这一步说好要交回的那张表里有的
            (row,) = shape[block.path][:1]
            for key, title in block.columns:
                assert key in row and title, (block.key, key)
    sections = [b.section for b in pack.dossier]
    assert sections[0] == "answer"
    for needed in ("data", "verified", "limits", "sources"):
        assert needed in sections


@pytest.mark.parametrize(
    ("stored", "shown"),
    [
        ("0.030000", "0.03"),
        ("2", "2.00"),
        ("1.021960", "1.02196"),
        ("0.5", "0.50"),
        ("0", "0.00"),
        ({"amount": "0.010000"}, "0.01"),
        ({}, "0.00"),
        (None, "0.00"),
        ("1E+1", "10.00"),
    ],
)
def test_how_money_is_written(stored, shown):
    assert money(stored) == shown


def test_the_budget_as_shown():
    assert budget_view(
        {"currency": "CNY", "limit": "2", "used": "0.180000", "reserved": "0.050000"}
    ) == {
        "currency": "CNY",
        "limit": "2.00",
        "used": "0.18",
        "reserved": "0.05",
        "left": "1.77",
        "running": "0.00",
        "spent": "0.18",
        "estimated": True,
    }
    # 正在做的这一步还没结账：页面上的「已花」把它加上
    live = budget_view(
        {"currency": "CNY", "limit": None, "used": "0.18", "reserved": "0"},
        running=Decimal("0.0312"),
    )
    # 不抹零头：一次调用常常只有几厘
    assert (live["running"], live["spent"]) == ("0.0312", "0.2112")
    assert live["limit"] is None and live["left"] is None


def test_data_that_changed_version_midway_is_not_hidden():
    from app.application.workbench.expert_desk import data_seen

    assert data_seen([{"data": None}, {}]) is None
    assert data_seen(
        [
            {"data": {"dataset": "d", "data_version": "v1", "as_of": "2025-12-31"}},
            {"data": None},
            {"data": {"data_version": "v2"}},
        ]
    ) == {
        "dataset": "d",
        "data_version": "v2",
        "as_of": "2025-12-31",
        "versions_seen": ["v1", "v2"],
    }


@pytest.mark.parametrize(
    ("state", "reason", "said"),
    [
        ("SUCCEEDED", None, ""),
        ("CANCELLED", {"by": "user"}, "你取消了这个委托"),
        (
            "FAILED",
            {"by": "user stopped", "interaction": "x"},
            "专家停下来问你，你选择了停止",
        ),
        ("FAILED", {"budget": "user declined top-up"}, "预算用完了，你没有追加"),
        ("FAILED", {"error": "turn timed out"}, "专家那一头出错了，这一步没有做完"),
        (
            "FAILED",
            {"code": "missing_input", "step": "note"},
            "前面步骤的交回物缺了，做不下去",
        ),
        ("FAILED", {}, "没有做完"),
        (
            "REJECTED",
            {
                "code": "no_expert_pack",
                "message": "这个问题不在专家范围，继续自己用 Codex 即可",
            },
            "这个问题不在专家的范围里，可以自己在对话里接着做",
        ),
        ("REJECTED", {}, "专家没有接这个委托"),
    ],
)
def test_why_a_delegation_ended_in_words_for_the_user(state, reason, said):
    assert reason_text(state, reason) == said
    # 给用户看的话里没有内部的词
    for word in ("Codex", "user", "stopped", "pack", "turn"):
        assert word not in said


def test_the_preparing_expert_is_listed_but_cannot_be_asked(monkeypatch):
    """所有者 2026-10-13 定的「筹备中」一档：看得见、读得到它要做什么，请不了。

    财务分析与预测同日切成可用；这一档留着给以后的专家，这里用一个筹备中的副本来验。
    """
    from app.application.workbench.delegation import expert_on_offer
    from app.domain.workbench.errors import ExpertNotReady
    from app.domain.workbench.packs import FIN_FORECAST

    preparing = FIN_FORECAST.model_copy(
        update={
            "status": "preparing",
            "preparing_note": "数据和方法正在接入。",
            "internal": False,  # 内置的已撤下，借一个对外的副本来验这一档
        }
    )
    monkeypatch.setitem(BUILTIN_PACKS, "FIN_FORECAST", preparing)
    view = pack_view(preparing)
    assert view["status"] == "preparing" and view["preparing_note"]
    assert view["name"] == "财务分析与预测" and len(view["steps"]) == 5
    with pytest.raises(ExpertNotReady):
        expert_on_offer("FIN_FORECAST")


def test_the_builtin_forecasting_expert_is_withdrawn():
    """10-13 切成可用的内置版，10-18 撤下（待办账 111）：改由我们以普通用户经 knowlab 孵化。"""
    from app.application.workbench.delegation import expert_on_offer
    from app.domain.workbench.errors import NoSuchExpert
    from app.domain.workbench.packs import FIN_FORECAST

    assert FIN_FORECAST.internal and FIN_FORECAST.version == "2"
    with pytest.raises(NoSuchExpert):
        expert_on_offer("FIN_FORECAST")
