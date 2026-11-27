"""Host-created logical method identities, independent of dispatch attempts.

The existing Task and Attempt remain authoritative for execution. A row here
links their evidence; it never creates a second scheduler or grants authority.
"""

import uuid

from app.application.ports.workbench import WorkbenchStore
from app.domain.workbench.capabilities import digest
from app.domain.workbench.errors import RecordsRefused


async def root_call(repo: WorkbenchStore, task: dict) -> dict:
    """Caller holds the Task lock; method admission owns when a Task may start."""
    owner, session_id, task_id = (
        str(task["owner_actor_id"]),
        str(task["session_id"]),
        str(task["id"]),
    )
    key = "task:" + task_id
    return await repo.add_call(
        {
            "id": str(uuid.uuid4()),
            "owner_actor_id": owner,
            "session_id": session_id,
            "kind": "method",
            "origin": "method",
            "call_key": key,
            "task_id": task_id,
            "digest": digest([key]),
            "status": task["state"].lower(),
            "detail": {
                "task_id": task_id,
                "profile_id": task.get("profile_id"),
                "profile_version": task.get("profile_version"),
                "expert_pack_version": task.get("expert_pack_version"),
                "identity_basis": "host_task",
            },
        }
    )


def step_identity(root_call_id: str, meta: dict) -> tuple[dict, str]:
    identity = {
        key: meta[key]
        for key in (
            "plan_version",
            "plan_digest",
            "pack_digest",
            "instance_id",
            "method_id",
            "method_version",
            "step_index",
        )
    }
    invocation_digest = digest([root_call_id, identity])
    return identity, invocation_digest


def matches_step_digest(stored: str, invocation_digest: str, identity: dict) -> bool:
    """Both recorded forms require a row found under the exact parent-bound call key."""
    return stored == invocation_digest or stored == digest(identity)


async def step_call(repo: WorkbenchStore, task: dict, meta: dict) -> dict:
    """Only the accepted-plan dispatcher supplies meta, never an HTTP/MCP caller."""
    root = await root_call(repo, task)
    identity, invocation_digest = step_identity(root["id"], meta)
    key = "method:" + invocation_digest
    call = await repo.add_call(
        {
            "id": str(uuid.uuid4()),
            "owner_actor_id": str(task["owner_actor_id"]),
            "session_id": str(task["session_id"]),
            "kind": "method",
            "origin": "method",
            "call_key": key,
            "parent_call_id": root["id"],
            "task_id": str(task["id"]),
            "digest": invocation_digest,
            "status": "created",
            "detail": {"identity_basis": "accepted_plan", **identity},
        }
    )
    # Earlier rows used only the step digest. add_call can return one on a
    # retry of the exact owner/session/key, under this same root call.
    if call["parent_call_id"] != root["id"] or not matches_step_digest(
        call["digest"], invocation_digest, identity
    ):
        raise RecordsRefused("method invocation identity changed")
    return call
