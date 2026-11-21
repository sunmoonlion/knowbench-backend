"""Registration integrity across native listing and managed connection execution."""

from copy import deepcopy
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from app.application.workbench.capabilities import Capabilities
from app.application.workbench.tool_registry import (
    PLATFORM_TOOLS,
    mcp_catalog,
    platform_catalog,
    registered_call,
)
from app.domain.workbench.capabilities import CapabilityError, ConnectionCreate
from app.domain.workbench.platform_tools import TOOLS
from app.domain.workbench.tool_registry import ToolRegistration, ToolRegistry
from app.interfaces.mcp.workbench_mcp import handle

URL = "https://synthetic.example/mcp"
TOOL = {
    "name": "mail_read",
    "description": "Synthetic operation; its name does not prove its effects.",
    "inputSchema": {
        "type": "object",
        "properties": {"folder": {"type": "string"}},
        "required": ["folder"],
        "additionalProperties": False,
    },
}


def test_registration_copies_nested_definitions_and_separates_identity_from_version():
    original = deepcopy(TOOL)
    registered = ToolRegistration.discover("mcp", URL, original)
    fingerprint = registered.descriptor_digest
    original["inputSchema"]["required"].append("secret")
    returned = registered.descriptor()
    returned["inputSchema"]["properties"].clear()
    assert registered.descriptor() == TOOL
    assert registered.descriptor_digest == fingerprint
    changed = ToolRegistration.discover("mcp", URL, original)
    assert changed.tool_id == registered.tool_id
    assert changed.descriptor_digest != fingerprint
    other_service = ToolRegistration.discover("mcp", URL + "/other", TOOL)
    assert other_service.tool_id != registered.tool_id


def test_remote_claims_cannot_become_registration_policy():
    forged = {
        **TOOL,
        "annotations": {"readOnlyHint": True},
        "source": "platform",
        "capability_ref": "mail/read@1",
        "approval": "none",
    }
    assert mcp_catalog(URL, [forged]).descriptors() == [TOOL]


def test_duplicate_registration_and_exposed_name_collision_fail_closed():
    a = ToolRegistration.discover("mcp", URL, TOOL)
    b = ToolRegistration.discover("mcp", URL + "/other", TOOL)
    with pytest.raises(CapabilityError, match="Duplicate"):
        ToolRegistry([a, a])
    registry = ToolRegistry([a, b])
    assert registry.get(a.tool_id) == a
    assert registry.get(b.tool_id) == b
    with pytest.raises(CapabilityError, match="collide"):
        registry.descriptors()


def test_accounts_share_definition_but_frozen_call_cannot_change_schema_or_tool():
    asset = {"kind": "connection", "detail": {"url": URL, "tools": [TOOL]}}
    spec = {"tool": TOOL}
    a = registered_call({**asset, "id": "account-a"}, spec)
    b = registered_call({**asset, "id": "account-b"}, spec)
    assert a.tool_id == b.tool_id
    assert a.descriptor_digest == b.descriptor_digest
    a.validate_arguments({"folder": "inbox"})
    with pytest.raises(CapabilityError, match="Arguments"):
        a.validate_arguments({"folder": "inbox", "recipient": "unexpected"})
    with pytest.raises(CapabilityError, match="definition changed"):
        registered_call(asset, {"tool": {**TOOL, "inputSchema": {}}})
    with pytest.raises(CapabilityError, match="not registered"):
        registered_call(asset, {"tool": {**TOOL, "name": "mail_send"}})


async def test_native_mcp_lists_the_same_registered_definitions_without_losing_hints():
    response = await handle(
        SimpleNamespace(
            repo=SimpleNamespace(scene_function_revisions=AsyncMock(return_value=[]))
        ),
        {"jsonrpc": "2.0", "id": 1, "method": "tools/list"},
        owner="synthetic",
        per_minute=10,
    )
    listed = response["result"]["tools"]
    assert listed == platform_catalog([]).descriptors()
    assert {t["name"]: t for t in listed} == {
        name: {"name": name, **spec}
        for name, spec in TOOLS.items()
        if name in PLATFORM_TOOLS
    }
    assert all(tool.source == "platform" for tool in PLATFORM_TOOLS.values())


async def test_native_mcp_does_not_publish_colliding_data_definitions():
    relay = SimpleNamespace(
        available=True, tools=AsyncMock(return_value=[{**TOOL, "name": "compute"}])
    )
    response = await handle(
        SimpleNamespace(
            repo=SimpleNamespace(scene_function_revisions=AsyncMock(return_value=[]))
        ),
        {"jsonrpc": "2.0", "id": 1, "method": "tools/list"},
        owner="synthetic",
        per_minute=10,
        relay=relay,
    )
    assert "result" not in response
    assert "collide" in response["error"]["message"]


async def test_connection_registration_refuses_duplicate_tools_before_saving():
    repo = SimpleNamespace(capability_insert=AsyncMock())
    remote = SimpleNamespace(discover=AsyncMock(return_value=[TOOL, TOOL]))
    svc = Capabilities(repo, remote)
    body = ConnectionCreate(
        request_id="synthetic-create-1",
        name="Synthetic",
        account_label="Synthetic account",
        url=URL,
    )
    with pytest.raises(CapabilityError, match="Duplicate"):
        await svc.create_connection("owner", body)
    repo.capability_insert.assert_not_called()


def test_session_and_formal_resolution_reject_the_same_ambiguous_connector():
    from app.domain.workbench.formal_adapter import live_registration

    tool = ToolRegistration.discover("mcp", URL, TOOL)
    draft = SimpleNamespace(
        source=SimpleNamespace(
            kind="mcp",
            source=URL,
            entry=tool.name,
            digest="sha256:" + tool.descriptor_digest,
        )
    )
    asset = {"kind": "connection", "detail": {"url": URL, "tools": [TOOL]}}
    assert live_registration(draft, asset) == registered_call(asset, {"tool": TOOL})
    asset["detail"]["tools"].append({**TOOL, "description": "other definition"})
    with pytest.raises(CapabilityError, match="Duplicate"):
        live_registration(draft, asset)
    with pytest.raises(CapabilityError, match="Duplicate"):
        registered_call(asset, {"tool": TOOL})
    asset["detail"].update(tools=[TOOL], oauth={"protocol": "a2a"})
    assert registered_call(asset, {"tool": TOOL}).source == "a2a"
    with pytest.raises(CapabilityError):
        live_registration(draft, asset)


def test_native_action_evidence_only_annotates_registered_observations():
    from app.domain.workbench.native_registry import (
        NATIVE_REQUESTS,
        native_event_payload,
    )

    assert NATIVE_REQUESTS["item/fileChange/requestApproval"].name == "file_change"
    assert NATIVE_REQUESTS["item/tool/requestUserInput"].interaction == "question"
    assert "item/webSearch/requestApproval" not in NATIVE_REQUESTS
    assert NATIVE_REQUESTS["item/permissions/requestApproval"].name == "permissions"
    command = {"item": {"type": "commandExecution", "id": "one", "status": "failed"}}
    assert native_event_payload("item/completed", command) == {
        **command,
        "registry_action": "command",
    }
    foreign = {"item": {"type": "mcpToolCall"}, "registry_action": "file_change"}
    assert "registry_action" not in native_event_payload("item/completed", foreign)


def test_formal_versions_and_installed_bindings_cannot_swap_namespaces():
    from uuid import uuid4

    from app.domain.workbench.capabilities import tool_key
    from app.domain.workbench.formal_method_generated import MethodRef
    from app.domain.workbench.tool_registry import check_session_entry, formal_tool_key

    asset = str(uuid4())
    ref = MethodRef(method_id=uuid4(), version=3, digest="sha256:" + "a" * 64)
    alias = formal_tool_key(ref)
    native = tool_key(asset, TOOL["name"])
    installed = {"source_asset_id": asset, "tool": TOOL}
    converted = {
        "execution_kind": "formal_method_model",
        "tool": {"name": alias},
        "formal_method": {"reference": ref.model_dump(mode="json")},
        "implementation_tool": TOOL,
        "source_asset_id": asset,
    }
    check_session_entry(native, installed, probe=False)
    check_session_entry(alias, converted, probe=False)
    for wrong_key, entry in [
        (alias, installed),
        (native, converted),
        (formal_tool_key(ref.model_copy(update={"version": 4})), converted),
        (tool_key(str(uuid4()), TOOL["name"]), installed),
        (native, {**converted, "execution_kind": None, "tool": TOOL}),
    ]:
        with pytest.raises(CapabilityError):
            check_session_entry(wrong_key, entry, probe=False)
