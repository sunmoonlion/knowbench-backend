"""清单 v2 的专家（SDD 0043、0048，decisions ⑭）：清单的收与拒、按解析清单定计划（纯函数）。"""

from __future__ import annotations

import pytest
from knowbot_fixtures import KNOWBOT, manifest

from app.application.workbench.catalog import pack_from_pin
from app.domain.workbench import knowbots
from app.domain.workbench.data_plan import item_ref, plan
from app.domain.workbench.data_request import DataRequest
from app.domain.workbench.errors import KnowbotInvalid

ANNUAL = {
    "key": "annual",
    "name": "年报",
    "subject": "600519.SH",
    "doc_type": "annual_report",
    "period": {"kind": "latest", "count": 2},
    "formats": ["pdf"],
}
NEWS = {
    **ANNUAL,
    "key": "news",
    "name": "公告",
    "doc_type": "disclosure",
    "period": {"kind": "recent", "months": 12},
    "required": False,
}
BOUND = {
    "material_id": "m-bound",
    "version_no": 3,
    "sha256": "e" * 64,
    "basis_ref": f"expert:{KNOWBOT}@1",
}


def v2(**over):
    return {
        **manifest(),
        "schema": "knowbot-manifest/v2",
        "bindings": [],
        **over,
    }


def test_a_v2_manifest_is_accepted_with_its_bindings_and_tools():
    doc = v2(bindings=[BOUND])
    doc["pack"]["workflow"][0]["tools"] = [
        "list_inputs",
        "query_facts",
        "compute",
        "request_data",
        "find_subject",
    ]
    pinned = pack_from_pin(
        {"knowbot_id": KNOWBOT, "version": "1", "manifest": doc, "digest": "d"}
    )
    assert pinned.consumer == f"expert:{KNOWBOT}@1"
    assert pinned.bindings[0].material_id == "m-bound"
    # 清单 v2 = 逻辑 + 作者挑选的数据：带了数据需求声明就不认（decisions ⑭）
    with pytest.raises(KnowbotInvalid):
        pack_from_pin(
            {
                "knowbot_id": KNOWBOT,
                "version": "1",
                "manifest": {**doc, "data_needs": [ANNUAL]},
                "digest": "d",
            }
        )
    # v1 的读数据工具不认；清单 v1 不认（decisions ⑰）
    doc["pack"]["workflow"][0]["tools"] = ["list_my_data"]
    with pytest.raises(KnowbotInvalid):
        knowbots.pack_from_manifest(doc, knowbot_id=KNOWBOT, version="1")
    old = {**manifest(), "schema": "knowbot-manifest/v1"}
    with pytest.raises(KnowbotInvalid, match="schema must be knowbot-manifest/v2"):
        knowbots.pack_from_manifest(old, knowbot_id=KNOWBOT, version="1")
    with pytest.raises(KnowbotInvalid):
        pack_from_pin(
            {"knowbot_id": KNOWBOT, "version": "1", "manifest": old, "digest": "d"}
        )


def test_bindings_must_be_frozen_for_this_expert_version():
    for bad in (
        {**BOUND, "basis_ref": f"expert:{KNOWBOT}@2"},
        {**BOUND, "sha256": "short"},
        {**BOUND, "need": "annual"},
    ):
        with pytest.raises(KnowbotInvalid):
            pack_from_pin(
                {
                    "knowbot_id": KNOWBOT,
                    "version": "1",
                    "manifest": v2(bindings=[bad]),
                    "digest": "d",
                }
            )


def candidate(mid: str, basis: str = "display") -> dict:
    return {
        "material_id": mid,
        "version_no": 1,
        "sha256": "a" * 64,
        "basis": basis,
        "name": f"{mid}.pdf",
        "media_type": "application/pdf",
        "size_bytes": 1,
    }


def entry(status: str, year: str = "2025", need: str = "annual", **more) -> dict:
    return {
        "need_key": need,
        "status": status,
        "key": {
            "subject": "600519.SH",
            "doc_type": "annual_report",
            "period_start": f"{year}-01-01",
            "period_end": f"{year}-12-31",
        },
        "chosen": more.get("chosen", []),
        "candidates": more.get("candidates", []),
        "reason": None,
    }


NEEDS = [DataRequest.model_validate(ANNUAL), DataRequest.model_validate(NEWS)]


def test_the_plan_pins_what_is_settled_and_confirms_what_is_missing():
    resolution = {
        "items": [
            entry("resolved", chosen=[candidate("m1", "public")]),
            entry("to_confirm", "2024"),
            entry("not_collectable", need="news"),
        ]
    }
    off = plan(resolution, NEEDS)
    assert off.problems == [] and off.incomplete == [
        "年报 600519.SH 2024-01-01–2024-12-31：暂不能补采",
        "公告 600519.SH 2025-01-01–2025-12-31：采不来，要本人到 knowbase 上传",
    ]
    on = plan(resolution, NEEDS, supply_enabled=True)
    assert on.problems == [] and [c["need_key"] for c in on.confirm] == ["annual"]
    assert [(p["item_id"], p["basis"], p["basis_ref"]) for p in on.pins] == [
        ("m1", "public", "public:disclosure")
    ]
    assert on.incomplete == [
        "公告 600519.SH 2025-01-01–2025-12-31：采不来，要本人到 knowbase 上传"
    ]
    assert on.grant_items[0]["material_id"] == "m1"


def test_ambiguous_items_need_a_pick_and_anything_may_be_dropped():
    both = [candidate("m1"), candidate("m2")]
    resolution = {"items": [entry("ambiguous", candidates=both)]}
    ref = item_ref(resolution["items"][0])
    assert "要选一份" in plan(resolution, NEEDS[:1]).problems[0]
    picked = plan(
        resolution, NEEDS[:1], picks={ref: {"material_id": "m2", "version_no": 1}}
    )
    assert [p["item_id"] for p in picked.pins] == ["m2"] and not picked.problems
    assert (
        "不在候选里"
        in plan(
            resolution, NEEDS[:1], picks={ref: {"material_id": "mx", "version_no": 1}}
        ).problems[0]
    )
    gone = plan(resolution, NEEDS[:1], dropped=[ref])
    assert gone.problems == [] and "你没要" in gone.incomplete[0]
    hidden = plan(
        {"items": [entry("not_shown", candidates=[candidate("m3")])]}, NEEDS[:1]
    )
    assert hidden.problems == [] and "放开给 knowbench" in hidden.incomplete[0]


def test_bindings_ride_along_and_after_supply_missing_is_missing():
    bound = knowbots.FrozenBinding.model_validate(BOUND)
    done = plan(
        {"items": [entry("waiting", "2024")]}, NEEDS[:1], (bound,), after_supply=True
    )
    assert done.problems == []
    assert done.missing == ["年报 600519.SH 2024-01-01–2024-12-31：没取到"]
    assert done.incomplete == ["年报 600519.SH 2024-01-01–2024-12-31：没取到"]
    assert [(p["item_id"], p["basis"], p["slot"]) for p in done.pins] == [
        ("m-bound", "frozen_binding", None)
    ]


def test_anything_may_go_and_gaps_are_only_recorded():
    """第 0 步与运行中提的数据由用户定（decisions ⑭）：哪一样都能去掉，标了必需的缺了
    也照做，记「数据不全」；有歧义的仍要选一份或去掉。"""
    resolution = {
        "items": [
            entry("resolved", chosen=[candidate("m1")]),
            entry("not_collectable", "2024"),
        ]
    }
    ref = item_ref(resolution["items"][0])
    done = plan(resolution, NEEDS[:1], dropped=[ref])
    assert done.problems == [] and done.pins == []
    assert done.incomplete == [
        "年报 600519.SH 2025-01-01–2025-12-31：你没要",
        "年报 600519.SH 2024-01-01–2024-12-31：采不来，要本人到 knowbase 上传",
    ]
    both = [candidate("m1"), candidate("m2")]
    unclear = {"items": [entry("ambiguous", candidates=both)]}
    assert "要选一份" in plan(unclear, NEEDS[:1]).problems[0]
    anyone = {
        "items": [
            {
                **entry("not_collectable"),
                "key": {"subject": None, "doc_type": "business_rule"},
                "reason": "no_subject",
            }
        ]
    }
    assert plan(anyone, NEEDS[:1]).incomplete == ["年报：不限主体的没找到，也补采不了"]


@pytest.mark.parametrize("supply_enabled", [True, False])
def test_legacy_waiting_is_not_silently_accepted_as_incomplete(supply_enabled):
    item = entry("waiting")
    result = plan({"items": [item]}, NEEDS, supply_enabled=supply_enabled)
    assert result.problems and "重新核对采单" in result.problems[0]
    assert result.incomplete == [] and result.pins == [] and result.confirm == []
    # Explicitly dropping the item remains the user's decision.
    dropped = plan({"items": [item]}, NEEDS, dropped=[item_ref(item)])
    assert not dropped.problems and dropped.gaps[0]["required"]
    # A tracked supply operation has ended: ask about the missing result.
    ended = plan({"items": [item]}, NEEDS, after_supply=True)
    assert ended.missing and ended.missing_gaps[0]["required"]
