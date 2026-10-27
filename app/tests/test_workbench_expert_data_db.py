"""清单 v2 的专家请来用数据（SDD 0043 块 6）：真数据库、经网页接口，knowbase 与 knowfeed
用替身。

填主体 → 列清单；不带清单不建委托；待确认的随请专家确认、交 knowfeed，委托核验时进
「等数据」；需求单办完了重解析、凭本人那次确认续签、钉住、送回核验（所有者 10-27 定
「等数据后自动接着跑」）；都齐了的当场本人签；委托取消了撤需求单。
"""

from __future__ import annotations

import copy
from datetime import UTC, datetime, timedelta
from typing import Any

import pytest
from knowbot_fixtures import KNOWBOT, FakeSummoner, manifest
from sqlalchemy import text
from test_workbench_ledger_db import db as db  # noqa: F401
from test_workbench_project_routes import machine, project
from test_workbench_routes import A
from test_workbench_routes import make_client as make_client  # noqa: F401

import app.interfaces.http.web.workbench_expert as expert_routes
from app.application.workbench.advisor import Advisor
from app.application.workbench.expert_data import (
    DataWaiter,
    ExpertData,
    request_data,
)
from app.bootstrap.workbench import _pinned_of_task, workbench_store, workbench_stores
from app.domain.workbench.data_plan import item_ref
from app.interfaces.http.middleware.auth import get_web_knowbase_token_provider

PROFILE = f"knowbot:{KNOWBOT}@1"
SHA = {"2025": "a" * 64, "2024": "b" * 64, "2023": "d" * 64}
ANNUAL = {
    "key": "annual",
    "name": "年报",
    "doc_type": "annual_report",
    "period": {"kind": "latest", "count": 2},
    "formats": ["pdf"],
    "reading": "pages",
}


def key(year: str) -> dict[str, Any]:
    return {
        "subject": "600519.SH",
        "doc_type": "annual_report",
        "period_start": f"{year}-01-01",
        "period_end": f"{year}-12-31",
    }


def candidate(year: str, basis: str = "public") -> dict[str, Any]:
    return {
        "material_id": f"m-{year}",
        "basis": basis,
        "version_no": 1,
        "sha256": SHA[year],
        "content_origin": "disclosure",
        "disclosed_on": None,
        "data_format": "pdf",
        "rendition_kind": "pages",
        "rendition_no": 1,
        "rendition_sha256": "c" * 64,
        "name": f"贵州茅台{year}年年度报告.pdf",
        "media_type": "application/pdf",
        "size_bytes": 100,
    }


def item(year: str, status: str) -> dict[str, Any]:
    resolved = status == "resolved"
    return {
        "need_key": "annual",
        "key": key(year),
        "status": status,
        "chosen": [candidate(year)] if resolved else [],
        "candidates": [],
        "reason": None,
    }


class FakeKnowbase:
    def __init__(self, items: list[dict[str, Any]]) -> None:
        self.revisions = [
            {
                "resolution_id": "res-1",
                "revision": 1,
                "consumer": f"expert:{KNOWBOT}@1",
                "contract_version": "knowbot-manifest/v2",
                "vocab_version": 2,
                "task_ref": None,
                "items": items,
                "created_at": "2026-10-27T00:00:00Z",
            }
        ]
        self.calls: list[tuple[str, Any]] = []

    async def resolve(self, user_token: str, **kw: Any) -> dict[str, Any]:
        self.calls.append(("resolve", kw))
        assert kw["needs"][0]["doc_type"] == "annual_report"
        return copy.deepcopy(self.revisions[0])

    async def resolution(self, user_token, resolution_id, revision=None):
        return copy.deepcopy(self.revisions[(revision or len(self.revisions)) - 1])

    async def confirm(self, user_token: str, **kw: Any) -> dict[str, Any]:
        self.calls.append(("confirm", kw))
        return {
            "confirmation_id": "conf-1",
            "kind": "description",
            "expires_at": (datetime.now(UTC) + timedelta(days=7)).isoformat(),
            "items": [
                {"item_id": f"c{n}", "key": c["key"], "reading": "pages"}
                for n, c in enumerate(kw["items"], start=1)
            ],
            "branches": [
                {"item_id": f"c{n}", "branch": "supply"}
                for n in range(1, len(kw["items"]) + 1)
            ],
        }

    async def run_grant(self, user_token: str, **kw: Any) -> dict[str, Any]:
        self.calls.append(("run_grant", kw))
        return {"grant_id": "g-user"}

    async def refresh(self, subject: str, resolution_id: str) -> dict[str, Any]:
        self.calls.append(("refresh", subject))
        latest = copy.deepcopy(self.revisions[-1])
        latest["items"] = [item("2025", "resolved"), item("2024", "resolved")]
        latest["revision"] = len(self.revisions) + 1
        self.revisions.append(latest)
        return copy.deepcopy(latest)

    async def grant_by_confirmation(self, **kw: Any) -> dict[str, Any]:
        self.calls.append(("grant_by_confirmation", kw))
        return {"grant_id": "g-confirmed"}

    async def add_needs(self, subject, resolution_id, needs):
        self.calls.append(("add_needs", needs))
        latest = copy.deepcopy(self.revisions[-1])
        for need in needs:
            latest["items"].append(
                {
                    "need_key": need["key"],
                    "key": key("2023"),
                    "status": "resolved",
                    "chosen": [candidate("2023")],
                    "candidates": [],
                    "reason": None,
                }
            )
        latest["revision"] = len(self.revisions) + 1
        self.revisions.append(latest)
        return copy.deepcopy(latest)

    async def release_run_grant(self, grant_id: str) -> None:
        self.calls.append(("release", grant_id))

    async def register_session(self, user_token: str) -> dict[str, Any]:
        return {"session_id": "kb-s-1", "expires_at": None}


class FakeSupply:
    def __init__(self) -> None:
        self.status = "fetching"
        self.submitted: list[dict[str, Any]] = []
        self.cancelled: list[str] = []

    def configured(self) -> bool:
        return True

    async def submit(self, **kw: Any) -> dict[str, Any]:
        self.submitted.append(kw)
        return {"demand_id": f"d-{len(self.submitted)}", "status": "fetching"}

    async def get(self, demand_id: str) -> dict[str, Any]:
        return {"demand_id": demand_id, "status": self.status}

    async def cancel(self, demand_id: str) -> dict[str, Any]:
        self.cancelled.append(demand_id)
        return {"demand_id": demand_id, "status": "cancelled"}


def v2_manifest() -> dict[str, Any]:
    return {
        **manifest(),
        "schema": "knowbot-manifest/v2",
        "data_needs": [ANNUAL],
        "bindings": [],
    }


@pytest.fixture
def world(make_client, monkeypatch):  # noqa: F811
    summoner = FakeSummoner()
    summoner.give(f"sub-{A}", v2_manifest())
    monkeypatch.setattr(expert_routes, "knowmart_summoner", lambda: summoner)
    state: dict[str, Any] = {
        "kb": FakeKnowbase([item("2025", "resolved"), item("2024", "to_confirm")]),
        "supply": FakeSupply(),
    }
    monkeypatch.setattr(
        expert_routes,
        "expert_data",
        lambda: ExpertData(state["kb"], state["supply"], supply_enabled=True),
    )

    async def user_token() -> str:
        return "user-token"

    make_client.app.dependency_overrides[get_web_knowbase_token_provider] = lambda: (
        user_token
    )
    return state


def ask(key_: str, data_v2: dict | None = None) -> dict[str, Any]:
    body: dict[str, Any] = {
        "idempotency_key": key_,
        "expert": PROFILE,
        "question": "分析茅台",
    }
    if data_v2 is not None:
        body["data_v2"] = data_v2
    return body


async def known(db) -> None:  # noqa: F811
    """登录过的用户在 auth_user 里有一行（knowbase、knowfeed 认的是它的 subject）。"""
    async with db() as s:
        await s.execute(
            text(
                """insert into auth_user (id, issuer, subject, roles, scopes)
                   values (:i, 'https://casdoor.test', :s, '[]', '[]')
                   on conflict do nothing"""
            ),
            {"i": A, "s": f"sub-{A}"},
        )
        await s.commit()


async def rows(db, sql: str, **params: Any) -> list[dict[str, Any]]:  # noqa: F811
    async with db() as s:
        return [dict(r) for r in (await s.execute(text(sql), params)).mappings().all()]


async def test_missing_data_is_confirmed_fetched_and_the_task_resumes(
    make_client,
    db,  # noqa: F811
    world,
) -> None:
    kb, supply = world["kb"], world["supply"]
    await known(db)
    async with make_client(A) as http:
        env = await machine(http)
        pid = (await project(http, env)).json()["id"]
        pinned = await http.post(
            "/api/workbench/knowbots/pin", json={"knowbot": f"{KNOWBOT}@1"}
        )
        assert pinned.status_code == 200, pinned.text
        needs = await http.get(f"/api/workbench/packs/{PROFILE}/data-needs")
        assert needs.json()["contract"] == "knowbot-manifest/v2"

        listed = await http.post(
            f"/api/workbench/packs/{PROFILE}/data-plan",
            json={"subjects": ["600519.SH"]},
        )
        assert listed.status_code == 200, listed.text
        body = listed.json()
        assert (body["to_confirm"], body["supply_enabled"], body["problems"]) == (
            1,
            True,
            [],
        )
        assert kb.calls[0][1]["consumer"] == f"expert:{KNOWBOT}@1"
        assert [i["ref"] for i in body["resolution"]["items"]] == [
            item_ref(item("2025", "resolved")),
            item_ref(item("2024", "to_confirm")),
        ]

        missing = await http.post(
            f"/api/workbench/projects/{pid}/delegations", json=ask("k-v2-missing")
        )
        assert (missing.status_code, missing.json()["code"]) == (422, "data_required")

        choice = {"resolution_id": "res-1", "revision": 1}
        made = await http.post(
            f"/api/workbench/projects/{pid}/delegations", json=ask("k-v2-0001", choice)
        )
        assert made.status_code == 201, made.text
        task_id = made.json()["task_id"]
        again = await http.post(
            f"/api/workbench/projects/{pid}/delegations", json=ask("k-v2-0001", choice)
        )
        assert again.json()["created"] is False
    # 确认了待确认的那一期、交 knowfeed；还没签授权、没钉
    confirmed = [c for c in kb.calls if c[0] == "confirm"]
    assert len(confirmed) == 1
    assert confirmed[0][1]["task_ref"] == "knowbench:k-v2-0001"
    assert confirmed[0][1]["items"] == [{"key": key("2024"), "need_key": "annual"}]
    assert [s["confirmation_id"] for s in supply.submitted] == ["conf-1"]
    assert supply.submitted[0]["items"][0]["readings"] == ["pages"]
    assert not [c for c in kb.calls if c[0] == "run_grant"]
    [row] = await rows(db, "select * from workbench_task_resolution")
    assert (row["status"], row["demand_id"], row["grant_ref"]) == (
        "waiting",
        "d-1",
        "knowbench:k-v2-0001",
    )
    assert await rows(db, "select * from workbench_task_data") == []

    # 核验：线已经起好（这里直接写上），清单还在等 → 进「等数据」
    async with db() as s, s.begin():
        await s.execute(
            text("update workbench_tasks set thread_id = 'th-1' where id = :t"),
            {"t": task_id},
        )
    stores = workbench_stores(db)
    assert await Advisor(stores, object()).drive(task_id) == "WAITING"  # type: ignore[arg-type]
    [task] = await rows(db, "select * from workbench_tasks where id = :t", t=task_id)
    assert task["waiting_reason"] == "DATA"

    later = datetime.now(UTC) + timedelta(minutes=10)
    waiter = DataWaiter(
        stores, kb, supply, pinned_of=_pinned_of_task, now=lambda: later
    )
    # 还在取：不动，过一会儿再查
    assert await waiter.sweep() == 1
    [row] = await rows(db, "select * from workbench_task_resolution")
    assert row["status"] == "waiting" and row["checks"] == 1
    assert row["detail"]["demand_status"] == "fetching"

    # 办完了：重解析、凭确认续签、钉住、送回核验并排上驾驶
    supply.status = "ready"
    waiter.now = lambda: later + timedelta(hours=1)
    assert await waiter.sweep() == 1
    [granted] = [c for c in kb.calls if c[0] == "grant_by_confirmation"]
    assert granted[1]["confirmation_id"] == "conf-1"
    assert granted[1]["ref"] == "knowbench:k-v2-0001"
    assert granted[1]["revision"] == 2
    assert {i["material_id"] for i in granted[1]["items"]} == {"m-2025", "m-2024"}
    assert all(i["basis"] == "public" for i in granted[1]["items"])
    pins = await rows(db, "select * from workbench_task_data order by position")
    assert [(p["source"], p["item_id"], p["grant_id"], p["basis"]) for p in pins] == [
        ("knowbase", "m-2025", "g-confirmed", "public"),
        ("knowbase", "m-2024", "g-confirmed", "public"),
    ]
    assert pins[0]["name"] == "贵州茅台2025年年度报告.pdf"
    [row] = await rows(db, "select * from workbench_task_resolution")
    assert (row["status"], row["revision"]) == ("ready", 2)
    [task] = await rows(db, "select * from workbench_tasks where id = :t", t=task_id)
    assert task["state"] == "VALIDATING"
    drives = await rows(
        db,
        "select payload from workbench_commands where kind = 'task.drive' "
        "and payload->>'task_id' = :t",
        t=task_id,
    )
    assert any(d["payload"].get("resumed") for d in drives)
    # 再扫一遍不重复开跑
    assert await waiter.sweep() == 0


async def test_everything_ready_is_signed_by_the_user_at_once(
    make_client,
    db,  # noqa: F811
    world,
) -> None:
    kb = FakeKnowbase([item("2025", "resolved"), item("2024", "resolved")])
    world["kb"] = kb
    await known(db)
    async with make_client(A) as http:
        env = await machine(http)
        pid = (await project(http, env)).json()["id"]
        await http.post("/api/workbench/knowbots/pin", json={"knowbot": f"{KNOWBOT}@1"})
        # 换选的条目必须在候选里；必填的不能去掉
        ref_2024 = item_ref(item("2024", "resolved"))
        bad_pick = await http.post(
            f"/api/workbench/projects/{pid}/delegations",
            json=ask(
                "k-v2-bad-01",
                {
                    "resolution_id": "res-1",
                    "revision": 1,
                    "picks": {ref_2024: {"material_id": "m-x", "version_no": 1}},
                },
            ),
        )
        assert bad_pick.json()["code"] == "data_choice_invalid"
        dropped = await http.post(
            f"/api/workbench/projects/{pid}/delegations",
            json=ask(
                "k-v2-bad-02",
                {"resolution_id": "res-1", "revision": 1, "dropped": [ref_2024]},
            ),
        )
        assert "必填" in dropped.json()["detail"]
        made = await http.post(
            f"/api/workbench/projects/{pid}/delegations",
            json=ask("k-v2-ready", {"resolution_id": "res-1", "revision": 1}),
        )
        assert made.status_code == 201, made.text
    [signed] = [c for c in kb.calls if c[0] == "run_grant"]
    assert (signed[1]["process"], signed[1]["ref"]) == (
        "delegation",
        "knowbench:k-v2-ready",
    )
    assert world["supply"].submitted == []
    [row] = await rows(db, "select * from workbench_task_resolution")
    assert row["status"] == "ready"
    pins = await rows(db, "select * from workbench_task_data")
    assert {p["grant_id"] for p in pins} == {"g-user"} and len(pins) == 2


async def test_a_cancelled_task_withdraws_its_demand(
    make_client,
    db,  # noqa: F811
    world,
) -> None:
    supply = world["supply"]
    await known(db)
    async with make_client(A) as http:
        env = await machine(http)
        pid = (await project(http, env)).json()["id"]
        await http.post("/api/workbench/knowbots/pin", json={"knowbot": f"{KNOWBOT}@1"})
        made = await http.post(
            f"/api/workbench/projects/{pid}/delegations",
            json=ask("k-v2-cancel", {"resolution_id": "res-1", "revision": 1}),
        )
        task_id = made.json()["task_id"]
        cancelled = await http.post(f"/api/workbench/tasks/{task_id}/cancel")
        assert cancelled.status_code in (200, 202), cancelled.text
    waiter = DataWaiter(
        workbench_stores(db),
        world["kb"],
        supply,
        pinned_of=_pinned_of_task,
        now=lambda: datetime.now(UTC) + timedelta(minutes=5),
    )
    assert await waiter.sweep() == 1
    assert supply.cancelled == ["d-1"]
    [row] = await rows(db, "select * from workbench_task_resolution")
    assert row["status"] == "cancelled"


async def test_the_expert_asks_for_more_and_the_user_confirms_or_declines(
    make_client,
    db,  # noqa: F811
    world,
) -> None:
    """0048 第五节：专家运行中 request_data → 列进用户的确认清单，委托在边界停下；用户
    确认了追加签一条授权、追加钉住，委托接着跑；不要的照现有的接着做。"""
    kb = FakeKnowbase([item("2025", "resolved"), item("2024", "resolved")])
    world["kb"] = kb
    await known(db)
    async with make_client(A) as http:
        env = await machine(http)
        pid = (await project(http, env)).json()["id"]
        await http.post("/api/workbench/knowbots/pin", json={"knowbot": f"{KNOWBOT}@1"})
        made = await http.post(
            f"/api/workbench/projects/{pid}/delegations",
            json=ask("k-v2-asks", {"resolution_id": "res-1", "revision": 1}),
        )
        task_id = made.json()["task_id"]
        async with db() as s:
            repo = workbench_store(s)
            task = await repo.get_task(task_id)
            listed = await request_data(
                repo,
                kb,
                task,
                [
                    {
                        "doc_type": "annual_report",
                        "period": {
                            "kind": "fixed",
                            "start": "2023-01-01",
                            "end": "2023-12-31",
                        },
                        "reading": "pages",
                        "reason": "再要 2023 年报比较",
                    }
                ],
            )
        assert listed["status"] == "listed" and listed["request"] == "r1"
        [(_, asked)] = [c for c in kb.calls if c[0] == "add_needs"]
        assert asked[0]["key"] == "req1_1" and asked[0]["required"] is False
        async with db() as s, s.begin():
            await s.execute(
                text("update workbench_tasks set thread_id = 'th-1' where id = :t"),
                {"t": task_id},
            )
        assert (
            await Advisor(workbench_stores(db), object()).drive(task_id)  # type: ignore[arg-type]
            == "WAITING"
        )
        shown = (await http.get(f"/api/workbench/tasks/{task_id}/data")).json()
        assert shown["listed"] is True
        assert [(r["id"], r["status"]) for r in shown["requests"]] == [
            ("r1", "pending")
        ]
        assert len(shown["pinned"]) == 2

        confirmed = await http.post(
            f"/api/workbench/tasks/{task_id}/data-requests/confirm"
        )
        assert confirmed.status_code == 200, confirmed.text
        assert confirmed.json()["status"] == "ready" and confirmed.json()["added"] == 1
        grants = [c for c in kb.calls if c[0] == "run_grant"]
        assert [i["material_id"] for i in grants[-1][1]["items"]] == ["m-2023"]
        assert grants[-1][1]["ref"] == "knowbench:k-v2-asks"
        pins = await rows(
            db, "select item_id from workbench_task_data order by position"
        )
        assert [p["item_id"] for p in pins] == ["m-2025", "m-2024", "m-2023"]
        [task] = await rows(
            db, "select state from workbench_tasks where id = :t", t=task_id
        )
        assert task["state"] == "VALIDATING"
        again = await http.post(f"/api/workbench/tasks/{task_id}/data-requests/confirm")
        assert again.json()["code"] == "data_choice_invalid"

        # 再要一次、这回不要：委托照现有的接着做
        async with db() as s:
            repo = workbench_store(s)
            await request_data(
                repo,
                kb,
                await repo.get_task(task_id),
                [{"doc_type": "interim_announcement", "reason": "近一年回购"}],
            )
        declined = await http.post(
            f"/api/workbench/tasks/{task_id}/data-requests/decline"
        )
        assert declined.json()["status"] == "declined"
        shown = (await http.get(f"/api/workbench/tasks/{task_id}/data")).json()
        assert [r["status"] for r in shown["requests"]] == ["done", "declined"]
