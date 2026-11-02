"""清单 v2 的专家请来用数据（SDD 0043 块 6a，decisions ⑭）：真数据库、经网页接口，
knowbase、knowfeed 与模型的那一轮用替身。

请专家只建一张空清单（作者挑选的数据当场本人签、钉）；委托第 0 步模型用 request_data
提出要哪些数据，委托停在步骤之间；用户在委托页确认（能换、改、去掉、加），齐的本人签，
缺的交 knowfeed、委托「等数据」；需求单办完了重解析、凭本人那次确认续签、钉住、接着跑
（所有者 10-27 定）；有没取到的停下问用户，照现有的接着做（记「数据不全」）或取消；
委托取消了撤需求单。
"""

from __future__ import annotations

import asyncio
import copy
import re
import uuid
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from typing import Any

import pytest
from httpx import ASGITransport, AsyncClient
from knowbot_fixtures import KNOWBOT, FakeSummoner, manifest
from sqlalchemy import text
from test_workbench_ledger_db import db as db  # noqa: F401
from test_workbench_project_routes import machine, project
from test_workbench_routes import A
from test_workbench_routes import make_client as make_client  # noqa: F401

import app.interfaces.endpoints.knowfeed_status_routes as status_routes
import app.interfaces.http.web.workbench_expert as expert_routes
from app.application.errors.exceptions import ServiceUnavailableError
from app.application.workbench.advisor import DATA_STEP_ID, Advisor, TurnResult
from app.application.workbench.expert_data import (
    CLAIM_LEASE,
    DataWaiter,
    ExpertData,
    find_subject,
    request_data,
)
from app.bootstrap.workbench import workbench_store, workbench_stores
from app.domain.workbench.data_plan import item_ref
from app.domain.workbench.errors import DataChoiceInvalid
from app.infrastructure.security.relation_auth import (
    ServiceCaller,
    require_supply_status_service,
)
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
        # 已有的那一样另有几份候选（用户能换）
        self.alternatives: list[dict[str, Any]] = []
        self.preference_fails = False
        # 续签时：抛这个（knowbase 拒签），或者先做这件事（模拟委托在续签时结束）
        self.grant_error: Exception | None = None
        self.on_grant: Any = None

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
                    "candidates": list(self.alternatives) if year == "2025" else [],
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
        if self.grant_error is not None:
            raise self.grant_error
        if self.on_grant is not None:
            await self.on_grant()
        return {"grant_id": "g-confirmed"}

    async def release_run_grant(self, grant_id: str) -> None:
        self.calls.append(("release", grant_id))

    async def register_session(self, user_token: str) -> dict[str, Any]:
        return {"session_id": "kb-s-1", "expires_at": None}

    async def refresh_mine(self, user_token: str, resolution_id: str) -> dict:
        self.calls.append(("refresh_mine", resolution_id))
        return self._revise(fetched=True)

    async def save_preference(self, user_token: str, **kw: Any) -> dict[str, Any]:
        self.calls.append(("save_preference", kw))
        if self.preference_fails:
            raise RuntimeError("knowbase has no preference writes yet")
        return {"key_id": "k", "material_ids": kw["material_ids"]}


class FakeSupply:
    def __init__(self) -> None:
        self.status = "fetching"
        self.items: list[dict[str, Any]] = []
        self.submitted: list[dict[str, Any]] = []
        self.cancelled: list[str] = []
        self.looked_up: list[str] = []
        self.asked: list[dict[str, Any]] = []
        # 预切合：None 是调不通
        self.answer: dict[str, Any] | None = {
            "status": "available",
            "reason": None,
            "license": "self",
            "eta_seconds": 300,
            "readings_missing": [],
            "candidates": [],
        }

    def configured(self) -> bool:
        return True

    async def prematch(
        self, items: list[dict[str, Any]], conditions: dict[str, Any]
    ) -> dict[str, Any]:
        self.asked.append({"items": items, "conditions": conditions})
        if self.answer is None:
            raise ServiceUnavailableError("down", code="knowfeed_unavailable")
        return {
            "contract": "knowfeed-supply-prematch/v1",
            "items": [{"ref": i["ref"], **self.answer} for i in items],
        }

    async def submit(self, **kw: Any) -> dict[str, Any]:
        self.submitted.append(kw)
        return {"demand_id": f"d-{len(self.submitted)}", "status": "fetching"}

    async def get(self, demand_id: str) -> dict[str, Any]:
        return {"demand_id": demand_id, "status": self.status, "items": self.items}

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


@pytest.mark.parametrize("cancelled", [False, True])
async def test_proposal_return_preserves_new_detail_and_respects_cancellation(
    make_client,
    db,
    world,
    monkeypatch,
    cancelled,  # noqa: F811
):
    from app.application.workbench.ledger import Ledger

    await known(db)
    async with make_client(A) as http:
        task_id, _, _ = await asked(http, db, world, "k-proposal-race")
    kb = world["kb"]
    original = kb.add_needs

    async def concurrent(subject, resolution_id, needs):
        fresh = await original(subject, resolution_id, needs)
        # A separate connection commits while the remote request is in flight.
        async with db() as other:
            repo = workbench_store(other)
            async with repo.transaction():
                await repo.set_resolution_prematch(task_id, {"new": "keep"})
                if cancelled:
                    await Ledger(repo).request_cancel(task_id, owner_actor_id=A)
        return fresh

    monkeypatch.setattr(kb, "add_needs", concurrent)
    async with db() as session:
        repo = workbench_store(session)
        task = await repo.get_task(task_id)
        if cancelled:
            with pytest.raises(DataChoiceInvalid, match="no longer accepting"):
                await request_data(repo, kb, task, PROPOSED[:1])
        else:
            result = await request_data(repo, kb, task, PROPOSED[:1])
            assert result["request"] == "r2"
        row = await repo.get_task_resolution(task_id)
        assert row["detail"]["prematch"] == {"new": "keep"}
        assert [r["id"] for r in row["detail"]["requests"]] == (
            ["r1"] if cancelled else ["r1", "r2"]
        )
    assert not world["supply"].submitted


async def test_parallel_author_test_proposals_have_distinct_ids(
    make_client,
    db,
    world,  # noqa: F811
):
    await known(db)
    async with make_client(A) as http:
        task_id, _, _ = await asked(http, db, world, "k-test-proposal-race")
    async with db() as session:
        await session.execute(
            text(
                "update workbench_task_resolution set resolution_id=null where task_id=:t"
            ),
            {"t": task_id},
        )
        await session.commit()
    ready = asyncio.Barrier(2)

    async def propose():
        async with db() as session:
            repo = workbench_store(session)
            task = await repo.get_task(task_id)
            await ready.wait()
            return await request_data(repo, None, task, PROPOSED[:1])  # type: ignore[arg-type]

    results = await asyncio.wait_for(asyncio.gather(propose(), propose()), 10)
    assert {r["request"] for r in results} == {"r2", "r3"}
    async with db() as session:
        row = await workbench_store(session).get_task_resolution(task_id)
        requests = row["detail"]["requests"]
        assert [r["id"] for r in requests] == ["r1", "r2", "r3"]
        assert [r["needs"][0]["key"] for r in requests] == [
            "req1_1",
            "req2_1",
            "req3_1",
        ]


async def test_provider_revision_conflict_is_not_reported_as_listed(
    make_client,
    db,
    world,
    monkeypatch,  # noqa: F811
):
    from app.infrastructure.external.knowbase_client import KnowbaseRefused

    await known(db)
    async with make_client(A) as http:
        task_id, _, _ = await asked(http, db, world, "k-provider-conflict")

    async def conflict(*args):
        raise KnowbaseRefused(409, "resolution_changed")

    monkeypatch.setattr(world["kb"], "add_needs", conflict)
    async with db() as session:
        repo = workbench_store(session)
        with pytest.raises(DataChoiceInvalid, match="reload it and retry"):
            await request_data(
                repo, world["kb"], await repo.get_task(task_id), PROPOSED
            )
        row = await repo.get_task_resolution(task_id)
        assert [r["id"] for r in row["detail"]["requests"]] == ["r1"]


async def test_parallel_remote_proposals_do_not_overwrite_a_successful_request(
    make_client,
    db,
    world,
    monkeypatch,  # noqa: F811
):
    await known(db)
    async with make_client(A) as http:
        task_id, _, _ = await asked(http, db, world, "k-remote-proposal-race")
    ready = asyncio.Barrier(2)
    original = world["kb"].add_needs

    async def permissive_provider(subject, resolution_id, needs):
        # Even an older provider returning success to both must not overwrite local state.
        result = await original(subject, resolution_id, needs)
        await ready.wait()
        return result

    monkeypatch.setattr(world["kb"], "add_needs", permissive_provider)

    async def propose(reason):
        async with db() as session:
            repo = workbench_store(session)
            return await request_data(
                repo,
                world["kb"],
                await repo.get_task(task_id),
                [{**PROPOSED[0], "reason": reason}],
            )

    results = await asyncio.wait_for(
        asyncio.gather(propose("one"), propose("two"), return_exceptions=True), 10
    )
    assert sum(isinstance(r, DataChoiceInvalid) for r in results) == 1
    assert sum(isinstance(r, dict) and r["request"] == "r2" for r in results) == 1
    async with db() as session:
        row = await workbench_store(session).get_task_resolution(task_id)
        assert [r["id"] for r in row["detail"]["requests"]] == ["r1", "r2"]
        assert row["detail"]["requests"][1]["labels"] in (["one"], ["two"])


@pytest.mark.parametrize(
    "outcome",
    [
        {"error": "HTTP 400: tool call result missing", "final_text": "先查主体"},
        {"error": "HTTP 400: tool call result missing", "final_text": None},
        {"timed_out": True, "error": "turn timed out", "final_text": "正在查"},
        {"interrupted": True, "final_text": "正在查"},
        {"stopped_by_user": True, "final_text": "暂停"},
    ],
)
@pytest.mark.parametrize("propose", [[], PROPOSED])
async def test_incomplete_data_turn_never_advances_even_with_text_or_proposals(
    make_client,
    db,
    world,
    monkeypatch,
    outcome,
    propose,
):
    original = StepZero.run_turn

    async def failed(self, thread_id, text_, *, timeout=0):  # noqa: ASYNC109
        await original(self, thread_id, text_, timeout=timeout)
        return TurnResult(
            turn_id="failed-turn", tokens={}, cost=Decimal("0.01"), **outcome
        )

    monkeypatch.setattr(StepZero, "run_turn", failed)
    await known(db)
    async with make_client(A) as http:
        task_id, _, state = await asked(http, db, world, "k-failed-data-turn", propose)
    assert state == "FAILED"
    [task] = await rows(db, "select * from workbench_tasks where id=:t", t=task_id)
    assert task["current_step"] == 0
    [attempt] = await rows(
        db, "select * from workbench_attempts where task_id=:t", t=task_id
    )
    assert attempt["status"] == "FAILED"
    async with db() as s:
        repo = workbench_store(s)
        resolution = await repo.get_task_resolution(task_id)
        assert resolution["status"] != "ready"
        events = await repo.list_events(session_id=str(task["session_id"]))
        assert not any(e["type"] == "step/accepted" for e in events)
    assert not world["supply"].submitted


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
        # 运行授权的 ref 服务端生成（独立审 10-30 第二轮）：不用客户端的幂等钥匙拼
        ref = resolved["task_ref"]
        assert re.fullmatch(r"knowbench:run:[0-9a-f-]{36}", ref)
        assert "k-v2-0001" not in ref
        [bound] = [c for c in kb.calls if c[0] == "run_grant"]
        assert [i["material_id"] for i in bound[1]["items"]] == ["m-bound"]
        assert bound[1]["ref"] == ref
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
        current = await http.get(f"/api/workbench/tasks/{task_id}/steps")
        assert current.status_code == 200
        assert current.json()["now"]["doing"]["code"] == "supplying"
        assert "已确认" in current.json()["now"]["doing"]["text"]
    [(_, asked_for)] = [c for c in kb.calls if c[0] == "confirm"]
    assert asked_for["items"] == [{"key": key("2024"), "need_key": "req1_2"}]
    assert asked_for["task_ref"] == ref
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
    supply.items = [
        {
            "item_id": "i-failed",
            "key": key("2024"),
            "readings": ["table"],
            "status": "failed",
            "reason": "invalid_request",
            "receipt": {"private": "must-not-leak"},
        }
    ]
    assert await waiter.sweep() == 1  # 还在取：不动，但部分失败已可见
    async with make_client(A) as http:
        response = await http.get(f"/api/workbench/tasks/{task_id}/data")
        progress = response.json()["supply_progress"]
        assert progress["checked_at"] == later.isoformat()
        assert progress["items"][0]["status"] == "failed"
        assert progress["items"][0]["reason"] == "invalid_request"
        assert "receipt" not in progress["items"][0]
        assert "must-not-leak" not in response.text
    supply.status = "ready"
    waiter.now = lambda: later + timedelta(hours=1)
    assert await waiter.sweep() == 1
    [granted] = [c for c in kb.calls if c[0] == "grant_by_confirmation"]
    assert [i["material_id"] for i in granted[1]["items"]] == ["m-2024"]
    assert granted[1]["ref"] == ref
    [row] = await rows(db, "select grant_ref from workbench_task_resolution")
    assert row["grant_ref"] == ref
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


def status_event(demand_id: str, status: str, owner: str = f"sub-{A}") -> dict:
    """knowfeed 推来的一条需求状态（契约 knowfeed-supply-status/v1）。"""
    now = datetime.now(UTC).isoformat()
    return {
        "contract": "knowfeed-supply-status/v1",
        "event_id": f"ev-{demand_id}-{status}",
        "demand_id": demand_id,
        "app": "knowbench",
        "owner": owner,
        "task_ref": "knowbench:k",
        "status": status,
        "items": [
            {
                "item_id": "i1",
                "confirmation_item": "c1",
                "status": status,
                "updated_at": now,
            }
        ],
        "occurred_at": now,
    }


async def test_knowfeed_pushing_a_finished_demand_resumes_the_task_at_once(
    make_client,
    db,  # noqa: F811
    world,
) -> None:
    """SDD 0045 第 2.5 节：需求单办完了 knowfeed 推过来，等数据的委托不等到点、马上重解析
    接着跑；还在办的、别人的、不是我们提的都不动；推送重发不重复开跑。"""
    kb, supply = world["kb"], world["supply"]
    await known(db)
    async with make_client(A) as http:
        task_id, _, _ = await asked(http, db, world, "k-v2-pushed")
        confirmed = await http.post(
            f"/api/workbench/tasks/{task_id}/data-requests/confirm", json={}
        )
        assert confirmed.json()["status"] == "supplying"
    app = make_client.app
    app.dependency_overrides[require_supply_status_service] = lambda: ServiceCaller(
        caller="knowfeed",
        principal=None,  # type: ignore[arg-type]
    )
    waiter = DataWaiter(workbench_stores(db), kb, supply)
    app.dependency_overrides[status_routes.data_waiter] = lambda: waiter
    url = "/api/internal/v1/knowfeed/demand-status"
    async with AsyncClient(
        transport=ASGITransport(app=app), base_url="http://127.0.0.1"
    ) as h:
        still = await h.post(url, json=status_event("d-1", "fetching"))
        assert still.status_code == 202, still.text
        assert still.json()["claimed"] == 0  # 还在办：照旧等
        supply.status = "ready"
        theirs = await h.post(url, json=status_event("d-1", "ready", owner="sub-x"))
        assert theirs.json()["claimed"] == 0  # 单号对得上、人对不上：不碰
        knowlabs = status_event("d-1", "ready") | {"app": "knowlab"}
        assert (await h.post(url, json=knowlabs)).status_code == 422
        bad = status_event("d-1", "ready") | {"extra": 1}
        assert (await h.post(url, json=bad)).status_code == 422
        done = await h.post(url, json=status_event("d-1", "ready"))
        assert done.status_code == 202, done.text
        assert done.json() == {"event_id": "ev-d-1-ready", "claimed": 1}
        [task] = await rows(
            db, "select state from workbench_tasks where id = :t", t=task_id
        )
        assert task["state"] == "VALIDATING"  # 没等到点，已经接着跑
        [row] = await rows(db, "select status from workbench_task_resolution")
        assert row["status"] == "ready"
        again = await h.post(url, json=status_event("d-1", "ready"))
        assert again.json()["claimed"] == 0  # 重发：不重复开跑
    assert len([c for c in kb.calls if c[0] == "grant_by_confirmation"]) == 1


async def test_a_claimed_task_left_unchecked_is_picked_up_by_the_sweep_later(
    make_client,
    db,  # noqa: F811
    world,
) -> None:
    """推送领下了、马上查没办成（进程停了）：领的那段时间过了，到点查的线照旧接着办。"""
    kb, supply = world["kb"], world["supply"]
    await known(db)
    async with make_client(A) as http:
        task_id, _, _ = await asked(http, db, world, "k-v2-lease")
        await http.post(
            f"/api/workbench/tasks/{task_id}/data-requests/confirm", json={}
        )
    now = datetime.now(UTC) + timedelta(minutes=1)
    waiter = DataWaiter(workbench_stores(db), kb, supply, now=lambda: now)
    claimed = await waiter.claim("d-1", owner_subject=f"sub-{A}")
    assert len(claimed) == 1
    assert await waiter.sweep() == 0  # 领着的时候到点查的线不碰
    supply.status = "ready"
    waiter.now = lambda: now + CLAIM_LEASE + timedelta(seconds=1)
    assert await waiter.sweep() == 1
    [row] = await rows(db, "select status from workbench_task_resolution")
    assert row["status"] == "ready"


async def test_find_subject_looks_up_the_directory() -> None:
    supply = FakeSupply()
    assert await find_subject(supply, "茅台") == {
        "found": [{"subject": "600519.SH", "name": "贵州茅台"}]
    }
    assert supply.looked_up == ["茅台"]


async def test_the_list_shows_the_prematch_and_the_confirmation_keeps_it(
    make_client,
    db,  # noqa: F811
    world,
) -> None:
    """0043 第 3.3 节：待确认的几项先问 knowfeed（能不能取、许可档、预计多久），面板上显示；
    确认时回包连同可接受的条件存进 knowbase 确认的供给条件快照。"""
    kb, supply = world["kb"], world["supply"]
    await known(db)
    async with make_client(A) as http:
        task_id, _, _ = await asked(http, db, world, "k-v2-prematch")
        shown = (await http.get(f"/api/workbench/tasks/{task_id}/data")).json()
        [request] = shown["requests"]
        have, want = request["items"]
        assert "prematch" not in have  # 已有的不问
        assert want["prematch"]["status"] == "available"
        assert (want["prematch"]["license"], want["prematch"]["eta_seconds"]) == (
            "self",
            300,
        )
        assert request["eta_seconds"] == 300
        [question] = supply.asked
        assert question["conditions"] == {"min_license": "self", "channels": []}
        assert question["items"] == [
            {
                "ref": want["ref"],
                "key": key("2024") | {"edition": "full", "topics": []},
                "readings": ["pages"],
            }
        ]
        confirmed = await http.post(
            f"/api/workbench/tasks/{task_id}/data-requests/confirm", json={}
        )
        assert confirmed.json()["status"] == "supplying", confirmed.text
    # 确认时不再同步问，快照用面板那次问到的；每项带上问的描述键
    assert len(supply.asked) == 1
    [(_, asked_for)] = [c for c in kb.calls if c[0] == "confirm"]
    assert asked_for["supply"] == {
        "conditions": {"min_license": "self", "channels": []},
        "prematch": {
            "contract": "knowfeed-supply-prematch/v1",
            "items": [
                {
                    "ref": want["ref"],
                    **supply.answer,
                    "key": key("2024") | {"edition": "full", "topics": []},
                }
            ],
        },
    }


async def test_without_knowfeed_the_list_is_shown_without_estimates(
    make_client,
    db,  # noqa: F811
    world,
) -> None:
    """knowfeed 调不通：清单照常列、照常能确认，只是没有预计；快照里只有条件。"""
    kb, supply = world["kb"], world["supply"]
    supply.answer = None
    await known(db)
    async with make_client(A) as http:
        task_id, _, _ = await asked(http, db, world, "k-v2-no-prematch")
        shown = (await http.get(f"/api/workbench/tasks/{task_id}/data")).json()
        [request] = shown["requests"]
        assert [i["status"] for i in request["items"]] == ["resolved", "to_confirm"]
        assert all("prematch" not in i for i in request["items"])
        assert request["eta_seconds"] is None
        confirmed = await http.post(
            f"/api/workbench/tasks/{task_id}/data-requests/confirm", json={}
        )
        assert confirmed.json()["status"] == "supplying", confirmed.text
    [(_, asked_for)] = [c for c in kb.calls if c[0] == "confirm"]
    assert asked_for["supply"] == {
        "conditions": {"min_license": "self", "channels": []}
    }


async def test_with_supply_off_knowfeed_is_not_asked(
    make_client,
    db,  # noqa: F811
    world,
    monkeypatch,
) -> None:
    """补采开关关着：待确认的一律「暂不能补采」，不问预切合（显示预计反倒误导）。"""
    supply = world["supply"]
    monkeypatch.setattr(
        expert_routes,
        "expert_data",
        lambda: ExpertData(world["kb"], supply, supply_enabled=False),
    )
    await known(db)
    async with make_client(A) as http:
        task_id, _, _ = await asked(http, db, world, "k-v2-supply-off")
        shown = (await http.get(f"/api/workbench/tasks/{task_id}/data")).json()
        assert shown["requests"][0]["eta_seconds"] is None
    assert supply.asked == []


async def test_switching_to_another_copy_is_written_back_as_a_preference(
    make_client,
    db,  # noqa: F811
    world,
) -> None:
    """0043 第 3.5 节：已定的换成候选里的另一份，确认后凭本人令牌记成偏好（用户 × 使用者 ×
    描述 → 资料）；照列出的确认不写；写不成不挡确认。"""
    kb = world["kb"]
    kb.alternatives = [
        candidate("2025") | {"material_id": "m-2025-mine", "basis": "display"}
    ]
    kb.preference_fails = True
    await known(db)
    async with make_client(A) as http:
        task_id, _, _ = await asked(http, db, world, "k-v2-prefer")
        shown = (await http.get(f"/api/workbench/tasks/{task_id}/data")).json()
        have, want = shown["requests"][0]["items"]
        done = await http.post(
            f"/api/workbench/tasks/{task_id}/data-requests/confirm",
            json={
                "picks": {have["ref"]: {"material_id": "m-2025-mine", "version_no": 1}},
                "dropped": [want["ref"]],
            },
        )
        assert done.json()["status"] == "ready", done.text
    [(_, saved)] = [c for c in kb.calls if c[0] == "save_preference"]
    assert saved == {
        # 偏好按专家记、不带版本（第三轮第 8 条）
        "consumer": f"expert:{KNOWBOT}",
        "key": key("2025"),
        "material_ids": ["m-2025-mine"],
    }
    pins = await rows(db, "select item_id from workbench_task_data order by position")
    assert [p["item_id"] for p in pins] == ["m-bound", "m-2025-mine"]


async def test_confirming_as_listed_writes_no_preference(
    make_client,
    db,  # noqa: F811
    world,
) -> None:
    kb = world["kb"]
    kb.alternatives = [candidate("2025") | {"material_id": "m-2025-mine"}]
    await known(db)
    async with make_client(A) as http:
        task_id, _, _ = await asked(http, db, world, "k-v2-as-listed")
        shown = (await http.get(f"/api/workbench/tasks/{task_id}/data")).json()
        have, _want = shown["requests"][0]["items"]
        # 选回 knowbase 挑的那一份，也不算换
        await http.post(
            f"/api/workbench/tasks/{task_id}/data-requests/confirm",
            json={"picks": {have["ref"]: {"material_id": "m-2025", "version_no": 1}}},
        )
    assert [c for c in kb.calls if c[0] == "save_preference"] == []


async def test_the_push_and_the_sweep_never_resume_the_same_task_twice(
    make_client,
    db,  # noqa: F811
    world,
) -> None:
    """独立审 10-30 第 5 条：到点查的线已经挑出这件、推送同时领下了它；到点查的线再领就
    领不到，只有一边接着跑、只续签一次；查完放开领用期限。"""
    kb, supply = world["kb"], world["supply"]
    await known(db)
    async with make_client(A) as http:
        task_id, _, _ = await asked(http, db, world, "k-v2-race")
        await http.post(
            f"/api/workbench/tasks/{task_id}/data-requests/confirm", json={}
        )
    supply.status = "ready"
    later = datetime.now(UTC) + timedelta(minutes=5)
    stores = workbench_stores(db)
    waiter = DataWaiter(stores, kb, supply, now=lambda: later)
    async with stores() as repo:
        due = await repo.due_task_resolutions(later)  # 到点查的线先挑出来
    assert [str(r["task_id"]) for r in due] == [task_id]
    pushed = await waiter.claim("d-1", owner_subject=f"sub-{A}")  # 推送同时领下
    assert len(pushed) == 1
    async with stores() as repo:
        async with repo.transaction():
            assert (
                await repo.claim_task_resolution(
                    task_id, now=later, until=later + CLAIM_LEASE
                )
                is None
            )
    assert await waiter.sweep() == 0  # 领着的时候到点查的线让开
    assert await waiter.claim("d-1", owner_subject=f"sub-{A}") == []  # 推送重发也让开
    assert await waiter.check_now(pushed) == 1
    assert len([c for c in kb.calls if c[0] == "grant_by_confirmation"]) == 1
    [row] = await rows(
        db, "select status, checking_until from workbench_task_resolution"
    )
    assert (row["status"], row["checking_until"]) == ("ready", None)


async def _waiting(http, db, world, key_: str) -> str:  # noqa: F811
    task_id, _, _ = await asked(http, db, world, key_)
    await http.post(f"/api/workbench/tasks/{task_id}/data-requests/confirm", json={})
    world["supply"].status = "ready"
    return task_id


async def test_a_task_that_ends_while_renewing_hands_the_new_grant_back(
    make_client,
    db,  # noqa: F811
    world,
) -> None:
    """独立审 10-30 第二轮第 3 条：续签的时候委托结束了（交还那条线可能已经按 ref 交还过）：
    刚签的那条不钉、马上交还；knowbase 对已交还的 ref 拒签，当终态收掉。"""
    kb, supply = world["kb"], world["supply"]
    await known(db)
    async with make_client(A) as http:
        task_id = await _waiting(http, db, world, "k-v2-ended")

    async def end_it() -> None:
        async with db() as s, s.begin():
            await s.execute(
                text("update workbench_tasks set state = 'CANCELLED' where id = :t"),
                {"t": task_id},
            )

    kb.on_grant = end_it
    later = datetime.now(UTC) + timedelta(minutes=5)
    waiter = DataWaiter(workbench_stores(db), kb, supply, now=lambda: later)
    assert await waiter.sweep() == 1
    assert ("release", "g-confirmed") in kb.calls
    pins = await rows(db, "select item_id from workbench_task_data")
    assert "m-2024" not in [p["item_id"] for p in pins]
    [row] = await rows(
        db, "select status, checking_until from workbench_task_resolution"
    )
    assert (row["status"], row["checking_until"]) == ("cancelled", None)


async def test_knowbase_refusing_a_released_ref_is_taken_as_the_end(
    make_client,
    db,  # noqa: F811
    world,
) -> None:
    from app.infrastructure.external.knowbase_client import KnowbaseRefused

    kb, supply = world["kb"], world["supply"]
    await known(db)
    async with make_client(A) as http:
        await _waiting(http, db, world, "k-v2-released")
    kb.grant_error = KnowbaseRefused(409, "ref_released")
    later = datetime.now(UTC) + timedelta(minutes=5)
    waiter = DataWaiter(workbench_stores(db), kb, supply, now=lambda: later)
    # 第三轮第 6 条：委托还没结束（不该交还却交还了）：停下问用户，不悄悄取消
    assert await waiter.sweep()
    [row] = await rows(db, "select status, detail from workbench_task_resolution")
    assert row["status"] == "ready"
    assert row["detail"]["reason"] == "数据服务拒绝了：ref_released"
    assert [r["status"] for r in row["detail"]["requests"]] == ["missing"]
    # 委托已经结束的：当终态收掉
    async with make_client(A) as http:
        ended = await _waiting(http, db, world, "k-v2-released-2")
    async with db() as s, s.begin():
        await s.execute(
            text("update workbench_tasks set state = 'CANCELLED' where id = :t"),
            {"t": ended},
        )
        # 到点查的那一刻委托还没结束（领到的那一行是旧的），续签被拒时才看到它结束了
        await s.execute(
            text(
                "update workbench_task_resolution set next_check_at = now()"
                " where task_id = :t"
            ),
            {"t": ended},
        )
    stale = await rows(
        db,
        """select r.*, 'RUNNING' as task_state, t.owner_actor_id
           from workbench_task_resolution r join workbench_tasks t on t.id = r.task_id
           where r.task_id = :t""",
        t=ended,
    )
    assert await waiter.check_now(stale) == 1
    [row] = await rows(
        db, "select status from workbench_task_resolution where task_id = :t", t=ended
    )
    assert row["status"] == "cancelled"


async def test_releasing_a_claim_only_clears_ones_own(
    make_client,
    db,  # noqa: F811
    world,
) -> None:
    """第二轮第 8 条：放开时只清自己领的那一次（期限值对得上）；期限长过最坏的一次查。"""
    await known(db)
    async with make_client(A) as http:
        task_id = await _waiting(http, db, world, "k-v2-unclaim")
    assert CLAIM_LEASE >= timedelta(minutes=8)
    now = datetime.now(UTC) + timedelta(minutes=5)
    stores = workbench_stores(db)
    async with stores() as repo:
        async with repo.transaction():
            mine = await repo.claim_task_resolution(
                task_id, now=now, until=now + CLAIM_LEASE
            )
    assert mine is not None
    async with stores() as repo:
        async with repo.transaction():
            await repo.release_resolution_claim(task_id, now)  # 不是我领的那一次
    [row] = await rows(db, "select checking_until from workbench_task_resolution")
    assert row["checking_until"] == mine["checking_until"]
    async with stores() as repo:
        async with repo.transaction():
            await repo.release_resolution_claim(task_id, mine["checking_until"])
    [row] = await rows(db, "select checking_until from workbench_task_resolution")
    assert row["checking_until"] is None


async def test_uploaded_then_rechecked_with_the_users_own_token(
    make_client,
    db,  # noqa: F811
    world,
) -> None:
    """第二轮第 5 条：「传好了，重新查」：凭本人令牌请 knowbase 重解析，面板再取是新的。"""
    kb = world["kb"]
    await known(db)
    async with make_client(A) as http:
        task_id, _, _ = await asked(http, db, world, "k-v2-recheck")
        done = await http.post(f"/api/workbench/tasks/{task_id}/data-requests/recheck")
        assert done.status_code == 200, done.text
        assert done.json()["status"] == "rechecked"
        assert [c for c in kb.calls if c[0] == "refresh_mine"] == [
            ("refresh_mine", "res-1")
        ]
        shown = (await http.get(f"/api/workbench/tasks/{task_id}/data")).json()
        assert [i["status"] for i in shown["requests"][0]["items"]] == [
            "resolved",
            "resolved",
        ]
        other = await http.post(
            f"/api/workbench/tasks/{uuid.uuid4()}/data-requests/recheck"
        )
        assert other.status_code == 404


@pytest.mark.parametrize(
    "status, code, expected",
    [
        (404, "http_error", (503, "user_data_unavailable")),
        (405, "http_error", (503, "user_data_unavailable")),
        (401, "unauthorized", (503, "user_data_unavailable")),
        (404, "resolution_not_found", (422, "data_choice_invalid")),
    ],
)
async def test_recheck_distinguishes_upstream_failure_without_mutating_the_task(
    make_client,
    db,
    world,
    monkeypatch,
    status,
    code,
    expected,
) -> None:
    from app.infrastructure.external.knowbase_client import KnowbaseRefused

    async def refused_refresh(*args):
        raise KnowbaseRefused(status, code)

    await known(db)
    async with make_client(A) as http:
        task_id, _, _ = await asked(http, db, world, "k-v2-recheck-failure")
        before = await rows(db, "select * from workbench_task_resolution")
        calls = list(world["kb"].calls)
        monkeypatch.setattr(world["kb"], "refresh_mine", refused_refresh)
        result = await http.post(
            f"/api/workbench/tasks/{task_id}/data-requests/recheck"
        )
        assert (result.status_code, result.json()["code"]) == expected, result.text
        assert await rows(db, "select * from workbench_task_resolution") == before
        assert world["kb"].calls == calls


async def test_a_slow_knowfeed_does_not_hold_the_panel(
    make_client,
    db,  # noqa: F811
    world,
    monkeypatch,
) -> None:
    """小项：面板上的预切合整体有时限，超了照常列、不带预计。"""
    import asyncio

    import app.application.workbench.expert_data as expert_module

    supply = world["supply"]
    monkeypatch.setattr(expert_module, "PREMATCH_BUDGET_SECONDS", 0.05)

    async def slow(items, conditions):  # type: ignore[no-untyped-def]
        await asyncio.sleep(1)
        return {"items": []}

    supply.prematch = slow
    await known(db)
    async with make_client(A) as http:
        task_id, _, _ = await asked(http, db, world, "k-v2-slow")
        shown = (await http.get(f"/api/workbench/tasks/{task_id}/data")).json()
    assert all("prematch" not in i for i in shown["requests"][0]["items"])


async def test_only_a_missing_required_item_makes_it_incomplete(
    make_client,
    db,  # noqa: F811
    world,
) -> None:
    """第二轮第 7 条：缺项带上是不是必需的；可选的去掉不算「数据不全」，必需的不要了、
    照现有的做才算。"""
    from app.domain.workbench.data_plan import short_of_required

    await known(db)
    async with make_client(A) as http:
        task_id, _, _ = await asked(http, db, world, "k-v2-gaps")
        shown = (await http.get(f"/api/workbench/tasks/{task_id}/data")).json()
        _have, want = shown["requests"][0]["items"]
        await http.post(
            f"/api/workbench/tasks/{task_id}/data-requests/confirm",
            json={"dropped": [want["ref"]]},
        )
    [row] = await rows(db, "select detail from workbench_task_resolution")
    assert row["detail"]["gaps"] == [
        {
            "item": "上一年比较 600519.SH 2024-01-01–2024-12-31：你没要",
            "required": False,
        }
    ]
    assert short_of_required(row["detail"]["gaps"]) is False

    async with make_client(A) as http:
        other, _, _ = await asked(http, db, world, "k-v2-gaps-2")
        declined = await http.post(
            f"/api/workbench/tasks/{other}/data-requests/decline"
        )
        assert declined.json()["status"] == "declined"
    [row] = await rows(
        db, "select detail from workbench_task_resolution where task_id = :t", t=other
    )
    assert {(g["item"], g["required"]) for g in row["detail"]["gaps"]} == {
        ("最近一年的年报：你没要", True),
        ("上一年比较：你没要", False),
    }
    assert short_of_required(row["detail"]["gaps"]) is True


async def test_a_switched_then_dropped_item_writes_no_preference(
    make_client,
    db,  # noqa: F811
    world,
) -> None:
    kb = world["kb"]
    kb.alternatives = [candidate("2025") | {"material_id": "m-2025-mine"}]
    await known(db)
    async with make_client(A) as http:
        task_id, _, _ = await asked(http, db, world, "k-v2-switch-drop")
        shown = (await http.get(f"/api/workbench/tasks/{task_id}/data")).json()
        have, want = shown["requests"][0]["items"]
        await http.post(
            f"/api/workbench/tasks/{task_id}/data-requests/confirm",
            json={
                "picks": {have["ref"]: {"material_id": "m-2025-mine", "version_no": 1}},
                "dropped": [have["ref"], want["ref"]],
            },
        )
    assert [c for c in kb.calls if c[0] == "save_preference"] == []


async def test_missing_data_can_be_reviewed_and_retried_in_the_same_task(
    make_client, db, world
):
    kb, supply = world["kb"], world["supply"]
    kb.arrives = False
    await known(db)
    async with make_client(A) as http:
        task_id, _, _ = await asked(http, db, world, "k-retry-failed-supply")
        path = f"/api/workbench/tasks/{task_id}/data-requests"
        assert (await http.post(path + "/confirm", json={})).status_code == 200
        first = supply.submitted[0]
        supply.status = "failed"
        waiter = DataWaiter(
            workbench_stores(db),
            kb,
            supply,
            now=lambda: datetime.now(UTC) + timedelta(hours=1),
        )
        assert await waiter.sweep() == 1
        before = (await http.get(f"/api/workbench/tasks/{task_id}/data")).json()
        assert before["requests"][0]["status"] == "missing"
        reviewed = await http.post(path + "/recheck", json={})
        assert reviewed.status_code == 200, reviewed.text
        after = (await http.get(f"/api/workbench/tasks/{task_id}/data")).json()
        assert after["requests"][0]["status"] == "pending"
        assert after["requests"][0]["needs"] == before["requests"][0]["needs"]
        assert len(supply.submitted) == 1  # 重新核对不是代用户确认
        assert (await http.post(path + "/recheck", json={})).status_code == 200
        assert (await http.post(path + "/confirm", json={})).status_code == 200
        second = supply.submitted[1]
        assert second["task_ref"] == first["task_ref"]
        assert second["items"] == first["items"]
        assert second["idempotency_key"] != first["idempotency_key"]
        assert (await http.post(path + "/confirm", json={})).status_code == 422
        assert len(supply.submitted) == 2
