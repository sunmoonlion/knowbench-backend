"""Trusted adapter mappings and exact resource ceilings, without remote access."""

from copy import deepcopy

import pytest
from pydantic import ValidationError

from app.domain.workbench.capabilities import CapabilityError
from app.domain.workbench.tool_operations import (
    McpOperation,
    OperationCatalog,
    ResourceBinding,
)
from app.domain.workbench.tool_registry import ToolRegistration
from core.config import Settings

URL = "https://reviewed.example.test/mcp"
TOOL = {
    "name": "do_it",
    "description": "Only the reviewed adapter defines what this does.",
    "inputSchema": {"type": "object"},
}


def definition(**changes):
    return McpOperation.model_validate(
        {
            "service_url": URL,
            "tool_name": TOOL["name"],
            "descriptor_digest": ToolRegistration.discover(
                "mcp", URL, TOOL
            ).descriptor_digest,
            "capability_ref": "mail",
            "version": "1",
            "operation": "send",
            "effect": "send",
            "resources": [
                {
                    "resource_role": "recipients",
                    "argument_path": ["message", "to"],
                    "cardinality": "many",
                    "allowed_values": ["a@example.test", "b@example.test"],
                }
            ],
            **changes,
        }
    )


def test_resources_resolve_exactly_without_granting_other_accounts_or_resources():
    policy = definition()
    result = policy.resolve({"message": {"to": ["b@example.test", "a@example.test"]}})
    assert result["resources"] == [
        {
            "resource_kind": "connector",
            "resource_role": "recipients",
            "values": ["a@example.test", "b@example.test"],
        }
    ]
    assert result["data_transfer"]["destination"] == URL
    for recipients in [["A@example.test"], ["a@example.test", "outsider"], ["*"]]:
        with pytest.raises(CapabilityError, match="outside"):
            policy.resolve({"message": {"to": recipients}})


@pytest.mark.parametrize(
    "arguments",
    [{}, {"message": []}, {"message": {}}]
    + [
        {"message": {"to": value}}
        for value in [
            None,
            [],
            "a@example.test",
            [1],
            [True],
            [""],
            [["a@example.test"]],
        ]
    ],
)
def test_missing_or_unsupported_resource_never_means_unrestricted(arguments):
    with pytest.raises(CapabilityError):
        definition().resolve(arguments)


def test_single_resource_and_empty_ceiling_are_not_coerced():
    binding = ResourceBinding(resource_role="folder", argument_path=("folder",))
    assert binding.resolve({"folder": "inbox"})["values"] == ["inbox"]
    for value in [[], ["inbox"], False, 123, {}]:
        with pytest.raises(CapabilityError):
            binding.resolve({"folder": value})
    with pytest.raises(ValidationError):
        ResourceBinding(
            resource_role="folder", argument_path=("folder",), allowed_values=()
        )


def test_remote_claims_and_changed_descriptors_cannot_select_or_override_policy():
    forged = {**TOOL, "annotations": {"readOnlyHint": True}, "effect": "read"}
    tool = ToolRegistration.discover("mcp", URL, forged)
    with pytest.raises(CapabilityError, match="reviewed"):
        OperationCatalog().require(tool)
    catalog = OperationCatalog((definition(),))
    assert catalog.require(tool).effect == "send"
    for changed in [
        ToolRegistration.discover("mcp", URL + "/other", TOOL),
        ToolRegistration.discover("mcp", URL, {**TOOL, "description": "changed"}),
        ToolRegistration.discover("platform", URL, TOOL),
    ]:
        with pytest.raises(CapabilityError, match="reviewed"):
            catalog.require(changed)


def test_policy_is_immutable_and_digest_covers_all_constraints():
    policy = definition()
    data = policy.model_dump(mode="json")
    data["resources"][0]["allowed_values"].append("outsider")
    assert policy.resources[0].allowed_values == ("a@example.test", "b@example.test")
    assert McpOperation.model_validate(data).policy_digest != policy.policy_digest
    assert definition(effect="read").policy_digest != policy.policy_digest
    assert definition(version="2").policy_digest != policy.policy_digest
    with pytest.raises(ValidationError):
        policy.operation = "read"


def test_settings_reject_ambiguous_or_unapproved_bindings_and_unknown_conditions(
    monkeypatch,
):
    policy = definition()
    with pytest.raises(ValueError, match="not in the reviewed"):
        Settings(_env_file=None, workbench_capability_operations=(policy,))
    with pytest.raises(ValueError, match="Duplicate"):
        Settings(
            _env_file=None,
            workbench_capability_mcp_urls=[URL],
            workbench_capability_operations=(policy, policy),
        )
    for changes in [
        {"effect": "unknown"},
        {"approval": "none"},
        {"service_url": "https://user:secret@example.test/mcp"},
        {"service_url": URL + "?token=value"},
    ]:
        with pytest.raises(ValidationError):
            definition(**changes)
    resource = policy.resources[0].model_dump(mode="json")
    for changes in [
        {"predicate": "allow()"},
        {"argument_path": []},
        {"allowed_values": ["a", "a"]},
    ]:
        with pytest.raises(ValidationError):
            definition(resources=[{**resource, **changes}])
    with pytest.raises(ValidationError, match="Duplicate"):
        definition(resources=[resource, deepcopy(resource)])
    monkeypatch.setenv("WORKBENCH_CAPABILITY_MCP_URLS", '["' + URL + '"]')
    monkeypatch.setenv(
        "WORKBENCH_CAPABILITY_OPERATIONS", "[" + policy.model_dump_json() + "]"
    )
    assert Settings(_env_file=None).workbench_capability_operations == (policy,)
