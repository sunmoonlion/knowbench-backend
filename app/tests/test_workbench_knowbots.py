"""分身清单变成专家包（SDD 0027 P4）：只认运行方是这里的、工具与审批不比平台松、不用数据线工具。"""

from __future__ import annotations

import hashlib
import json

import pytest
from knowbot_fixtures import KNOWBOT, manifest

from app.application.workbench.catalog import (
    SOURCE_KNOWMART,
    expert_name,
    pack_for_task,
)
from app.domain.workbench import knowbots
from app.domain.workbench.errors import KnowbotInvalid


def test_market_rejects_fixed_manifest_before_pinning():
    from app.application.workbench.catalog import pack_from_pin

    with pytest.raises(KnowbotInvalid, match="retired"):
        pack_from_pin(
            {
                "knowbot_id": KNOWBOT,
                "version": "1",
                "manifest": manifest(),
                "digest": "d",
            }
        )


def test_refs_and_profile_ids() -> None:
    assert knowbots.parse_ref(f"{KNOWBOT}@3") == (KNOWBOT, "3")
    for bad in (
        None,
        "",
        KNOWBOT,
        f"{KNOWBOT}@0",
        f"{KNOWBOT}@1@2",
        "x@1",
        f"{KNOWBOT.upper()}@1",
    ):
        assert knowbots.parse_ref(bad) is None
    pid = knowbots.profile_id(KNOWBOT, "3")
    assert knowbots.parse_profile(pid) == (KNOWBOT, "3")
    assert knowbots.is_knowbot(pid) and not knowbots.is_knowbot("FIN_REVIEW")


def test_the_digest_is_knowmarts_canonical_json_sha256() -> None:
    doc = manifest()
    body = json.dumps(doc, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    want = "sha256:" + hashlib.sha256(body.encode("utf-8")).hexdigest()
    assert knowbots.manifest_digest(doc) == want


def test_tasks_read_their_own_snapshot() -> None:
    from test_referenced_method_catalog import manifest as dynamic_manifest

    from app.application.workbench.catalog import pack_from_pin

    doc = dynamic_manifest()
    pin = pack_from_pin(
        {
            "knowbot_id": KNOWBOT,
            "version": doc["version"],
            "manifest": doc,
            "digest": "sha256:" + "a" * 64,
        }
    )
    pack = pin.pack
    task = {
        **pin.task_columns(),
        "profile_id": pack.profile_id,
        "profile_version": pack.version,
    }
    assert pack_for_task(task | {"pack_snapshot": manifest()["pack"]}) is None
    first = pack_for_task(task)
    assert first == pack
    assert pack_for_task(dict(task)) is first  # 同一份快照只解析一次
    assert pack_for_task(task | {"pack_snapshot": None}) is None
    builtin = pack_for_task({"profile_id": "FIN_REVIEW", "profile_version": None})
    assert builtin is not None and builtin.profile_id == "FIN_REVIEW"
    assert expert_name(task) == (pack.name or pack.title)
    assert (
        expert_name({"pack_source": SOURCE_KNOWMART, "pack_name": "列表行"}) == "列表行"
    )
