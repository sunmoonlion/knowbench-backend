from copy import deepcopy

from app.domain.workbench.capabilities import SkillCreate, tool_key
from app.domain.workbench.skill_dependencies import skill_dependencies

URL = "https://mail.example.test/mcp"


def account(identity, status="active"):
    return {
        "id": identity,
        "kind": "connection",
        "name": identity,
        "status": status,
        "detail": {
            "url": URL,
            "account_label": identity + "@example.test",
            "tools": [{"name": "send"}],
        },
    }


def test_portable_requirements_do_not_bind_accounts_or_select_tools():
    skill = {"detail": {"tool_requirements": [{"url": URL, "name": "send"}]}}
    assert skill_dependencies(skill, [])[0]["status"] == "connection_required"
    assets = [account("work", "disabled"), account("personal")]
    before = deepcopy(assets)
    assert skill_dependencies(skill, assets)[0]["status"] == "available"
    assert (
        skill_dependencies(skill, assets, [tool_key("work", "send")])[0]["status"]
        == "selection_required"
    )
    result = skill_dependencies(skill, assets, [tool_key("personal", "send")])[0]
    assert result["status"] == "selected" and len(result["candidates"]) == 2
    assert assets == before


def test_legacy_dependency_never_switches_accounts_on_disconnect():
    skill = {"detail": {"dependencies": [tool_key("work", "send")]}}
    assets = [account("work", "disconnected"), account("personal")]
    dependency = skill_dependencies(skill, assets, [tool_key("personal", "send")])[0]
    assert dependency["status"] == "connection_inactive"
    assert [c["connection_id"] for c in dependency["candidates"]] == ["work"]
    assert skill_dependencies(skill, assets[1:])[0]["status"] == "connection_required"


def test_empty_extension_preserves_legacy_install_request_digest_input():
    skill = SkillCreate(
        request_id="install-test",
        name="notice",
        description="Notice",
        version="1",
        content="Read first",
    )
    assert "tool_requirements" not in skill.model_dump()


def test_method_scope_covers_dependencies_without_changing_legacy_snapshots():
    import pytest
    from pydantic import ValidationError

    from app.domain.workbench.method_capabilities import (
        MethodCapabilities,
        matches_skill,
    )

    need = {"url": URL, "name": "send"}
    requirement = {"name": "notice", "version": "1", "digest": "a" * 64}
    legacy = MethodCapabilities(tools=[need], skills=[requirement])
    assert legacy.model_dump(mode="json")["skills"] == [requirement]
    with pytest.raises(ValidationError, match="method tool scope"):
        MethodCapabilities(tools=[], skills=[{**requirement, "required_tools": [need]}])
    scope = MethodCapabilities(
        tools=[need], skills=[{**requirement, "required_tools": [need]}]
    )
    skill = {
        "name": "notice",
        "detail": {"version": "1", "digest": "a" * 64, "tool_requirements": [need]},
    }
    assert matches_skill(scope, skill, [])
    skill["detail"]["tool_requirements"].append({"url": URL, "name": "delete"})
    assert not matches_skill(scope, skill, [])
    assert not matches_skill(legacy, skill, [])
