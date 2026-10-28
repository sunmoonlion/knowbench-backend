"""清单 v2 的专家请来用数据（SDD 0043 块 6a，decisions ⑭）：真数据库、经网页接口，
knowbase、knowfeed 与模型的那一轮用替身。

请专家只建一张空清单（作者挑选的数据当场本人签、钉）；委托第 0 步模型用 request_data
提出要哪些数据，委托停在步骤之间；用户在委托页确认（能换、改、去掉、加），齐的本人签，
缺的交 knowfeed、委托「等数据」；需求单办完了重解析、凭本人那次确认续签、钉住、接着跑
（所有者 10-27 定）；有没取到的停下问用户，照现有的接着做（记「数据不全」）或取消；
委托取消了撤需求单。
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
from app.application.workbench.advisor import DATA_STEP_ID, Advisor, TurnResult
from app.application.workbench.expert_data import (
    DataWaiter,
    ExpertData,
    find_subject,
    request_data,
)
from app.bootstrap.workbench import workbench_store, workbench_stores
from app.domain.workbench.data_plan import item_ref
from app.interfaces.http.middleware.auth import get_web_knowbase_token_provider

PROFILE = f"knowbot:{KNOWBOT}@1"
SHA = {"2025": "a" * 64, "2024": "b" * 64, "2023": "d" * 64}
BOUND = {
    "material_id": "m-bound",
    "version_no": 3,
    "sha256": "e" * 64,
    "basis_ref": f"expert:{KNOWBOT}@1",
}
# 第 0 步模型提的：2025 年报（已有）、2024 年报（要去取）
PROPOSED = [
    {
        "subject": "600519.SH",
        "doc_type": "annual_report",
        "period": {"kind": "fixed", "start": "2025-01-01", "end": "2025-12-31"},
        "reading": "pages",
        "essential": True,
        "reason": "最近一年的年报",
    },
    {
        "subject": "600519.SH",
        "doc_type": "annual_report",
        "period": {"kind": "fixed", "start": "2024-01-01", "end": "2024-12-31"},
        "reading": "pages",
        "reason": "上一年比较",
    },
]


def key(year: str, subject: str = "600519.SH") -> dict[str, Any]:
    return {
        "subject": subject,
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


class FakeKnowbase:
    """knowbase 的替身：2025 年报已有，2024 年报要去取（`arrives` 决定补采后到没到）。"""

    def __init__(self) -> None:
        self.revisions: list[dict[str, Any]] = []
        self.needs: list[dict[str, Any]] = []
        self.calls: list[tuple[str, Any]] = []
        self.arrives = True

    def _items(self, fetched: bool) -> list[dict[str, Any]]:
        out = []
        for need in self.needs:
            year = str(need["period"].get("start", "2025-01-01"))[:4]
            here = year == "2025" or (fetched and self.arrives)
            out.append(
                {
                    "need_key": need["key"],
                    "key": key(year, need.get("subject") or "600519.SH"),
                    "status": "resolved" if here else "to_confirm",
                    "chosen": [candidate(year)] if here and year in SHA else [],
                    "candidates": [],
                    "reason": None,
                }
            )
        return out

    def _revise(self, fetched: bool = False) -> dict[str, Any]:
        latest = {
            "resolution_id": "res-1",
            "revision": len(self.revisions) + 1,
            "consumer": f"expert:{KNOWBOT}@1",
            "contract_version": "data-request/v1",
            "vocab_version": 2,
            "task_ref": None,
            "items": self._items(fetched),
            "created_at": "2026-10-28T00:00:00Z",
        }
        self.revisions.append(latest)
        return copy.deepcopy(latest)

    async def resolve(self, user_token: str, **kw: Any) -> dict[str, Any]:
        self.calls.append(("resolve", kw))
        assert kw["needs"] == []
        return self._revise()

    async def resolution(self, user_token, resolution_id, revision=None):
        return copy.deepcopy(self.revisions[(revision or len(self.revisions)) - 1])

    async def add_needs(self, subject, resolution_id, needs):
        self.calls.append(("add_needs", needs))
        self.needs += needs
        return self._revise()

    async def revise_needs(self, user_token, resolution_id, needs):
        self.calls.append(("revise_needs", needs))
        self.needs = list(needs)
        return self._revise()

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
        return {"grant_id": f"g-user-{len(self.calls)}"}

    async def refresh(self, subject: str, resolution_id: str) -> dict[str, Any]:
        self.calls.append(("refresh", subject))
        return self._revise(fetched=True)

    async def grant_by_confirmation(self, **kw: Any) -> dict[str, Any]:
        self.calls.append(("grant_by_confirmation", kw))
        return {"grant_id": "g-confirmed"}

    async def release_run_grant(self, grant_id: str) -> None:
        self.calls.append(("release", grant_id))

    async def register_session(self, user_token: str) -> dict[str, Any]:
        return {"session_id": "kb-s-1", "expires_at": None}


class FakeSupply:
    def __init__(self) -> None:
        self.status = "fetching"
        self.submitted: list[dict[str, Any]] = []
        self.cancelled: list[str] = []
        self.looked_up: list[str] = []

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

    async def securities(self, query: str, limit: int = 10) -> list[dict[str, Any]]:
        self.looked_up.append(query)
        return [{"subject": "600519.SH", "name": "贵州茅台"}]


class StepZero:
    """模型的那一轮（第 0 步）：照说明查主体、提数据；`propose` 为空就是不要数据。"""

    def __init__(self, db, kb, propose) -> None:  # noqa: F811
        self.db, self.kb, self.propose = db, kb, propose
        self.texts: list[str] = []

    async def run_turn(self, thread_id: str, text_: str, *, timeout: float = 0):  # noqa: ASYNC109
        self.texts.append(text_)
        if self.propose:
            async with self.db() as s:
                repo = workbench_store(s)
                task_id = text_.split('task="', 1)[1].split('"', 1)[0]
                await request_data(
                    repo, self.kb, await repo.get_task(task_id), self.propose
                )
        return TurnResult(turn_id="turn-0", final_text="提了两样", tokens={}, cost=None)


def v2_manifest() -> dict[str, Any]:
    return {**manifest(), "schema": "knowbot-manifest/v2", "bindings": [BOUND]}


@pytest.fixture
def world(make_client, monkeypatch):  # noqa: F811
    summoner = FakeSummoner()
    summoner.give(f"sub-{A}", v2_manifest())
    monkeypatch.setattr(expert_routes, "knowmart_summoner", lambda: summoner)
    state: dict[str, Any] = {"kb": FakeKnowbase(), "supply": FakeSupply()}
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


def ask(key_: str) -> dict[str, Any]:
    return {"idempotency_key": key_, "expert": PROFILE, "question": "分析茅台"}


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


async def asked(http, db, world, key_: str, propose=PROPOSED):  # noqa: F811
    """请专家、起线、跑第 0 步；回委托编号与那一轮的替身。"""
    env = await machine(http)
    pid = (await project(http, env)).json()["id"]
    pinned = await http.post(
        "/api/workbench/knowbots/pin", json={"knowbot": f"{KNOWBOT}@1"}
    )
    assert pinned.status_code == 200, pinned.text
    made = await http.post(f"/api/workbench/projects/{pid}/delegations", json=ask(key_))
    assert made.status_code == 201, made.text
    task_id = made.json()["task_id"]
    async with db() as s, s.begin():
        await s.execute(
            text("update workbench_tasks set thread_id = 'th-1' where id = :t"),
            {"t": task_id},
        )
    turn = StepZero(db, world["kb"], propose)
    state = await Advisor(workbench_stores(db), turn, records=True).drive(task_id)
    return task_id, turn, state


async def test_asking_lists_nothing_and_step_zero_proposes_the_data(
    make_client,
    db,  # noqa: F811
    world,
) -> None:
    kb, supply = world["kb"], world["supply"]
    await known(db)
    async with make_client(A) as http:
        await http.post("/api/workbench/knowbots/pin", json={"knowbot": f"{KNOWBOT}@1"})
        needs = await http.get(f"/api/workbench/packs/{PROFILE}/data-needs")
        assert (needs.json()["contract"], needs.json()["bound"]) == (
            "knowbot-manifest/v2",
            1,
        )
        assert set(needs.json()) == {"contract_version", "contract", "bound"}
        task_id, turn, state = await asked(http, db, world, "k-v2-0001")
        # 请专家：空清单、作者挑选的那一份本人签、钉住
        [(_, resolved)] = [c for c in kb.calls if c[0] == "resolve"]
        assert resolved["task_ref"] == "knowbench:k-v2-0001"
        [bound] = [c for c in kb.calls if c[0] == "run_grant"]
        assert [i["material_id"] for i in bound[1]["items"]] == ["m-bound"]
        # 第 0 步：说明里有专家的做法、问题、两个工具；提了就停在步骤之间等确认
        assert "find_subject" in turn.texts[0] and "分析茅台" in turn.texts[0]
        assert "1 item(s) of its own" in turn.texts[0]
        assert state == "WAITING"
        [task] = await rows(
            db, "select * from workbench_tasks where id = :t", t=task_id
        )
        assert (task["waiting_reason"], task["current_step"]) == ("DATA", 0)
        [attempt] = await rows(
            db,
            "select step_id, status from workbench_attempts where task_id = :t",
            t=task_id,
        )
        assert (attempt["step_id"], attempt["status"]) == (DATA_STEP_ID, "COMPLETED")
        [(_, proposed)] = [c for c in kb.calls if c[0] == "add_needs"]
        assert [(n["subject"], n["required"]) for n in proposed] == [
            ("600519.SH", True),
            ("600519.SH", False),
        ]
        # 看板：这件委托在「等你」，等的是确认数据
        [task] = await rows(
            db, "select project_id from workbench_tasks where id = :t", t=task_id
        )
        board = (
            await http.get(f"/api/workbench/projects/{task['project_id']}/board")
        ).json()
        column = {c["key"]: c["cards"] for c in board["columns"]}
        assert [c["task_id"] for c in column["waiting_you"]] == [task_id]
        assert column["waiting_you"][0]["waiting"] == {
            "who": "you",
            "what": "confirm_data",
        }
        shown = (await http.get(f"/api/workbench/tasks/{task_id}/data")).json()
        [request] = shown["requests"]
        assert (request["id"], request["status"]) == ("r1", "pending")
        assert [i["status"] for i in request["items"]] == ["resolved", "to_confirm"]
        assert request["items"][0]["ref"] == item_ref(request["items"][0])
        assert [p["basis"] for p in shown["pinned"]] == ["frozen_binding"]

        # 确认：齐的本人签、钉，缺的交 knowfeed，委托接着等
        confirmed = await http.post(
            f"/api/workbench/tasks/{task_id}/data-requests/confirm", json={}
        )
        assert confirmed.status_code == 200, confirmed.text
        assert confirmed.json()["status"] == "supplying"
    [(_, asked_for)] = [c for c in kb.calls if c[0] == "confirm"]
    assert asked_for["items"] == [{"key": key("2024"), "need_key": "req1_2"}]
    assert asked_for["task_ref"] == "knowbench:k-v2-0001"
    assert [s["confirmation_id"] for s in supply.submitted] == ["conf-1"]
    pins = await rows(
        db, "select item_id, basis from workbench_task_data order by position"
    )
    assert [p["item_id"] for p in pins] == ["m-bound", "m-2025"]
    [row] = await rows(db, "select * from workbench_task_resolution")
    assert (row["status"], row["demand_id"]) == ("waiting", "d-1")

    stores = workbench_stores(db)
    later = datetime.now(UTC) + timedelta(minutes=10)
    waiter = DataWaiter(stores, kb, supply, now=lambda: later)
    assert await waiter.sweep() == 1  # 还在取：不动
    supply.status = "ready"
    waiter.now = lambda: later + timedelta(hours=1)
    assert await waiter.sweep() == 1
    [granted] = [c for c in kb.calls if c[0] == "grant_by_confirmation"]
    assert [i["material_id"] for i in granted[1]["items"]] == ["m-2024"]
    assert granted[1]["ref"] == "knowbench:k-v2-0001"
    pins = await rows(db, "select item_id from workbench_task_data order by position")
    assert [p["item_id"] for p in pins] == ["m-bound", "m-2025", "m-2024"]
    [task] = await rows(
        db, "select state from workbench_tasks where id = :t", t=task_id
    )
    assert task["state"] == "VALIDATING"
    assert await waiter.sweep() == 0  # 再扫一遍不重复开跑


async def test_what_did_not_arrive_waits_for_the_user_to_decide(
    make_client,
    db,  # noqa: F811
    world,
) -> None:
    kb, supply = world["kb"], world["supply"]
    kb.arrives = False
    await known(db)
    async with make_client(A) as http:
        task_id, _, _ = await asked(http, db, world, "k-v2-missing")
        await http.post(
            f"/api/workbench/tasks/{task_id}/data-requests/confirm", json={}
        )
        # 在取：看板上是「等别人」，等数据服务
        [task] = await rows(
            db, "select project_id from workbench_tasks where id = :t", t=task_id
        )
        board = (
            await http.get(f"/api/workbench/projects/{task['project_id']}/board")
        ).json()
        column = {c["key"]: c["cards"] for c in board["columns"]}
        assert column["waiting_others"][0]["waiting"] == {
            "who": "data_service",
            "what": "fetch_data",
        }
        supply.status = "ready"
        waiter = DataWaiter(
            workbench_stores(db),
            kb,
            supply,
            now=lambda: datetime.now(UTC) + timedelta(hours=1),
        )
        assert await waiter.sweep() == 1
        [task] = await rows(
            db,
            "select state, waiting_reason from workbench_tasks where id = :t",
            t=task_id,
        )
        assert (task["state"], task["waiting_reason"]) == ("WAITING", "DATA")
        shown = (await http.get(f"/api/workbench/tasks/{task_id}/data")).json()
        [request] = shown["requests"]
        assert request["status"] == "missing"
        assert request["missing"] == [
            "上一年比较 600519.SH 2024-01-01–2024-12-31：没取到"
        ]
        # 照现有的接着做：记「数据不全」，委托接着跑
        going = await http.post(f"/api/workbench/tasks/{task_id}/data-requests/decline")
        assert going.json()["status"] == "declined"
        shown = (await http.get(f"/api/workbench/tasks/{task_id}/data")).json()
        assert shown["incomplete"] == request["missing"]
        [task] = await rows(
            db, "select state from workbench_tasks where id = :t", t=task_id
        )
        assert task["state"] == "VALIDATING"


async def test_the_user_changes_the_list_before_confirming(
    make_client,
    db,  # noqa: F811
    world,
) -> None:
    """确认前改：换一家、去掉一样、自己加一样；整份交 knowbase 重解析再确认。"""
    kb = world["kb"]
    await known(db)
    async with make_client(A) as http:
        task_id, _, _ = await asked(http, db, world, "k-v2-change")
        shown = (await http.get(f"/api/workbench/tasks/{task_id}/data")).json()
        [first, _second] = shown["requests"][0]["needs"]
        added = {
            "doc_type": "annual_report",
            "subject": "000858.SZ",
            "period": {"kind": "fixed", "start": "2025-01-01", "end": "2025-12-31"},
            "reading": "pages",
        }
        changed = await http.post(
            f"/api/workbench/tasks/{task_id}/data-requests/confirm",
            json={"needs": [first, added]},
        )
        assert changed.status_code == 200, changed.text
        assert changed.json()["status"] == "ready"
        [(_, revised)] = [c for c in kb.calls if c[0] == "revise_needs"]
        assert [(n["key"], n["subject"]) for n in revised] == [
            ("req1_1", "600519.SH"),
            ("r1_u2", "000858.SZ"),
        ]
        shown = (await http.get(f"/api/workbench/tasks/{task_id}/data")).json()
        assert [r["status"] for r in shown["requests"]] == ["done"]
        assert shown["requests"][0]["labels"] == ["最近一年的年报", "annual_report"]


async def test_no_data_needed_goes_straight_to_step_one_and_a_drop_is_recorded(
    make_client,
    db,  # noqa: F811
    world,
) -> None:
    await known(db)
    async with make_client(A) as http:
        task_id, _, state = await asked(http, db, world, "k-v2-none", propose=[])
        assert (
            state != "WAITING"
            or (
                await rows(
                    db,
                    "select waiting_reason from workbench_tasks where id = :t",
                    t=task_id,
                )
            )[0]["waiting_reason"]
            != "DATA"
        )
        [row] = await rows(db, "select status from workbench_task_resolution")
        assert row["status"] == "ready"
        attempts = await rows(
            db,
            "select step_id from workbench_attempts where task_id = :t order by started_at",
            t=task_id,
        )
        assert attempts[0]["step_id"] == DATA_STEP_ID


async def test_dropping_an_item_records_it_and_bad_picks_are_refused(
    make_client,
    db,  # noqa: F811
    world,
) -> None:
    await known(db)
    async with make_client(A) as http:
        task_id, _, _ = await asked(http, db, world, "k-v2-drop")
        shown = (await http.get(f"/api/workbench/tasks/{task_id}/data")).json()
        [have, want] = shown["requests"][0]["items"]
        bad = await http.post(
            f"/api/workbench/tasks/{task_id}/data-requests/confirm",
            json={"picks": {have["ref"]: {"material_id": "m-x", "version_no": 1}}},
        )
        assert bad.json()["code"] == "data_choice_invalid"
        dropped = await http.post(
            f"/api/workbench/tasks/{task_id}/data-requests/confirm",
            json={"dropped": [want["ref"]]},
        )
        assert dropped.json()["status"] == "ready", dropped.text
        assert world["supply"].submitted == []
        shown = (await http.get(f"/api/workbench/tasks/{task_id}/data")).json()
        assert shown["incomplete"] == [
            "上一年比较 600519.SH 2024-01-01–2024-12-31：你没要"
        ]


async def test_a_cancelled_task_withdraws_its_demand(
    make_client,
    db,  # noqa: F811
    world,
) -> None:
    supply = world["supply"]
    await known(db)
    async with make_client(A) as http:
        task_id, _, _ = await asked(http, db, world, "k-v2-cancel")
        await http.post(
            f"/api/workbench/tasks/{task_id}/data-requests/confirm", json={}
        )
        cancelled = await http.post(f"/api/workbench/tasks/{task_id}/cancel")
        assert cancelled.status_code in (200, 202), cancelled.text
    waiter = DataWaiter(
        workbench_stores(db),
        world["kb"],
        supply,
        now=lambda: datetime.now(UTC) + timedelta(minutes=5),
    )
    assert await waiter.sweep() == 1
    assert supply.cancelled == ["d-1"]
    [row] = await rows(db, "select status from workbench_task_resolution")
    assert row["status"] == "cancelled"


async def test_find_subject_looks_up_the_directory() -> None:
    supply = FakeSupply()
    assert await find_subject(supply, "茅台") == {
        "found": [{"subject": "600519.SH", "name": "贵州茅台"}]
    }
    assert supply.looked_up == ["茅台"]
