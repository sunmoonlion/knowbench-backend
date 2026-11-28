"""工具级审批的策略：三档表、分类、匹配。纯函数，不碰数据库。

设计：`SDD/architecture/approval.md`「工具级：策略」「问人怎么问」「被拒之后」。
- 三档：allow（回 accept，来源 policy）、ask（挂起问人）、deny（回 decline，来源 policy）。
- 没写到的请求种类一律问人，不可配置。
- 步骤级覆盖只能比专家包更严或更具体。
- 同一步里刚拒绝过，随后任何请求一律问人。
"""

from __future__ import annotations

import shlex
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field

Tier = Literal["allow", "ask", "deny"]
TIER_ORDER: dict[str, int] = {"allow": 0, "ask": 1, "deny": 2}

CATEGORY_FILE_IN = "file_change_in_project"
CATEGORY_FILE_OUT = "file_change_outside"
CATEGORY_COMMAND = "command"
CATEGORY_NETWORK = "network"
CATEGORY_OTHER = "other"

# 四个按钮（approval.md「问人怎么问」第 3 条）。工作台永远不向 Codex 发 acceptForSession
DECISION_ACCEPT = "accept"
DECISION_ACCEPT_TASK = "accept_task"
DECISION_DECLINE = "decline"
DECISION_CANCEL = "cancel"
DECISIONS = (DECISION_ACCEPT, DECISION_ACCEPT_TASK, DECISION_DECLINE, DECISION_CANCEL)

# 回给 Codex 的只有这三个
CODEX_DECISIONS = {"accept", "decline", "cancel"}


class _Strict(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)


class ToolPolicy(_Strict):
    """专家包里的三档表。默认值就是 approval.md 的表。"""

    file_change_in_project: Tier = "allow"
    file_change_outside: Tier = "deny"
    command: Tier = "ask"
    # command 为 ask 时仍可直接放行的命令前缀，逐个参数精确匹配
    command_allow: tuple[tuple[str, ...], ...] = ()
    network: Tier = "deny"
    # Codex 反过来问用户（request_user_input）：allow / deny；每步最多几问
    questions: Literal["allow", "deny"] = "allow"
    max_questions: int = Field(default=3, ge=0, le=10)

    def tier_of(self, category: str) -> Tier:
        if category == CATEGORY_FILE_IN:
            return self.file_change_in_project
        if category == CATEGORY_FILE_OUT:
            return self.file_change_outside
        if category == CATEGORY_COMMAND:
            return self.command
        if category == CATEGORY_NETWORK:
            return self.network
        return "ask"  # 其他：问人，不可配置


class ToolPolicyOverride(_Strict):
    """步骤级覆盖。每一项都可以不给；给了只能更严，command_allow 只能加。"""

    file_change_in_project: Tier | None = None
    file_change_outside: Tier | None = None
    command: Tier | None = None
    command_allow: tuple[tuple[str, ...], ...] = ()
    network: Tier | None = None
    questions: Literal["allow", "deny"] | None = None
    max_questions: int | None = Field(default=None, ge=0, le=10)


def merge_policy(base: ToolPolicy, override: ToolPolicyOverride | None) -> ToolPolicy:
    """步骤覆盖专家包：只能更严或更具体。放宽直接报错，让专家包在加载时就被拒。"""
    if override is None:
        return base
    fields: dict[str, Any] = {}
    for name in ("file_change_in_project", "file_change_outside", "command", "network"):
        want = getattr(override, name)
        have = getattr(base, name)
        if want is None:
            continue
        if TIER_ORDER[want] < TIER_ORDER[have]:
            raise ValueError(f"step tool_policy relaxes {name}: {have} -> {want}")
        fields[name] = want
    if override.questions is not None:
        if base.questions == "deny" and override.questions == "allow":
            raise ValueError("step tool_policy relaxes questions: deny -> allow")
        fields["questions"] = override.questions
    if override.max_questions is not None:
        if override.max_questions > base.max_questions:
            raise ValueError("step tool_policy raises max_questions")
        fields["max_questions"] = override.max_questions
    if override.command_allow:
        fields["command_allow"] = tuple(base.command_allow) + tuple(
            override.command_allow
        )
    return base.model_copy(update=fields)


def command_tokens(command: Any) -> tuple[str, ...]:
    """Codex 给的命令可能是字符串，也可能已经是参数列表。"""
    if isinstance(command, (list, tuple)):
        return tuple(str(x) for x in command)
    if isinstance(command, str) and command.strip():
        try:
            return tuple(shlex.split(command))
        except ValueError:
            return tuple(command.split())
    return ()


def classify(method: str, params: dict[str, Any]) -> tuple[str, tuple[str, ...]]:
    """请求 → 类别 + 命令参数。

    文件改动的请求不带路径，只带 `grant_root`：Codex 要求把项目外的某个目录放开写时才给它。
    所以有 grant_root 算项目外，没有算项目内（untrusted 策略下每个补丁都会来问，那些是项目内）。
    """
    if method.startswith("item/commandExecution/"):
        return CATEGORY_COMMAND, command_tokens(params.get("command"))
    if method.startswith("item/fileChange/"):
        if params.get("grantRoot") or params.get("grant_root"):
            return CATEGORY_FILE_OUT, ()
        return CATEGORY_FILE_IN, ()
    if method.startswith("item/permissions/"):
        perms = params.get("permissions") or {}
        if isinstance(perms, dict) and perms.get("network"):
            return CATEGORY_NETWORK, ()
        return CATEGORY_OTHER, ()
    return CATEGORY_OTHER, ()


def prefix_matches(prefix: tuple[str, ...], tokens: tuple[str, ...]) -> bool:
    return bool(prefix) and tuple(tokens[: len(prefix)]) == tuple(prefix)


def decide(
    policy: ToolPolicy,
    category: str,
    tokens: tuple[str, ...],
    *,
    after_decline: bool = False,
) -> Tier:
    """三档决定。刚拒绝过的同一步里，随后任何请求都问人（approval.md「被拒之后」）。"""
    tier = policy.tier_of(category)
    if tier == "deny":
        return "deny"
    if after_decline:
        return "ask"
    if category == CATEGORY_COMMAND and tier == "ask":
        if any(prefix_matches(p, tokens) for p in policy.command_allow):
            return "allow"
    return tier


def grant_prefix(category: str, tokens: tuple[str, ...]) -> list[str] | None:
    """「这个任务里都允许」记什么前缀：命令记可执行文件加子命令（至多两个参数），别的类别不记。"""
    if category != CATEGORY_COMMAND:
        return None
    return list(tokens[:2]) if tokens else []


def grant_matches(
    grant: dict[str, Any], category: str, tokens: tuple[str, ...]
) -> bool:
    if grant.get("category") != category:
        return False
    prefix = grant.get("command_prefix")
    if category != CATEGORY_COMMAND:
        return True
    if not prefix:
        return False
    return prefix_matches(tuple(str(x) for x in prefix), tokens)


def dependents(workflow: list[Any], step_id: str) -> list[dict[str, str]]:
    """这一步之后还有哪些步骤吃它的交回物（按 input_refs 机械算）。"""
    out: list[dict[str, str]] = []
    produced = None
    for st in workflow:
        if st.step_id == step_id:
            produced = st.output_artifact
            break
    if produced is None:
        return out
    for st in workflow:
        if st.step_id != step_id and produced in st.input_refs:
            out.append({"step_id": st.step_id, "title": st.title})
    return out
