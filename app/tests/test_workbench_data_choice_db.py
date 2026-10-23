"""请写了数据需求声明的专家（SDD 0042 第五节）：真数据库、经网页接口。

没选数据不建委托；选了的钉进委托、签一条运行授权；同一把钥匙重发不再签；同一项目再请同一位
专家默认带上上次选的；委托结束后交还授权。
"""

from __future__ import annotations

import uuid
from typing import Any

import pytest
from knowbot_fixtures import KNOWBOT, FakeSummoner, manifest
from sqlalchemy import text
from test_workbench_ledger_db import db as db  # noqa: F401
from test_workbench_project_routes import machine, project
from test_workbench_routes import A
from test_workbench_routes import make_client as make_client  # noqa: F401

import app.interfaces.http.web.workbench_expert as expert_routes
from app.application.workbench.data_choice import DataChooser, GrantReleaser
from app.bootstrap.workbench import workbench_stores
from app.domain.workbench.errors import DataChoiceInvalid
from app.interfaces.http.middleware.auth import get_web_knowbase_token_provider

PROFILE = f"knowbot:{KNOWBOT}@1"
REPORTS = {
    "key": "annual_reports",
    "name": "年报",
    "formats": ["pdf"],
    "min_count": 1,
    "max_count": 2,
}


class Cloud:
    def __init__(self) -> None:
        self.granted: list[dict[str, Any]] = []
        self.released: list[str] = []

    async def item(self, user_token: str, item_id: str) -> dict[str, Any]:
        if item_id != "i1":
            raise DataChoiceInvalid("a chosen item is not in your cloud knowledge base")
        return {
            "name": "2024 年报.pdf",
            "media_type": "application/pdf",
            "size_bytes": 10,
            "sha256": "c" * 64,
        }

    async def grant(self, user_token, *, ref, items, ttl_seconds) -> str:  # type: ignore[no-untyped-def]
        self.granted.append({"token": user_token, "ref": ref, "items": items})
        return f"grant-{len(self.granted)}"

    async def release(self, grant_id: str) -> None:
        self.released.append(grant_id)


@pytest.fixture
def world(make_client, monkeypatch):  # noqa: F811
    fake = FakeSummoner()
    fake.give(f"sub-{A}", {**manifest(), "data_needs": [REPORTS]})
    monkeypatch.setattr(expert_routes, "knowmart_summoner", lambda: fake)
    cloud = Cloud()
    monkeypatch.setattr(
        expert_routes, "data_chooser", lambda: DataChooser(cloud=cloud, agent=None)
    )

    async def user_token() -> str:
        return "user-token"

    make_client.app.dependency_overrides[get_web_knowbase_token_provider] = lambda: (
        user_token
    )
    return cloud


def ask(key: str, data: list | None = None, expert: str = PROFILE) -> dict:
    body: dict[str, Any] = {
        "idempotency_key": key,
        "expert": expert,
        "question": "看年报",
    }
    if data is not None:
        body["data"] = data
    return body


CHOICE = [{"need": "annual_reports", "source": "cloud", "item_id": "i1"}]


async def test_an_expert_with_needs_is_asked_with_its_data(
    make_client, db, world
) -> None:  # noqa: F811
    cloud = world
    async with make_client(A) as http:
        env = await machine(http)
        pid = (await project(http, env)).json()["id"]
        assert (
            await http.post(
                "/api/workbench/knowbots/pin", json={"knowbot": f"{KNOWBOT}@1"}
            )
        ).status_code == 200
        shown = await http.get(
            f"/api/workbench/packs/{PROFILE}/data-needs", params={"project_id": pid}
        )
        assert shown.status_code == 200, shown.text
        assert [n["key"] for n in shown.json()["needs"]] == ["annual_reports"]
        assert shown.json()["last"] == []

        missing = await http.post(
            f"/api/workbench/projects/{pid}/delegations", json=ask("k-missing-1")
        )
        assert (missing.status_code, missing.json()["code"]) == (422, "data_required")
        wrong = await http.post(
            f"/api/workbench/projects/{pid}/delegations",
            json=ask("k-wrong-001", [{**CHOICE[0], "item_id": "nope"}]),
        )
        assert (wrong.status_code, wrong.json()["code"]) == (422, "data_choice_invalid")
        assert cloud.granted == []

        made = await http.post(
            f"/api/workbench/projects/{pid}/delegations",
            json=ask("k-chosen-01", CHOICE),
        )
        assert made.status_code == 201, made.text
        task_id = made.json()["task_id"]
        again = await http.post(
            f"/api/workbench/projects/{pid}/delegations",
            json=ask("k-chosen-01", CHOICE),
        )
        assert again.json()["task_id"] == task_id and again.json()["created"] is False
        assert [g["ref"] for g in cloud.granted] == ["knowbench:k-chosen-01"]
        assert cloud.granted[0]["items"] == [{"item_id": "i1", "sha256": "c" * 64}]

        shown = await http.get(
            f"/api/workbench/packs/{PROFILE}/data-needs", params={"project_id": pid}
        )
        assert shown.json()["last"] == [
            {
                "need": "annual_reports",
                "source": "cloud",
                "name": "2024 年报.pdf",
                "item_id": "i1",
            }
        ]
    async with db() as s:
        rows = (
            (
                await s.execute(
                    text("select * from workbench_task_data where task_id = :t"),
                    {"t": task_id},
                )
            )
            .mappings()
            .all()
        )
        tasks = (await s.execute(text("select count(*) from workbench_tasks"))).scalar()
    assert tasks == 1  # 没选、选错的都没留下委托
    assert [(r["source"], r["slot"], r["grant_id"]) for r in rows] == [
        ("cloud", "annual_reports", "grant-1")
    ]

    # 委托还在跑：不交还；结束了：交还一次
    releaser = GrantReleaser(workbench_stores(db), cloud)
    assert await releaser.sweep() == 0
    async with db() as s, s.begin():
        await s.execute(
            text("update workbench_tasks set state = 'CANCELLED' where id = :t"),
            {"t": task_id},
        )
    assert await releaser.sweep() == 1
    assert await releaser.sweep() == 0
    assert cloud.released == ["grant-1"]


@pytest.mark.usefixtures("builtin_packs_on_offer")
async def test_a_builtin_expert_takes_no_data(make_client, db, world) -> None:  # noqa: F811
    async with make_client(A) as http:
        env = await machine(http)
        pid = (await project(http, env)).json()["id"]
        refused = await http.post(
            f"/api/workbench/projects/{pid}/delegations",
            json=ask(f"k-{uuid.uuid4().hex[:10]}", CHOICE, expert="FIN_REVIEW"),
        )
        assert (refused.status_code, refused.json()["code"]) == (
            422,
            "data_choice_invalid",
        )
        needs = await http.get("/api/workbench/packs/FIN_REVIEW/data-needs")
        assert needs.json()["needs"] == [] and needs.json()["last"] == []
