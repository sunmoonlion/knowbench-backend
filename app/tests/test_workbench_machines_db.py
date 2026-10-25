"""我的机器：账上的三件事（报到、置离线、列身份）与一次真实的对账。真数据库。"""

from __future__ import annotations

from contextlib import asynccontextmanager

from sqlalchemy import text
from test_workbench_ledger_db import OTHER, OWNER
from test_workbench_ledger_db import db as db  # noqa: F401

from app.application.workbench.machines import MachineSync
from app.application.workbench.project_service import ProjectService
from app.infrastructure.workbench.repository import (
    SqlWorkbenchStores,
    WorkbenchRepository,
)

CEILING = {"sandbox": "workspace-write", "network": False}


class Relay:
    def __init__(self, online):
        self.online = online

    async def agents(self):
        return self.online


def agent(name, roots):
    return {
        "codex": "0.155.1",
        "software": "0.2.0",
        "since": 1,
        "machine": {"name": name, "roots": roots, "ceiling": CEILING},
    }


@asynccontextmanager
async def book(factory):
    async with factory() as session:
        repo = WorkbenchRepository(session)
        async with repo.transaction():
            yield repo


async def identity(factory, owner, relay_user):
    async with book(factory) as repo:
        await repo.put_relay_identity(
            owner,
            relay_user=relay_user,
            agent_token_ciphertext="a",
            sandbox_token_ciphertext="s",
        )


async def test_reporting_twice_keeps_one_machine_and_refreshes_it(db):
    async with book(db) as repo:
        first = await repo.report_environment(
            owner_actor_id=OWNER,
            name="laptop",
            agent_version="0.2.0",
            codex_version="0.155.1",
            roots=["/home/u/a"],
            ceiling=CEILING,
        )
    async with book(db) as repo:
        await repo.set_environment_status(first, "offline")
    async with book(db) as repo:
        again = await repo.report_environment(
            owner_actor_id=OWNER,
            name="laptop",
            agent_version="0.3.0",
            codex_version="0.156.0",
            roots=["/home/u/a", "/home/u/b"],
            ceiling={"sandbox": "read-only", "network": True},
        )
        other = await repo.report_environment(
            owner_actor_id=OTHER,
            name="laptop",
            agent_version=None,
            codex_version=None,
            roots=[],
            ceiling={},
        )
    assert again == first and other != first
    async with db() as session:
        mine = await WorkbenchRepository(session).list_environments(
            owner_actor_id=OWNER
        )
    assert len(mine) == 1
    env = mine[0]
    assert env["status"] == "online" and env["last_seen_at"] is not None
    assert env["roots"] == ["/home/u/a", "/home/u/b"]
    assert env["ceiling"] == {"sandbox": "read-only", "network": True}
    assert env["agent_version"] == "0.3.0" and env["codex_version"] == "0.156.0"


async def test_a_new_device_id_on_the_same_computer_takes_over_the_old_record(db):
    """同一台电脑上重新 init 换了设备编号：认领同名、同主机、已离线的老记录，项目不落在
    永远离线的那条上；别的主机上同名的另起一条（Fable 评审 P2-15）。"""
    common = {
        "owner_actor_id": OWNER,
        "name": "laptop",
        "agent_version": "0.5.2",
        "codex_version": None,
        "roots": [],
        "ceiling": {},
    }
    old_id, new_id = (
        "11111111-1111-4111-8111-111111111111",
        "22222222-2222-4222-8222-222222222222",
    )
    async with book(db) as repo:
        first = await repo.report_environment(
            device_id=old_id, host="LAPTOP-1", **common
        )
        await repo.set_environment_status(first, "offline")
    async with book(db) as repo:
        again = await repo.report_environment(
            device_id=new_id, host="LAPTOP-1", **common
        )
    assert again == first
    async with book(db) as repo:
        await repo.set_environment_status(first, "offline")
        copied = await repo.report_environment(
            device_id="33333333-3333-4333-8333-333333333333", host="OTHER-PC", **common
        )
    assert copied != first


async def test_going_offline_touches_only_that_persons_online_machines(db):
    async with book(db) as repo:
        ids = {}
        for owner, name in ((OWNER, "a"), (OWNER, "b"), (OTHER, "c")):
            ids[name] = await repo.report_environment(
                owner_actor_id=owner,
                name=name,
                agent_version=None,
                codex_version=None,
                roots=[],
                ceiling={},
            )
    async with book(db) as repo:
        assert await repo.set_environments_offline(OWNER, except_id=ids["a"]) == 1
    async with book(db) as repo:
        # 已经离线的不再算
        assert await repo.set_environments_offline(OWNER, except_id=ids["a"]) == 0
    async with db() as session:
        rows = (
            await session.execute(
                text("select name, status from workbench_environments order by name")
            )
        ).all()
    assert [tuple(r) for r in rows] == [
        ("a", "online"),
        ("b", "offline"),
        ("c", "online"),
    ]
    async with book(db) as repo:
        assert await repo.set_environments_offline(OWNER) == 1


async def test_identities_are_listed_without_their_tokens(db):
    await identity(db, OWNER, "u-owner")
    await identity(db, OTHER, "u-other")
    async with db() as session:
        listed = await WorkbenchRepository(session).list_relay_identities()
    assert [(str(i["owner_actor_id"]), i["relay_user"]) for i in listed] == [
        (OWNER, "u-owner"),
        (OTHER, "u-other"),
    ]
    assert all(set(i) == {"owner_actor_id", "relay_user"} for i in listed)


async def test_an_agent_coming_and_going_shows_up_as_a_workspace_then_offline(db):
    await identity(db, OWNER, "u-owner")
    relay = Relay({"u-owner": agent("laptop", ["/home/u/research"])})
    sync = MachineSync(SqlWorkbenchStores(db), relay)

    assert await sync.sync_once() == {"online": 1, "offline": 0, "untouched": 0}
    async with db() as session:
        workspaces = await ProjectService(WorkbenchRepository(session)).workspaces(
            owner_actor_id=OWNER
        )
    assert [(w["environment_name"], w["root"]) for w in workspaces] == [
        ("laptop", "/home/u/research")
    ]

    # 同一台机器再报一次：还是那一台
    assert await sync.sync_once() == {"online": 1, "offline": 0, "untouched": 0}
    # 换了一台机器接进来：旧的离线
    relay.online = {"u-owner": agent("desktop", ["/data/work"])}
    assert await sync.sync_once() == {"online": 1, "offline": 1, "untouched": 0}
    # 代理关了：全部离线
    relay.online = {}
    assert await sync.sync_once() == {"online": 0, "offline": 1, "untouched": 0}
    async with db() as session:
        envs = await WorkbenchRepository(session).list_environments(
            owner_actor_id=OWNER
        )
    assert {e["name"]: e["status"] for e in envs} == {
        "laptop": "offline",
        "desktop": "offline",
    }


DEV_A = "0f8fad5b-d9cb-469f-a165-70867728950e"
DEV_B = "7c9e6679-7425-40de-944b-e07fc1f90ae7"


async def report(repo, *, name, device=None, host=None, owner=OWNER):
    return await repo.report_environment(
        owner_actor_id=owner,
        name=name,
        agent_version="0.5.2",
        codex_version="0.155.1",
        roots=[],
        ceiling={},
        device_id=device,
        host=host,
        os="Windows" if device else None,
    )


async def test_device_id_keeps_same_named_links_apart_and_follows_renames(db):
    """账 143：按设备编号认本地 link。同名的两台不并成一条；改了电脑名还是原来那条。"""
    async with book(db) as repo:
        a = await report(repo, name="my-pc", device=DEV_A, host="DESKTOP-A")
        b = await report(repo, name="my-pc", device=DEV_B, host="DESKTOP-B")
    async with book(db) as repo:
        renamed = await report(repo, name="study-pc", device=DEV_A, host="DESKTOP-A")
    assert a != b and renamed == a
    async with db() as session:
        rows = await WorkbenchRepository(session).list_environments(
            owner_actor_id=OWNER
        )
    by_id = {str(r["id"]): r for r in rows}
    assert by_id[a]["name"] == "study-pc" and by_id[a]["device_id"] == DEV_A
    assert by_id[a]["host"] == "DESKTOP-A" and by_id[a]["first_host"] == "DESKTOP-A"
    assert by_id[a]["os"] == "Windows" and by_id[b]["device_id"] == DEV_B


async def test_new_link_adopts_its_legacy_row_so_projects_stay_attached(db):
    """老代理按名字登记过的那条，换上 0.5.2 后第一次报到时被认领（项目挂在它上面），不另起一条。"""
    async with book(db) as repo:
        legacy = await report(repo, name="laptop")
    async with book(db) as repo:
        adopted = await report(repo, name="laptop", device=DEV_A, host="LAPTOP")
        # 同名、但另一个编号：已有编号的那条不能再被认领，另起一条
        fresh = await report(repo, name="laptop", device=DEV_B, host="LAPTOP-2")
    assert adopted == legacy and fresh != legacy
    async with db() as session:
        assigned = await WorkbenchRepository(session).assigned_environment(OWNER)
    assert assigned is not None and assigned["device_id"] == DEV_B


async def test_copied_config_shows_as_a_host_change(db):
    """配置目录被整个拷到别的电脑：编号跟过去，主机名对不上，first_host 留着第一次的。"""
    async with book(db) as repo:
        env = await report(repo, name="pc", device=DEV_A, host="OLD-PC")
    async with book(db) as repo:
        same = await report(repo, name="pc", device=DEV_A, host="NEW-PC")
    async with db() as session:
        assigned = await WorkbenchRepository(session).assigned_environment(OWNER)
    assert same == env
    assert assigned["first_host"] == "OLD-PC" and assigned["host"] == "NEW-PC"


async def test_sync_registers_by_the_device_id_the_relay_passes_on(db):
    await identity(db, OWNER, "u-owner")
    online = agent("laptop", ["/home/u/a"])
    online["machine"].update({"device_id": DEV_A, "host": "LAPTOP", "os": "Windows"})
    sync = MachineSync(SqlWorkbenchStores(db), Relay({"u-owner": online}))
    assert (await sync.sync_once())["online"] == 1
    online["machine"]["device_id"] = "not-a-uuid"
    online["machine"]["name"] = "laptop-2"
    await sync.sync_once()
    async with db() as session:
        rows = await WorkbenchRepository(session).list_environments(
            owner_actor_id=OWNER
        )
    devices = sorted((r["name"], r["device_id"]) for r in rows)
    assert devices == [("laptop", DEV_A), ("laptop-2", None)]
