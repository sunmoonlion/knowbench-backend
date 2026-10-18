"""项目与对话的接口（PRD/apps/investment.md 的 AT-INV-*）：真数据库，依赖覆盖注入身份。"""

from __future__ import annotations

import uuid

import pytest
from sqlalchemy import text
from test_workbench_ledger_db import db as db  # noqa: F401
from test_workbench_routes import A, B
from test_workbench_routes import make_client as make_client  # noqa: F401

pytestmark = pytest.mark.usefixtures("builtin_packs_on_offer")

ROOT = "/home/u/research"


async def machine(http, *, sandbox=True):
    env = (
        await http.post(
            "/api/workbench/environments",
            json={"name": "pc", "roots": [ROOT, "/home/u/notes"]},
        )
    ).json()["environment_id"]
    if sandbox:
        await http.post(
            "/api/workbench/sandboxes",
            json={
                "app_server_url": "ws://sandbox:47800",
                "token_ref": "env:APP_SERVER_TOKEN",
            },
        )
    return env


async def project(http, env, path="hengrui", **more):
    return await http.post(
        "/api/workbench/projects",
        json={"environment_id": env, "workspace_root": ROOT, "path": path, **more},
    )


async def test_workspace_then_project_then_conversations(make_client, db):  # noqa: F811
    async with make_client(A) as http:
        env = await machine(http)
        spaces = (await http.get("/api/workbench/workspaces")).json()
        assert spaces["contract_version"] == 2
        assert [w["root"] for w in spaces["workspaces"]] == [ROOT, "/home/u/notes"]
        assert spaces["workspaces"][0]["environment_id"] == env

        created = await project(http, env, "2025/恒瑞医药")
        assert created.status_code == 201
        made = created.json()
        assert made["directory"] == "/home/u/research/2025/恒瑞医药"
        assert made["title"] == "恒瑞医药"

        chat = await http.post(
            "/api/workbench/sessions", json={"kind": "chat", "project_id": made["id"]}
        )
        work = await http.post(
            "/api/workbench/sessions",
            json={"kind": "work", "project_id": made["id"], "title": "整理年报"},
        )
        free = await http.post("/api/workbench/sessions", json={"kind": "chat"})
        assert [r.status_code for r in (chat, work, free)] == [201, 201, 201]
        assert free.json()["project_id"] is None

        seen = (await http.get(f"/api/workbench/projects/{made['id']}")).json()
        assert seen["project"]["id"] == made["id"] and seen["active_task_id"] is None
        assert {(c["kind"], c["title"]) for c in seen["conversations"]} == {
            ("chat", None),
            ("work", "整理年报"),
        }
        listed = (await http.get("/api/workbench/projects")).json()["projects"]
        assert [(p["path"], p["conversations"]) for p in listed] == [
            ("2025/恒瑞医药", 2)
        ]
        inside = await http.get(
            "/api/workbench/sessions", params={"project_id": made["id"]}
        )
        outside = await http.get(
            "/api/workbench/sessions", params={"without_project": "true"}
        )
        assert len(inside.json()["sessions"]) == 2
        assert [s["id"] for s in outside.json()["sessions"]] == [
            free.json()["session_id"]
        ]
        view = await http.get(f"/api/workbench/sessions/{work.json()['session_id']}")
        session = view.json()["session"]
        assert (session["kind"], session["title"]) == ("work", "整理年报")
        assert session["project_id"] == made["id"]
    # 每段新对话都排了「起一条线」的命令
    async with db() as s:
        kinds = (
            (await s.execute(text("select kind from workbench_commands")))
            .scalars()
            .all()
        )
    assert kinds == ["session.start_thread"] * 3


async def test_work_needs_a_project_and_a_sandbox(make_client, db):  # noqa: F811
    """AT-INV-04"""
    async with make_client(A) as http:
        await machine(http, sandbox=False)
        none = await http.post("/api/workbench/sessions", json={"kind": "chat"})
        assert none.status_code == 409 and none.json()["code"] == "no_sandbox"
    async with make_client(B) as http:
        await machine(http)
        refused = await http.post("/api/workbench/sessions", json={"kind": "work"})
        assert refused.status_code == 409
        assert refused.json()["code"] == "project_required"
        for body in (
            {"kind": "talk"},
            {"kind": "chat", "project_root": "/home/u/research/x"},
            {"kind": "chat", "thread_settings": {"sandbox": "danger-full-access"}},
            {},
            {"environment_id": "x"},
        ):
            bad = await http.post("/api/workbench/sessions", json=body)
            assert bad.status_code == 422, body


@pytest.mark.parametrize("path", ["../elsewhere", "/etc", "a/../b", "C:\\x", "a\\b"])
async def test_a_project_cannot_leave_its_workspace(make_client, db, path):  # noqa: F811
    """AT-INV-05"""
    async with make_client(A) as http:
        env = await machine(http)
        refused = await project(http, env, path)
        assert refused.status_code == 400
        assert refused.json()["code"] == "project_path_invalid"
        assert (await http.get("/api/workbench/projects")).json()["projects"] == []


async def test_project_rules_over_http(make_client, db):  # noqa: F811
    async with make_client(A) as http:
        env = await machine(http)
        first = (await project(http, env)).json()
        again = await project(http, env)
        assert again.status_code == 409 and again.json()["code"] == "project_exists"
        outside = await http.post(
            "/api/workbench/projects",
            json={"environment_id": env, "workspace_root": "/home/u", "path": "x"},
        )
        assert outside.status_code == 400
        assert outside.json()["code"] == "root_outside_whitelist"
        extra = await project(http, env, "y", owner_actor_id=B)
        assert extra.status_code == 422

        path = f"/api/workbench/projects/{first['id']}"
        renamed = await http.patch(path, json={"title": " 恒瑞  研究 "})
        assert renamed.status_code == 200 and renamed.json()["title"] == "恒瑞 研究"
        assert (await http.patch(path, json={})).status_code == 422
        archived = await http.patch(path, json={"archived": True})
        assert archived.json()["archived"] is True
        refused = await http.post(
            "/api/workbench/sessions", json={"kind": "work", "project_id": first["id"]}
        )
        assert refused.status_code == 409
        assert refused.json()["code"] == "project_archived"
        assert (await http.get("/api/workbench/projects")).json()["projects"] == []
        kept = await http.get(
            "/api/workbench/projects", params={"include_archived": "true"}
        )
        assert len(kept.json()["projects"]) == 1
        restored = await http.patch(path, json={"archived": False})
        assert restored.json()["archived"] is False


async def test_a_chat_is_put_into_a_project_becomes_work_and_is_renamed(
    make_client,  # noqa: F811
    db,  # noqa: F811
):
    """AT-INV-06、AT-INV-07 的账"""
    async with make_client(A) as http:
        env = await machine(http)
        one, two = (
            (await project(http, env, "a")).json(),
            (await project(http, env, "b")).json(),
        )
        sid = (
            await http.post("/api/workbench/sessions", json={"kind": "chat"})
        ).json()["session_id"]
        early = await http.post(
            f"/api/workbench/sessions/{sid}/kind", json={"kind": "work"}
        )
        assert early.status_code == 409 and early.json()["code"] == "project_required"
        ask = await http.post(
            f"/api/workbench/sessions/{sid}/handover",
            json={
                "idempotency_key": "k-0000-0001",
                "profile_id": "DATA_QUERY",
                "original_input": {"text": "毛利率"},
                "budget_limit": "2",
            },
        )
        assert ask.status_code == 409 and ask.json()["code"] == "project_required"

        attached = await http.post(
            f"/api/workbench/sessions/{sid}/project", json={"project_id": one["id"]}
        )
        assert attached.status_code == 200
        assert attached.json()["project_id"] == one["id"]
        assert attached.json()["project_root"] == "/home/u/research/a"
        moved = await http.post(
            f"/api/workbench/sessions/{sid}/project", json={"project_id": two["id"]}
        )
        assert moved.status_code == 409
        assert moved.json()["code"] == "conversation_change_refused"

        back = await http.post(
            f"/api/workbench/sessions/{sid}/kind", json={"kind": "chat"}
        )
        assert back.status_code == 422  # 工作不转回聊天，接口上就没有这个写法
        worked = await http.post(
            f"/api/workbench/sessions/{sid}/kind", json={"kind": "work"}
        )
        assert worked.status_code == 200 and worked.json()["kind"] == "work"

        named = await http.patch(
            f"/api/workbench/sessions/{sid}", json={"title": "恒瑞的盈利"}
        )
        assert named.json()["title"] == "恒瑞的盈利"
        cleared = await http.patch(
            f"/api/workbench/sessions/{sid}", json={"title": " "}
        )
        assert cleared.json()["title"] is None


async def test_the_two_rules_over_http(make_client, db):  # noqa: F811
    """一个项目一个专家；专家在时别的对话可以聊天、不可以工作。AT-INV-13"""
    body = {
        "profile_id": "DATA_QUERY",
        "original_input": {"text": "毛利率"},
        "budget_limit": "2",
    }
    async with make_client(A) as http:
        env = await machine(http)
        made = (await project(http, env)).json()

        async def new(kind):
            created = await http.post(
                "/api/workbench/sessions", json={"kind": kind, "project_id": made["id"]}
            )
            return created.json()["session_id"]

        handed, working, chatting = (
            await new("work"),
            await new("work"),
            await new("chat"),
        )
        first = await http.post(
            f"/api/workbench/sessions/{handed}/handover",
            json={**body, "idempotency_key": "k-0000-0001"},
        )
        assert first.status_code == 201
        second = await http.post(
            f"/api/workbench/sessions/{working}/handover",
            json={**body, "idempotency_key": "k-0000-0002"},
        )
        # 同一项目第二位专家排队，不再拒绝（0017：一次一位，其余排队）
        assert second.status_code == 201
        assert second.json()["queued_behind"] == first.json()["task_id"]

        async def say(sid):
            return await http.post(
                f"/api/workbench/sessions/{sid}/turns", json={"text": "继续"}
            )

        held = await say(handed)
        assert held.status_code == 409 and held.json()["code"] == "wheel_held_by_other"
        blocked = await say(working)
        assert blocked.status_code == 409
        assert blocked.json()["code"] == "project_held_by_expert"
        assert (await say(chatting)).status_code == 202
        seen = (await http.get(f"/api/workbench/projects/{made['id']}")).json()
        assert seen["active_task_id"] == first.json()["task_id"]
        assert seen["tasks"][0]["question"] == "毛利率"
        busy = await http.patch(
            f"/api/workbench/projects/{made['id']}", json={"archived": True}
        )
        assert busy.status_code == 409 and busy.json()["code"] == "project_busy"

        cancelled = await http.post(
            f"/api/workbench/tasks/{first.json()['task_id']}/cancel"
        )
        assert cancelled.status_code in (200, 202)
        # 排队的第二位自动开始：它那段对话的方向盘到了专家手里；聊天照常
        taken = await say(working)
        assert (
            taken.status_code == 409 and taken.json()["code"] == "wheel_held_by_other"
        )
        assert (await say(chatting)).status_code == 202
        seen = (await http.get(f"/api/workbench/projects/{made['id']}")).json()
        assert seen["active_task_id"] == second.json()["task_id"]


async def test_nobody_reaches_somebody_elses_projects_or_conversations(
    make_client,  # noqa: F811
    db,  # noqa: F811
):
    """AT-INV-15"""
    async with make_client(A) as http:
        env = await machine(http)
        made = (await project(http, env)).json()
        sid = (
            await http.post(
                "/api/workbench/sessions",
                json={"kind": "work", "project_id": made["id"]},
            )
        ).json()["session_id"]
        free = (
            await http.post("/api/workbench/sessions", json={"kind": "chat"})
        ).json()["session_id"]
    async with make_client(B) as http:
        other_env = await machine(http)
        assert (await http.get("/api/workbench/projects")).json()["projects"] == []
        assert (await http.get("/api/workbench/sessions")).json()["sessions"] == []
        for method, path, body in [
            ("GET", f"/api/workbench/projects/{made['id']}", None),
            ("PATCH", f"/api/workbench/projects/{made['id']}", {"title": "x"}),
            ("PATCH", f"/api/workbench/projects/{made['id']}", {"archived": True}),
            ("GET", f"/api/workbench/sessions?project_id={made['id']}", None),
            (
                "POST",
                "/api/workbench/sessions",
                {"kind": "chat", "project_id": made["id"]},
            ),
            ("PATCH", f"/api/workbench/sessions/{sid}", {"title": "x"}),
            ("POST", f"/api/workbench/sessions/{sid}/kind", {"kind": "work"}),
            (
                "POST",
                f"/api/workbench/sessions/{free}/project",
                {"project_id": made["id"]},
            ),
            ("GET", f"/api/workbench/projects/{uuid.uuid4()}", None),
            ("GET", "/api/workbench/projects/not-a-uuid", None),
        ]:
            response = await http.request(method, path, json=body)
            assert response.status_code == 404, (method, path, response.text)
        # 在别人的机器上建项目也不行
        stolen = await http.post(
            "/api/workbench/projects",
            json={"environment_id": env, "workspace_root": ROOT, "path": "x"},
        )
        assert stolen.status_code == 404
        # 自己的聊天放不进别人的项目
        mine = (
            await http.post("/api/workbench/sessions", json={"kind": "chat"})
        ).json()["session_id"]
        refused = await http.post(
            f"/api/workbench/sessions/{mine}/project", json={"project_id": made["id"]}
        )
        assert refused.status_code == 404 and other_env != env


async def test_the_experts_on_offer(make_client, db):  # noqa: F811
    """AT-INV-17、AT-INV-19"""
    async with make_client(A) as http:
        got = await http.get("/api/workbench/packs")
        assert got.status_code == 200
        body = got.json()
        assert body["contract_version"] == 2
        assert body["budget"] == {"currency": "CNY", "default": None}
        assert [p["name"] for p in body["packs"]] == [
            "问数",
            "财报体检",
            "财务分析与预测",
        ]
        # 筹备中的那位看得见、请不了（所有者 2026-10-13 定先占位）
        assert [p["status"] for p in body["packs"]] == ["ready", "ready", "ready"]
        assert "method" not in got.text and "Reply with ONE JSON" not in got.text

        one = await http.get("/api/workbench/packs/FIN_REVIEW")
        assert one.status_code == 200
        assert len(one.json()["pack"]["steps"]) == 7

        # 只给我们自己测机制用的，不给用户看
        assert (await http.get("/api/workbench/packs/SMOKE")).status_code == 404
        assert (await http.get("/api/workbench/packs/NOPE")).status_code == 404


async def test_remove_conversation_and_project_hide_them_but_keep_history(
    make_client, db
):  # noqa: F811
    async with make_client(A) as http:
        env = await machine(http)
        made = (await project(http, env, "wuxi")).json()
        kept = (
            await http.post(
                "/api/workbench/sessions",
                json={"kind": "work", "project_id": made["id"], "title": "留着"},
            )
        ).json()
        gone_response = await http.post(
            "/api/workbench/sessions",
            json={"kind": "chat", "project_id": made["id"], "title": "删掉"},
        )
        assert gone_response.status_code == 201, gone_response.json()
        gone = gone_response.json()
        # 对话：删掉之后清单、项目页都不再列，但记录还在、直接打开还能看
        assert (
            await http.delete(f"/api/workbench/sessions/{gone['session_id']}")
        ).status_code == 204
        listed = {
            c["id"]
            for c in (await http.get("/api/workbench/sessions")).json()["sessions"]
        }
        assert kept["session_id"] in listed and gone["session_id"] not in listed
        seen = (await http.get(f"/api/workbench/projects/{made['id']}")).json()
        assert [c["id"] for c in seen["conversations"]] == [kept["session_id"]]
        assert (
            await http.get(f"/api/workbench/sessions/{gone['session_id']}")
        ).status_code == 200
        rows = (await http.get("/api/workbench/projects")).json()["projects"]
        assert next(p for p in rows if p["id"] == made["id"])["conversations"] == 1
        # 项目：删掉 = 从工作台移除（归档），清单不再列；勾「含归档」还能找回
        assert (
            await http.delete(f"/api/workbench/projects/{made['id']}")
        ).status_code == 204
        rows = (await http.get("/api/workbench/projects")).json()["projects"]
        assert all(p["id"] != made["id"] for p in rows)
        rows = (await http.get("/api/workbench/projects?include_archived=true")).json()[
            "projects"
        ]
        assert next(p for p in rows if p["id"] == made["id"])["archived"] is True
        # 已删的对话不能再删一遍到别人头上：别的用户看不见
    async with make_client(B) as other:
        assert (
            await other.delete(f"/api/workbench/sessions/{kept['session_id']}")
        ).status_code == 404
        assert (
            await other.delete(f"/api/workbench/projects/{made['id']}")
        ).status_code == 404
