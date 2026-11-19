"""Owner-visible terms and exact matching for reviewed connector actions."""

import json
from copy import deepcopy
from datetime import datetime

from app.domain.workbench.capabilities import CapabilityError, digest
from app.domain.workbench.method_authorization import MethodAuthorization


async def read_offer(repo, owner, intent, runtime, tool_key):
    op = intent.get("operation", {})
    if not op.get("read_result_id_field") or not intent["purpose"].strip():
        return {}
    # Only the reviewed selector can narrow. Filters, cursors and all other outbound data stay fixed.
    terms = {
        k: intent[k]
        for k in (
            "source_asset_id",
            "revision",
            "purpose",
            "selection_revision",
            "method",
            "tool",
        )
    }
    remaining = deepcopy(intent["arguments"])
    parent = remaining
    path = op["read_resource_path"]
    for part in path[:-1]:
        parent = parent[part]
    del parent[path[-1]]
    terms.update(
        operation=op["policy_digest"], arguments=remaining, access=intent.get("access")
    )
    until = datetime.fromisoformat(runtime["expires_at"])
    binding = intent.get("method")
    if binding:
        raw = await repo.method_authorization(owner, binding["call_id"])
        if raw is None:
            raise CapabilityError("Method read authority is unavailable")
        authority = MethodAuthorization.model_validate(raw)
        grant = next(
            (
                g
                for g in authority.grants
                if g.grant_id == digest([binding["context"], tool_key])
            ),
            None,
        )
        if not grant or authority.revoked:
            raise CapabilityError("Method read scope is unavailable")
        until = min(until, authority.expires_at, grant.expires_at)
        terms["method"] = {
            k: binding[k] for k in ("task_id", "call_id", "context", "step_id")
        }
        terms["authority_revision"] = authority.revision
        terms["parent_revision"] = authority.parent_revision
    return {"read_offer_key": digest(terms), "read_offer_until": until.isoformat()}


def action_digest(detail):
    return digest(
        {
            k: v
            for k, v in detail.items()
            if k
            not in (
                "approval_token",
                "result",
                "read_approval_id",
                "read_approval_until",
            )
        }
    )


def scoped_result(operation, result):
    field = operation.get("read_result_id_field")
    if not field or result.get("isError"):
        return result if not field else {"isError": True, "content": []}
    payload = result.get("structuredContent")
    items_field = operation["read_result_items_field"]
    records = (
        payload.get(items_field)
        if items_field and isinstance(payload, dict)
        else [payload]
    )
    allowed = set(operation["resources"][0]["values"])
    if not isinstance(records, list) or len(records) > len(allowed):
        raise CapabilityError("The provider result exceeds the approved resource set")
    fields = (field, *operation["read_result_fields"])
    seen = set()
    rows = []
    for record in records:
        if (
            not isinstance(record, dict)
            or not isinstance(record.get(field), str)
            or record[field] not in allowed
            or record[field] in seen
            or any(
                k not in record or isinstance(record[k], (dict, list)) for k in fields
            )
        ):
            raise CapabilityError(
                "The provider result does not match its reviewed resource and fields"
            )
        seen.add(record[field])
        rows.append({k: record[k] for k in fields})
    projected = {items_field: rows} if items_field else rows[0]
    return {
        "isError": False,
        "structuredContent": projected,
        "content": [
            {"type": "text", "text": json.dumps(projected, ensure_ascii=False)}
        ],
    }


def approval_preview(call):
    detail = call["detail"]
    op = detail.get("operation") or {}
    if op.get("effect") not in ("read", "write", "send") or "data_transfer" not in op:
        return None
    return {
        "verified_zero_cost": (op.get("formal_execution_cost") or {}).get(
            "incremental_cost"
        )
        == "0",
        "effect": op["effect"],
        "operation": op["operation"],
        "resources": op["resources"],
        "destination": op["data_transfer"]["destination"],
        "arguments_digest": op["data_transfer"]["arguments_digest"],
        "attachments": op.get("attachments", []),
        "read_until": detail.get("read_approval_until", detail.get("read_offer_until")),
        "granted": bool(call.get("read_scope_key")),
        "revoked": bool(
            call.get("read_revoked_at") or call.get("origin_read_revoked_at")
        ),
        "origin": detail.get("read_approval_id"),
    }
