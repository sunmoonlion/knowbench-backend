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
    MethodPermissionRevoke,
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
from app.domain.workbench.formal_method_calls import (  # noqa: E402
    FormalMethodCallRequest,
    FormalMethodCatalogResponse,
    FormalMethodInvocation,
    FormalMethodInvocations,
)
from app.domain.workbench.skill_dependencies import public_skill_asset  # noqa: E402
from app.domain.workbench.skill_execution import public_call  # noqa: E402
from app.domain.workbench.skill_installation import (  # noqa: E402
    SkillInstall,
    SkillSourcePreview,
    SkillSourceRequest,
)
from app.domain.workbench.skill_package import SkillPackageCreate  # noqa: E402

models = (
    ConnectionCreate,
    ConnectionRenew,
    SkillCreate,
    SkillPackageCreate,
    SkillSourceRequest,
    SkillSourcePreview,
    SkillInstall,
    AssetAction,
    SelectionPut,
    ActionDecision,
    MethodPermissionRevoke,
    Asset,
    Catalog,
    Selection,
    Call,
    Calls,
    FormalMethodCallRequest,
    FormalMethodCatalogResponse,
    FormalMethodInvocation,
    FormalMethodInvocations,
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
    "skill_mode": "explicit",
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
    "source_asset_id": ID,
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
catalog["assets"][1] = public_skill_asset(skill, [asset])
selection["available_skills"] = [public_skill_asset(skill, [asset], [key])]
call = public_call(call)
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
host = {
    **call,
    "id": "66666666-6666-4666-8666-666666666666",
    "detail": {
        **{
            k: v
            for k, v in call["detail"].items()
            if k not in ("runtime", "selection_revision", "method")
        },
        "execution_kind": "formal_mcp",
    },
}
examples["HostCall"] = Call.model_validate(host).model_dump(
    mode="json", exclude_unset=True
)
examples["Calls"] = Calls.model_validate({"calls": [call, host]}).model_dump(
    mode="json", exclude_unset=True
)
formal_call = {
    **host,
    "id": "77777777-7777-4777-8777-777777777777",
    "detail": {**host["detail"], "execution_kind": "formal_method"},
}
reference = {
    "method_id": "88888888-8888-4888-8888-888888888888",
    "version": 1,
    "digest": "sha256:" + "a" * 64,
}
invocation = {
    "request_id": "99999999-9999-4999-8999-999999999999",
    "method_call_id": "aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaaa",
    "reference": reference,
    "title": "Validated private notice",
    "status": "awaiting_approval",
    "call": formal_call,
    "outputs": None,
    "detail": "Awaiting the owner's exact approval; no operation has run.",
}
slot = {
    "description": "Recipient or receipt",
    "schema": {"type": "string"},
    "required": True,
    "meaning": {},
    "on_missing": "fail",
}
formal_item = {
    "reference": reference,
    "title": "Validated private notice",
    "purpose": "Send one confirmed notice",
    "inputs": {"to": slot},
    "outputs": {"receipt": slot},
    "access_requirements": [
        {
            "requirement_id": "deliver-notice",
            "capability_ref": "synthetic-mail",
            "version": "1",
            "operations": ["send"],
            "resources": [
                {
                    "resource_kind": "connector",
                    "resource_role": "recipient",
                    "values": ["reader@example.test"],
                }
            ],
            "data_transfer": {
                "destination": "https://tools.example.test/mcp",
                "argument_fields": ["to"],
                "purpose": "Deliver the confirmed notice",
            },
            "required_when": "always",
            "on_unavailable": "pause",
        }
    ],
    "uncovered": [
        {
            "scenario": "Supplier business effectiveness",
            "reason": "Typed assertions do not prove all supplier behavior",
            "scope": "Private use of the tested method",
        }
    ],
    "available": True,
    "issue": None,
}
for model, value in [
    (
        FormalMethodCallRequest,
        {
            "request_id": invocation["request_id"],
            "reference": reference,
            "values": {"to": "reader@example.test"},
        },
    ),
    (
        FormalMethodCatalogResponse,
        {
            "items": [
                formal_item,
                {
                    **formal_item,
                    "reference": {**reference, "version": 2},
                    "available": False,
                    "issue": "Source execution rights are no longer current",
                },
            ]
        },
    ),
    (FormalMethodInvocation, invocation),
    (FormalMethodInvocations, {"calls": [invocation]}),
]:
    examples[model.__name__] = model.model_validate(value).model_dump(
        mode="json", by_alias=True
    )
examples["FormalCall"] = Call.model_validate(formal_call).model_dump(
    mode="json", exclude_unset=True
)
examples["Calls"] = Calls.model_validate(
    {"calls": [call, host, formal_call]}
).model_dump(mode="json", exclude_unset=True)
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
