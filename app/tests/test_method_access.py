"""Business ceilings cannot be substituted by technical installation bindings."""

from copy import deepcopy

import pytest
from method_access_support import ACCESS
from pydantic import ValidationError
from test_capabilities_db import OPERATION, URL

from app.application.workbench.method_access import check_access, declared_access
from app.domain.workbench.capabilities import CapabilityError, SkillCreate
from app.domain.workbench.method_capabilities import MethodAccess


def contract():
    return {
        "access_requirements": [deepcopy(ACCESS)],
        "managed_capabilities": {
            "tools": [{"url": URL, "name": "send", "requirement_id": "deliver-notice"}]
        },
    }


def grant():
    method = MethodAccess.model_validate(contract())
    return declared_access(
        {
            "requirements": method.managed_capabilities,
            "access_requirements": method.access_requirements,
        },
        OPERATION,
    )


@pytest.mark.parametrize(
    "mutation", ["missing_id", "unknown_id", "duplicate", "unused", "wildcard", "empty"]
)
def test_invalid_method_permissions_are_not_executable(mutation):
    doc = contract()
    if mutation == "missing_id":
        del doc["managed_capabilities"]["tools"][0]["requirement_id"]
    elif mutation == "unknown_id":
        doc["managed_capabilities"]["tools"][0]["requirement_id"] = "other"
    elif mutation == "duplicate":
        doc["access_requirements"] *= 2
    elif mutation == "unused":
        doc["managed_capabilities"]["tools"] = []
    elif mutation == "wildcard":
        doc["access_requirements"][0]["resources"][0]["glob"] = "*"
    else:
        doc["access_requirements"][0]["resources"][0]["values"] = []
    with pytest.raises(ValidationError):
        MethodAccess.model_validate(doc)


@pytest.mark.parametrize(
    "field,value",
    [("capability_ref", "unreviewed"), ("version", "2"), ("operation", "read")],
)
def test_technical_binding_does_not_substitute_business_permission(field, value):
    policy = OPERATION.model_copy(update={field: value})
    candidate = grant()
    candidate["operation_digest"] = policy.policy_digest
    with pytest.raises(CapabilityError, match="differs"):
        check_access(candidate, policy)


def test_resource_and_outbound_field_limits_include_reads():
    read = OPERATION.model_copy(update={"effect": "read", "operation": "read"})
    candidate = grant()
    candidate["operation_digest"] = read.policy_digest
    candidate["requirement"]["operations"] = ["read"]
    check_access(candidate, read, {"to": "test@example.test"})
    with pytest.raises(CapabilityError, match="resource"):
        check_access(candidate, read, {"to": "other@example.test"})
    with pytest.raises(CapabilityError, match="Outbound"):
        check_access(
            candidate, read, {"to": "test@example.test", "secret": "not-declared"}
        )
    candidate["requirement"]["data_transfer"]["destination"] = (
        "https://other.example.test/mcp"
    )
    with pytest.raises(CapabilityError, match="differs"):
        check_access(candidate, read)


def test_chat_skill_dependencies_do_not_require_method_permissions():
    skill = SkillCreate(
        request_id="ordinary-skill",
        name="notice",
        version="1",
        description="Notice",
        content="Read documentation",
        tool_requirements=[{"url": URL, "name": "send"}],
    )
    assert skill.tool_requirements[0].name == "send"
