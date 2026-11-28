"""Permission reports never authorise a command; persist before acknowledging."""

import time
import uuid
from copy import deepcopy

import pytest
from test_workbench_machines import OWNER_A, Relay, Store, Stores, agent

from app.application.workbench.agent_reports import record_agent_reports
from app.application.workbench.machines import MachineSync


def envelope():
    return {
        "receipt": "c" * 32,
        "report": {
            "id": str(uuid.uuid4()),
            "conn": "01234567",
            "threadId": str(uuid.uuid4()),
            "requestDigest": "a" * 64,
            "permissionDigest": "b" * 64,
            "decision": "approved",
            "scope": {"sandbox": "workspace-write", "network": False},
            "expiresAt": int(time.time()) + 300,
        },
    }


class AuditStore(Store):
    def __init__(self, item):
        super().__init__([(OWNER_A, "u-a")])
        self.row = {
            "id": str(uuid.uuid4()),
            "owner_actor_id": OWNER_A,
            "environment_id": f"{OWNER_A}/laptop",
            "thread_id": item["report"]["threadId"],
        }
        self.events = []

    async def get_session_by_thread(self, thread_id):
        return self.row if self.row and self.row["thread_id"] == thread_id else None

    async def get_session(self, session_id, *, owner_actor_id, for_update):
        assert owner_actor_id == self.row["owner_actor_id"] and for_update
        return self.row

    async def permission_report_payloads(self, *, session_id, report_id):
        return [e["payload"] for e in self.events if e["payload"]["id"] == report_id]

    async def append_event(self, **event):
        self.events.append(event)


class AuditRelay(Relay):
    def __init__(self, store, item):
        super().__init__({"u-a": agent(permission_reports=[item])})
        self.store = store
        self.receipts = []

    async def permission_receipts(self, receipts):
        assert self.store.committed > 0
        self.receipts.extend(receipts)


async def test_sync_commits_before_ack_and_repeated_poll_is_idempotent():
    item = envelope()
    store = AuditStore(item)
    relay = AuditRelay(store, item)
    sync = MachineSync(Stores(store), relay)
    await sync.sync_once()
    await sync.sync_once()
    assert len(store.events) == 1
    assert store.events[0]["payload"] == item["report"]
    assert relay.receipts == [{"receipt": item["receipt"], "status": "recorded"}] * 2


@pytest.mark.parametrize(
    "change", ["owner", "machine", "thread", "expired", "different-replay"]
)
async def test_wrong_binding_or_changed_report_is_rejected(change):
    item = envelope()
    store = AuditStore(item)
    if change == "owner":
        store.row["owner_actor_id"] = str(uuid.uuid4())
    if change == "machine":
        store.row["environment_id"] = "another-machine"
    if change == "thread":
        store.row["thread_id"] = str(uuid.uuid4())
    if change == "expired":
        item["report"]["expiresAt"] = int(time.time()) - 1
    if change == "different-replay":
        store.events.append({"payload": deepcopy(item["report"])})
        item["report"]["scope"]["network"] = True
    result = await record_agent_reports(
        store, owner=OWNER_A, environment_id=f"{OWNER_A}/laptop", reports=[item]
    )
    assert result == [{"receipt": item["receipt"], "status": "rejected"}]


async def test_unknown_fields_or_bad_types_never_enter_audit():
    for transform in [
        lambda i: i.update(token="synthetic"),
        lambda i: i["report"].update(argv=["secret"]),
        lambda i: i["report"]["scope"].update(network="true"),
        lambda i: i["report"].update(expiresAt=True),
    ]:
        item = envelope()
        store = AuditStore(item)
        transform(item)
        assert (
            await record_agent_reports(
                store, owner=OWNER_A, environment_id=f"{OWNER_A}/laptop", reports=[item]
            )
            == []
        )
        assert store.events == []


async def test_a_database_error_never_sends_receipt():
    item = envelope()
    store = AuditStore(item)
    relay = AuditRelay(store, item)

    async def fail(**kwargs):
        raise RuntimeError("fixture rollback")

    store.append_event = fail
    with pytest.raises(RuntimeError):
        await MachineSync(Stores(store), relay).sync_once()
    assert relay.receipts == []


async def test_unsandboxed_request_can_only_record_a_denial():
    item = envelope()
    item["report"]["scope"]["sandbox"] = "danger-full-access"
    store = AuditStore(item)
    assert (
        await record_agent_reports(
            store,
            owner=OWNER_A,
            environment_id=store.row["environment_id"],
            reports=[item],
        )
        == []
    )
    item["report"]["decision"] = "denied"
    assert await record_agent_reports(
        store, owner=OWNER_A, environment_id=store.row["environment_id"], reports=[item]
    ) == [{"receipt": item["receipt"], "status": "recorded"}]
    assert store.events[0]["payload"]["decision"] == "denied"
