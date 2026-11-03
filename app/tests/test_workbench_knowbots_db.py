"""请自己的分身（SDD 0027 P4）：钉住、只给自己、按钉住的快照建委托（真数据库，假 knowmart）。"""

from __future__ import annotations

import uuid

import pytest
from knowbot_fixtures import KNOWBOT, FakeSummoner, manifest
from sqlalchemy import text
from sqlalchemy.exc import DBAPIError
from test_workbench_expert_data_db import FakeKnowbase, FakeSupply, known
from test_workbench_ledger_db import db as db  # noqa: F401
from test_workbench_project_routes import machine, project
from test_workbench_routes import A, B
from test_workbench_routes import make_client as make_client  # noqa: F401

import app.interfaces.http.web.workbench_expert as expert_routes
from app.application.workbench.expert_data import ExpertData
from app.domain.workbench.knowbots import manifest_digest
from app.interfaces.http.middleware.auth import get_web_knowbase_token_provider

PROFILE = f"knowbot:{KNOWBOT}@1"


@pytest.fixture
def knowmart(monkeypatch):
    fake = FakeSummoner()
    fake.give(f"sub-{A}", manifest())
    monkeypatch.setattr(expert_routes, "knowmart_summoner", lambda: fake)
    return fake


async def pin(http, ref=f"{KNOWBOT}@1"):
    return await http.post("/api/workbench/knowbots/pin", json={"knowbot": ref})


def ask(expert=PROFILE):
    return {
        "idempotency_key": f"k-{uuid.uuid4().hex[:12]}",
        "expert": expert,
        "question": "看看这家公司",
    }


async def test_pinning_checks_with_knowmart_as_the_session_owner(
    make_client,  # noqa: F811
    db,  # noqa: F811
    knowmart,
) -> None:
    async with make_client(A) as http:
        got = await pin(http)
        assert got.status_code == 200, got.text
        assert got.json()["pack"]["id"] == PROFILE
        assert got.json()["pack"]["source"] == "knowbot"
        assert "method_text" not in got.text  # 方法原文不出后端
        assert (await pin(http)).status_code == 200  # 再钉一次：原样
        listed = (await http.get("/api/workbench/packs")).json()["packs"]
        assert [p["id"] for p in listed if p["source"] == "knowbot"] == [PROFILE]
        assert (await http.get(f"/api/workbench/packs/{PROFILE}")).status_code == 200
        assert (await pin(http, "not-a-ref")).status_code == 404
        assert (await pin(http, f"{KNOWBOT}@2")).status_code == 404
    assert knowmart.calls[0] == (f"sub-{A}", KNOWBOT, "1")
    async with make_client(B) as http:
        # 别人的分身：knowmart 按 B 的会话查，查不到；也看不到 A 钉过的
        assert (await pin(http)).status_code == 404
        listed = (await http.get("/api/workbench/packs")).json()["packs"]
        assert not [p for p in listed if p["source"] == "knowbot"]
        assert (await http.get(f"/api/workbench/packs/{PROFILE}")).status_code == 404


async def test_removing_expert_only_hides_own_listing_and_repin_restores(
    make_client, db, knowmart
):  # noqa: F811
    async with make_client(A) as http:
        assert (await pin(http)).status_code == 200
    async with make_client(B) as other:
        assert (
            await other.delete(f"/api/workbench/packs/{PROFILE}")
        ).status_code == 404
    async with make_client(A) as http:
        assert (await http.delete(f"/api/workbench/packs/{PROFILE}")).status_code == 200
        assert (await http.delete(f"/api/workbench/packs/{PROFILE}")).status_code == 200
        assert PROFILE not in [
            p["id"] for p in (await http.get("/api/workbench/packs")).json()["packs"]
        ]
        assert (await http.get(f"/api/workbench/packs/{PROFILE}")).status_code == 200
        assert (await pin(http)).status_code == 200
        assert PROFILE in [
            p["id"] for p in (await http.get("/api/workbench/packs")).json()["packs"]
        ]
    async with db() as s:
        assert (
            await s.execute(text("SELECT count(*) FROM workbench_knowbot_pins"))
        ).scalar() == 1
        assert (
            await s.execute(text("SELECT count(*) FROM workbench_hidden_experts"))
        ).scalar() == 0


async def test_bad_or_tampered_manifests_are_not_pinned(
    make_client,  # noqa: F811
    db,  # noqa: F811
    knowmart,
) -> None:
    knowmart.digests[(f"sub-{A}", KNOWBOT, "1")] = "sha256:" + "0" * 64
    async with make_client(A) as http:
        assert (await pin(http)).status_code == 422
    knowmart.digests.clear()
    knowmart.give(f"sub-{A}", manifest(tool_policy={"command": "allow"}))
    async with make_client(A) as http:
        assert (await pin(http)).status_code == 422
    async with db() as s:
        count = (
            await s.execute(text("select count(*) from workbench_knowbot_pins"))
        ).scalar_one()
    assert count == 0


async def test_a_pinned_version_never_changes(make_client, db, knowmart) -> None:  # noqa: F811
    async with make_client(A) as http:
        assert (await pin(http)).status_code == 200
        # knowmart 同一版给出别的内容（不该发生）：不覆盖，拒绝
        changed = manifest(title="改过的")
        knowmart.give(f"sub-{A}", changed)
        assert (await pin(http)).status_code == 422
    async with db() as s:
        with pytest.raises(DBAPIError, match="never changes"):
            async with s.begin():
                await s.execute(text("update workbench_knowbot_pins set digest = 'x'"))


async def test_asking_a_pinned_knowbot_snapshots_it_on_the_task(
    make_client,  # noqa: F811
    db,  # noqa: F811
    knowmart,
    monkeypatch,
) -> None:
    # 分身都是清单 v2：请时建空清单（knowbase 的替身），要认得这个用户
    monkeypatch.setattr(
        expert_routes,
        "expert_data",
        lambda: ExpertData(FakeKnowbase(), FakeSupply(), supply_enabled=False),
    )

    async def user_token() -> str:
        return "user-token"

    make_client.app.dependency_overrides[get_web_knowbase_token_provider] = lambda: (
        user_token
    )
    await known(db)
    async with make_client(A) as http:
        env = await machine(http)
        pid = (await project(http, env)).json()["id"]
        # 没钉过不能请
        assert (
            await http.post(f"/api/workbench/projects/{pid}/delegations", json=ask())
        ).status_code == 404
        assert (await pin(http)).status_code == 200
        made = await http.post(f"/api/workbench/projects/{pid}/delegations", json=ask())
        assert made.status_code == 201, made.text
        task_id = made.json()["task_id"]
        steps = (await http.get(f"/api/workbench/tasks/{task_id}/steps")).json()
        assert steps["steps"], steps
        # 移除后正在进行的任务仍可查看；不删除或改写快照。
        assert (await http.delete(f"/api/workbench/packs/{PROFILE}")).status_code == 200
        assert (await http.get(f"/api/workbench/tasks/{task_id}/steps")).json() == steps
    async with db() as s:
        row = (
            (
                await s.execute(
                    text(
                        "select profile_id, profile_version, pack_source, pack_digest,"
                        " pack_snapshot from workbench_tasks where id = :t"
                    ),
                    {"t": task_id},
                )
            )
            .mappings()
            .one()
        )
    assert row["profile_id"] == PROFILE and row["profile_version"] == "1"
    assert row["pack_source"] == "knowmart"
    assert row["pack_digest"] == manifest_digest(manifest())
    assert row["pack_snapshot"]["profile_id"] == PROFILE
    # 来源与快照、摘要绑在一起：分身的委托不能没有快照
    async with db() as s:
        with pytest.raises(DBAPIError, match="ck_workbench_tasks_pack_source"):
            async with s.begin():
                await s.execute(
                    text(
                        "update workbench_tasks set pack_snapshot = null where id = :t"
                    ),
                    {"t": task_id},
                )
    async with make_client(B) as http:
        env = await machine(http)
        pid = (await project(http, env)).json()["id"]
        refused = await http.post(
            f"/api/workbench/projects/{pid}/delegations", json=ask()
        )
        assert refused.status_code == 404
