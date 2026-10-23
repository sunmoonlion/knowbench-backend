"""我的机器：本地代理报到 → 登记、在线、离线。这里不碰数据库，用替身看对账的规矩。"""

from __future__ import annotations

import asyncio
from contextlib import asynccontextmanager

import pytest

from app.application.workbench.machines import MachineSync, machine_report
from app.application.workbench.provisioning import RelayAdminFailed

OWNER_A = "11111111-1111-1111-1111-111111111111"
OWNER_B = "22222222-2222-2222-2222-222222222222"


def agent(name="laptop", roots=("/home/u/research",), **over):
    info = {
        "codex": "0.155.1",
        "software": "0.2.0",
        "since": 1,
        "machine": {
            "name": name,
            "roots": list(roots),
            "ceiling": {"sandbox": "workspace-write", "network": False},
        },
    }
    info.update(over)
    return info


class Store:
    def __init__(self, identities, environments=None):
        self.identities = identities
        self.environments: dict[str, dict] = environments or {}
        self.calls: list[tuple] = []
        self.committed = 0

    @asynccontextmanager
    async def transaction(self):
        yield
        self.committed += 1

    async def list_relay_identities(self):
        return [{"owner_actor_id": o, "relay_user": u} for o, u in self.identities]

    async def report_environment(self, *, owner_actor_id, name, **fields):
        self.calls.append(("report", owner_actor_id, name))
        key = f"{owner_actor_id}/{name}"
        self.environments[key] = {
            "owner": owner_actor_id,
            "name": name,
            "status": "online",
            **fields,
        }
        return key

    async def set_environments_offline(self, owner_actor_id, *, except_id=None):
        self.calls.append(("offline", owner_actor_id, except_id))
        changed = 0
        for key, env in self.environments.items():
            if (
                env["owner"] == owner_actor_id
                and env["status"] == "online"
                and key != except_id
            ):
                env["status"] = "offline"
                changed += 1
        return changed

    async def queue_environment_recovery(self, environment_id, *, owner_actor_id):
        self.calls.append(("recovery_probe", owner_actor_id, environment_id))
        return 0


class Stores:
    def __init__(self, store):
        self.store = store

    @asynccontextmanager
    async def __call__(self):
        yield self.store


class Relay:
    def __init__(self, agents=None, fail=False):
        self.online = agents or {}
        self.fail = fail
        self.asked = 0

    async def agents(self):
        self.asked += 1
        if self.fail:
            raise RelayAdminFailed("relay admin channel unreachable")
        return self.online


def test_a_report_keeps_only_what_can_be_registered():
    report = machine_report(
        agent(
            name="  我的电脑  ",
            roots=["/home/u/a", "relative", "D:\\work", "C:/x", 3, ""],
        )
        | {
            "machine": {
                "name": "  我的电脑  ",
                "roots": ["/home/u/a", "relative", "D:\\work", "C:/x", 3, ""],
                "ceiling": {"sandbox": "read-only", "network": True, "extra": 1},
            }
        }
    )
    assert report == {
        "name": "我的电脑",
        "roots": ["/home/u/a", "D:\\work", "C:/x"],
        "ceiling": {"sandbox": "read-only", "network": True},
        "agent_version": "0.2.0",
        "codex_version": "0.155.1",
        "device_id": None,
        "host": None,
        "os": None,
    }


def test_a_report_carries_a_well_formed_device_id_host_and_os():
    """账 143：0.5.2 起本地 link 报设备编号、主机名、系统；编号不合格式就当没报。"""
    machine = {
        "name": "pc",
        "roots": [],
        "ceiling": {},
        "device_id": "0f8fad5b-d9cb-469f-a165-70867728950e",
        "host": "DESKTOP-" + "x" * 300,
        "os": "Windows",
    }
    report = machine_report({"codex": "0.155.1", "machine": machine})
    assert report is not None
    assert report["device_id"] == "0f8fad5b-d9cb-469f-a165-70867728950e"
    assert len(report["host"]) == 128 and report["os"] == "Windows"
    bad = machine_report({"machine": machine | {"device_id": "PC-1"}})
    assert bad is not None and bad["device_id"] is None


@pytest.mark.parametrize(
    "machine",
    [None, "text", {}, {"name": ""}, {"name": "   "}, {"roots": ["/a"]}],
)
def test_an_agent_that_names_no_machine_reports_nothing(machine):
    assert machine_report({"codex": "0.155.1", "machine": machine}) is None


def test_unknown_ceiling_values_are_dropped_not_guessed():
    report = machine_report(
        {"machine": {"name": "pc", "ceiling": {"sandbox": "root", "network": "yes"}}}
    )
    assert report["ceiling"] == {}
    assert report["roots"] == []
    assert report["agent_version"] is None and report["codex_version"] is None


async def test_an_online_agent_registers_its_machine_and_the_rest_go_offline():
    store = Store(
        [(OWNER_A, "u-a")],
        {f"{OWNER_A}/old-pc": {"owner": OWNER_A, "name": "old-pc", "status": "online"}},
    )
    sync = MachineSync(Stores(store), Relay({"u-a": agent()}))
    assert await sync.sync_once() == {"online": 1, "offline": 1, "untouched": 0}
    env = store.environments[f"{OWNER_A}/laptop"]
    assert env["status"] == "online"
    assert env["roots"] == ["/home/u/research"]
    assert env["ceiling"] == {"sandbox": "workspace-write", "network": False}
    assert env["agent_version"] == "0.2.0" and env["codex_version"] == "0.155.1"
    assert store.environments[f"{OWNER_A}/old-pc"]["status"] == "offline"
    assert store.committed == 1


async def test_a_person_whose_agent_is_gone_has_every_machine_offline():
    store = Store(
        [(OWNER_A, "u-a"), (OWNER_B, "u-b")],
        {
            f"{OWNER_A}/laptop": {
                "owner": OWNER_A,
                "name": "laptop",
                "status": "online",
            },
            f"{OWNER_B}/desk": {"owner": OWNER_B, "name": "desk", "status": "online"},
        },
    )
    sync = MachineSync(Stores(store), Relay({"u-b": agent(name="desk")}))
    assert await sync.sync_once() == {"online": 1, "offline": 1, "untouched": 0}
    assert store.environments[f"{OWNER_A}/laptop"]["status"] == "offline"
    assert store.environments[f"{OWNER_B}/desk"]["status"] == "online"


async def test_an_old_agent_without_machine_info_leaves_the_books_alone():
    store = Store(
        [(OWNER_A, "u-a")],
        {f"{OWNER_A}/manual": {"owner": OWNER_A, "name": "manual", "status": "online"}},
    )
    old = {"codex": "0.155.1", "software": "0.1.0", "since": 1, "machine": {}}
    sync = MachineSync(Stores(store), Relay({"u-a": old}))
    assert await sync.sync_once() == {"online": 0, "offline": 0, "untouched": 1}
    assert store.calls == []
    assert store.environments[f"{OWNER_A}/manual"]["status"] == "online"


async def test_agents_of_people_we_never_issued_an_identity_to_are_ignored():
    store = Store([(OWNER_A, "u-a")])
    sync = MachineSync(Stores(store), Relay({"u-stranger": agent()}))
    assert await sync.sync_once() == {"online": 0, "offline": 0, "untouched": 0}
    assert store.environments == {}


async def test_when_the_relay_cannot_be_asked_nothing_is_changed():
    store = Store(
        [(OWNER_A, "u-a")],
        {f"{OWNER_A}/laptop": {"owner": OWNER_A, "name": "laptop", "status": "online"}},
    )
    sync = MachineSync(Stores(store), Relay(fail=True))
    with pytest.raises(RelayAdminFailed):
        await sync.sync_once()
    assert store.calls == [] and store.committed == 0
    assert store.environments[f"{OWNER_A}/laptop"]["status"] == "online"


async def test_the_loop_survives_a_relay_outage_and_stops_when_told():
    store = Store([(OWNER_A, "u-a")])
    relay = Relay(fail=True)
    sync = MachineSync(Stores(store), relay, interval_seconds=0.01)
    stop = asyncio.Event()
    task = asyncio.create_task(sync.run_forever(stop))
    for _ in range(200):
        if relay.asked >= 2:
            break
        await asyncio.sleep(0.01)
    assert relay.asked >= 2 and not task.done()
    relay.fail = False
    relay.online = {"u-a": agent()}
    for _ in range(200):
        if store.environments:
            break
        await asyncio.sleep(0.01)
    assert store.environments[f"{OWNER_A}/laptop"]["status"] == "online"
    stop.set()
    await asyncio.wait_for(task, 2)
