import argparse
import hashlib
import json
import sys
from pathlib import Path

parser = argparse.ArgumentParser(
    description="Export provider-owned schemas/examples; optionally update the web consumer lock."
)
parser.add_argument("--consumer", type=Path)
args = parser.parse_args()
b = Path(__file__).resolve().parents[1]
f = args.consumer
sys.path.insert(0, str(b))
from app.domain.workbench.capabilities import (  # noqa: E402
    ActionDecision,
    AssetAction,
    ConnectionCreate,
    ConnectionRenew,
    SelectionPut,
    SkillCreate,
    tool_key,
)
from app.domain.workbench.capability_contract import (  # noqa: E402
    Asset,
    Call,
    Calls,
    Catalog,
    Selection,
)

models = (
    ConnectionCreate,
    ConnectionRenew,
    SkillCreate,
    AssetAction,
    SelectionPut,
    ActionDecision,
    Asset,
    Catalog,
    Selection,
    Call,
    Calls,
)
(b / "contracts/capabilities-v1.json").write_text(
    json.dumps(
        {m.__name__: m.model_json_schema() for m in models},
        ensure_ascii=False,
        indent=2,
    )
    + "\n"
)
ID = "11111111-1111-4111-8111-111111111111"
SID = "22222222-2222-4222-8222-222222222222"
SK = "33333333-3333-4333-8333-333333333333"
CID = "44444444-4444-4444-8444-444444444444"
ACT = "55555555-5555-4555-8555-555555555555"
now = "2026-11-13T00:00:00Z"
key = tool_key(ID, "send")
tool = {
    "name": "send",
    "description": "Send one notice",
    "inputSchema": {
        "type": "object",
        "properties": {"to": {"type": "string"}},
        "required": ["to"],
        "additionalProperties": False,
    },
}
asset = {
    "id": ID,
    "kind": "connection",
    "name": "Office mail",
    "revision": 1,
    "status": "active",
    "default_selected": False,
    "detail": {
        "url": "https://tools.example.test/mcp",
        "account_label": "office@example.test",
        "auth": "bearer",
        "tools": [{**tool, "key": key}],
        "checked_at": now,
    },
    "created_at": now,
    "updated_at": now,
}
skill = {
    "id": SK,
    "kind": "skill",
    "name": "notice",
    "revision": 1,
    "status": "active",
    "default_selected": False,
    "detail": {
        "description": "Write a notice",
        "version": "1",
        "content": "Draft and validate a notice before requesting delivery.",
        "digest": "b" * 64,
        "dependencies": [key],
        "format": "instruction-markdown",
    },
    "created_at": now,
    "updated_at": now,
}
catalog = {
    "assets": [asset, skill],
    "services": [{"url": asset["detail"]["url"], "authentication": ["none", "bearer"]}],
    "skill_formats": ["instruction-markdown"],
    "runtime_enabled": True,
}
selection = {
    "session_id": SID,
    "method": None,
    "revision": 1,
    "tools": [key],
    "skills": [SK],
    "purpose": "Notify reader",
    "editable": True,
    "applied_revision": None,
    "preparation": "not_prepared",
    "issues": [],
    "available_tools": [
        {
            **tool,
            "key": key,
            "connection_id": ID,
            "connection_name": "Office mail",
            "account_label": "office@example.test",
            "status": "active",
        }
    ],
    "available_skills": [skill],
}
call = {
    "id": CID,
    "owner_actor_id": ACT,
    "session_id": SID,
    "connection_id": ID,
    "interaction_id": SK,
    "digest": "d" * 64,
    "status": "awaiting_approval",
    "created_at": now,
    "updated_at": now,
    "detail": {
        "connection_id": ID,
        "revision": 1,
        "connection_name": "Office mail",
        "account_label": "office@example.test",
        "purpose": "Notify reader",
        "tool": tool,
        "arguments": {"to": "reader@example.test"},
        "selection_revision": 1,
        "runtime": "e" * 64,
        "approval_token": "synthetic-approval-only",
    },
}
examples = {
    m.__name__: m.model_validate(v).model_dump(mode="json", exclude_unset=True)
    for m, v in [
        (Asset, asset),
        (Catalog, catalog),
        (Selection, selection),
        (Call, call),
        (Calls, {"calls": [call]}),
    ]
}
text = json.dumps(examples, ensure_ascii=False, indent=2) + "\n"
(b / "contracts/capabilities-v1.samples.json").write_text(text)
if f is None:
    sys.exit(0)
(f / "tests/fixtures/capabilities-v1.samples.json").write_text(text)
(f / "contracts/provider-locks").mkdir(exist_ok=True)
for name in ("capabilities-v1.json", "capabilities-v1.samples.json"):
    data = (b / "contracts" / name).read_bytes()
    (f / "contracts/provider-locks" / name).write_bytes(data)
lock = {
    "provider": "knowbench-backend",
    "contract": "capabilities-v1",
    "sha256": hashlib.sha256(
        (b / "contracts/capabilities-v1.json").read_bytes()
    ).hexdigest(),
    "samples_sha256": hashlib.sha256(text.encode()).hexdigest(),
}
(f / "contracts/provider-locks/capabilities-v1.lock.json").write_text(
    json.dumps(lock, indent=2) + "\n"
)
