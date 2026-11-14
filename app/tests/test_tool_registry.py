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
        SimpleNamespace(),  # no execution or repository is needed to list definitions
        {"jsonrpc": "2.0", "id": 1, "method": "tools/list"},
        owner="synthetic",
        per_minute=10,
    )
    listed = response["result"]["tools"]
    assert listed == platform_catalog([]).descriptors()
    assert {t["name"]: t for t in listed} == {
        name: {"name": name, **spec} for name, spec in TOOLS.items()
    }
    assert all(tool.source == "platform" for tool in PLATFORM_TOOLS.values())


async def test_native_mcp_does_not_publish_colliding_data_definitions():
    relay = SimpleNamespace(tools=AsyncMock(return_value=[{**TOOL, "name": "compute"}]))
    response = await handle(
        SimpleNamespace(),
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
