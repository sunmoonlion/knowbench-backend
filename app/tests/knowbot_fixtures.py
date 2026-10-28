"""分身测试共用（SDD 0027 P4）：合格的分身清单 v2（以烟测专家包为底）与假 knowmart。"""

from __future__ import annotations

import copy
from typing import Any

from app.domain.workbench.errors import NoSuchExpert
from app.domain.workbench.knowbots import manifest_digest
from app.domain.workbench.packs import SMOKE

KNOWBOT = "0b8a6c3e-1f2d-4c5b-9a7e-6d5c4b3a2f10"


def manifest(version: str = "1", **pack_overrides: Any) -> dict[str, Any]:
    pack = SMOKE.model_dump(mode="json")
    for key in (
        "profile_id",
        "internal",
        "status",
        "preparing_note",
        "name",
        "tagline",
    ):
        pack.pop(key, None)
    pack.update({"pack_id": "my-method", "version": version, "title": "我的方法"})
    pack.update(pack_overrides)
    return {
        "schema": "knowbot-manifest/v2",
        "name": "我的方法",
        "tagline": "按我的口径看一家公司",
        "version": version,
        "runtime": "knowbench",
        "pack": pack,
        "bindings": [],
        "rule_sources": [{"rule": "口径", "source": "user_text", "ref": "第 4 问"}],
    }


class FakeSummoner:
    """按（所有者, 分身, 版本）给清单；没有的就是 knowmart 的 404。"""

    def __init__(self) -> None:
        self.manifests: dict[tuple[str, str, str], dict[str, Any]] = {}
        self.digests: dict[tuple[str, str, str], str] = {}
        self.calls: list[tuple[str, str, str]] = []

    def give(self, owner_subject: str, doc: dict[str, Any], knowbot_id: str = KNOWBOT):
        self.manifests[(owner_subject, knowbot_id, doc["version"])] = doc

    async def summon(
        self, *, owner_subject: str, knowbot_id: str, version: str
    ) -> dict[str, Any]:
        key = (owner_subject, knowbot_id, version)
        self.calls.append(key)
        if key not in self.manifests:
            raise NoSuchExpert("no such knowbot", knowbot=knowbot_id)
        doc = copy.deepcopy(self.manifests[key])
        return {
            "manifest": doc,
            "digest": self.digests.get(key, manifest_digest(doc)),
            "name": doc["name"],
            "tagline": doc["tagline"],
        }
