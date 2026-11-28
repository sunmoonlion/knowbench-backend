"""工具级审批的策略（approval.md「工具级：策略」）：三档、分类、步骤只能更严、授权匹配。纯函数。"""

import pytest

from app.domain.workbench.tool_policy import (
    CATEGORY_COMMAND,
    CATEGORY_FILE_IN,
    CATEGORY_FILE_OUT,
    CATEGORY_OTHER,
    ToolPolicy,
    ToolPolicyOverride,
    classify,
    decide,
    dependents,
    grant_matches,
    grant_prefix,
    merge_policy,
)


def test_defaults_follow_the_table():
    p = ToolPolicy()
    assert decide(p, CATEGORY_FILE_IN, ()) == "allow"
    assert decide(p, CATEGORY_FILE_OUT, ()) == "deny"
    assert decide(p, CATEGORY_COMMAND, ("git", "status")) == "ask"
    assert decide(p, "network", ()) == "deny"
    # 没写到的种类：问人，不可配置
    assert decide(p, CATEGORY_OTHER, ()) == "ask"


def test_command_allow_prefix_matches_argument_by_argument():
    p = ToolPolicy(command_allow=(("git", "status"), ("python", "-m", "pytest")))
    assert decide(p, CATEGORY_COMMAND, ("git", "status", "--short")) == "allow"
    assert decide(p, CATEGORY_COMMAND, ("git", "push")) == "ask"
    # 前缀必须逐个参数相等，不是子串
    assert decide(p, CATEGORY_COMMAND, ("git", "status-x")) == "ask"


def test_after_a_decline_permitted_operations_ask_and_prohibitions_hold():
    p = ToolPolicy()
    assert decide(p, CATEGORY_FILE_IN, (), after_decline=True) == "ask"
    assert decide(p, CATEGORY_FILE_OUT, (), after_decline=True) == "deny"


def test_classify_reads_the_request():
    assert classify(
        "item/commandExecution/requestApproval", {"command": "touch /x/y"}
    ) == (CATEGORY_COMMAND, ("touch", "/x/y"))
    assert classify(
        "item/commandExecution/requestApproval", {"command": ["git", "status"]}
    ) == (CATEGORY_COMMAND, ("git", "status"))
    assert classify("item/fileChange/requestApproval", {}) == (CATEGORY_FILE_IN, ())
    assert classify(
        "item/fileChange/requestApproval", {"grantRoot": "/home/u/other"}
    ) == (CATEGORY_FILE_OUT, ())
    assert classify("item/something/requestApproval", {}) == (CATEGORY_OTHER, ())


def test_steps_can_only_tighten():
    base = ToolPolicy()
    tighter = merge_policy(base, ToolPolicyOverride(file_change_in_project="ask"))
    assert tighter.file_change_in_project == "ask"
    added = merge_policy(base, ToolPolicyOverride(command_allow=(("ls",),)))
    assert added.command_allow == (("ls",),)
    with pytest.raises(ValueError):
        merge_policy(base, ToolPolicyOverride(command="allow"))
    with pytest.raises(ValueError):
        merge_policy(base, ToolPolicyOverride(file_change_outside="ask"))
    with pytest.raises(ValueError):
        merge_policy(
            ToolPolicy(questions="deny"), ToolPolicyOverride(questions="allow")
        )


def test_task_grant_matches_same_category_and_prefix():
    tokens = ("python", "scripts/build.py", "--all")
    prefix = grant_prefix(CATEGORY_COMMAND, tokens)
    assert prefix == ["python", "scripts/build.py"]
    grant = {"category": CATEGORY_COMMAND, "command_prefix": prefix}
    assert grant_matches(grant, CATEGORY_COMMAND, tokens)
    assert grant_matches(grant, CATEGORY_COMMAND, ("python", "scripts/build.py"))
    assert not grant_matches(grant, CATEGORY_COMMAND, ("python", "other.py"))
    assert not grant_matches(grant, CATEGORY_FILE_IN, ())
    # 文件类授权不记前缀，同类即匹配
    assert grant_prefix(CATEGORY_FILE_OUT, ()) is None
    assert grant_matches(
        {"category": CATEGORY_FILE_OUT, "command_prefix": None}, CATEGORY_FILE_OUT, ()
    )


def test_dependents_follow_input_refs():
    class S:
        def __init__(self, step_id, out, refs=()):
            self.step_id, self.output_artifact, self.input_refs, self.title = (
                step_id,
                out,
                refs,
                step_id,
            )

    wf = [S("a", "A"), S("b", "B", ("A",)), S("c", "C", ("A", "B")), S("d", "D")]
    assert [d["step_id"] for d in dependents(wf, "a")] == ["b", "c"]
    assert dependents(wf, "d") == []
