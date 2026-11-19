"""Stable identities for genuine nested formal calls; no authority or dispatch."""

from app.domain.workbench.capabilities import digest
from app.domain.workbench.formal_execution import expanded_plan, snapshot_for_task

TREE_ADAPTER = "knowbench/formal-orchestration-tree-host/v1"


def node_key(task_id, path):
    return "formal-method-node:" + str(task_id) + ":" + digest(list(path))


def node_slot(path):
    return "node:" + digest(list(path))


def node_artifact(path, *, exports=False):
    return ("formal-exports:" if exports else "formal-node:") + digest(list(path))


def node_identity(task, node):
    snapshot = snapshot_for_task(task)
    parent_definition = snapshot.draft.definition
    if node.parent_path:
        parent_definition = next(
            n.definition
            for n in expanded_plan(snapshot).nodes
            if n.path == node.parent_path
        )
    return {
        "identity_basis": "formal_recursive",
        "task_id": str(task["id"]),
        "snapshot_digest": task["pack_digest"],
        "path": list(node.path),
        "parent_path": list(node.parent_path),
        "reference": node.reference.model_dump(mode="json"),
        "step_digest": digest(node.step.model_dump(mode="json", by_alias=True)),
        "parent_definition_digest": parent_definition.digest(),
    }
