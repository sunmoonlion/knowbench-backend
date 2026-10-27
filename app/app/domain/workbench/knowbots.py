"""用户自己的分身（SDD 0027 P4）：从 knowmart 请来的清单，变成工作台能跑的专家包。

清单外层（`knowbot-manifest/v1`）归 knowmart，里面的 `pack` 就是工作台的专家包形状，由这里
按 ExpertPack 完整校验。分身只归请它的那个用户：专家 id 写作 `knowbot:<分身>@<版本>`，钉在
委托上的是这一版的包快照与摘要，之后编排、底稿、验收都读快照，knowmart 停了也不影响在跑的。

分身是用户的方法，不是我们的：工具与审批只能比平台默认更严，不许自带命令放行前缀；
不许用数据线的工具（`C-L2`：分身运行时只读用户自己的数据）。

- 清单 v1：读用户数据用 `list_my_data`、`read_my_data`、`read_my_table`，读请的人选的
  （或旧格式里作者绑的）那几份（0027 §3.5、0042）。
- 清单 v2（SDD 0043）：数据需求声明换成 knowbase 的 `knowbot-data-needs/v2`，作者绑的
  数据只带冻结的引用（`bindings`）；读数据用 knowbase 数据 MCP 的工具（经工作台中继，
  0048），缺数据用 `request_data` 提出要什么，算数仍用 `compute`。
"""

from __future__ import annotations

import hashlib
import json
import re
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, ValidationError

from app.domain.workbench.errors import KnowbotInvalid
from app.domain.workbench.packs import ExpertPack
from app.domain.workbench.tool_policy import TIER_ORDER, ToolPolicy

PREFIX = "knowbot:"
MANIFEST_SCHEMA = "knowbot-manifest/v1"
MANIFEST_SCHEMA_V2 = "knowbot-manifest/v2"
MANIFEST_SCHEMAS = (MANIFEST_SCHEMA, MANIFEST_SCHEMA_V2)
RUNTIME = "knowbench"
KNOWBOT_ID = r"[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}"
VERSION = r"[1-9][0-9]{0,5}"
REF = re.compile(rf"^({KNOWBOT_ID})@({VERSION})$")
PROFILE = re.compile(rf"^{PREFIX}({KNOWBOT_ID})@({VERSION})$")
# 分身能用的工具：读用户自己的数据（0027 §3.5）、按格子读与计算（0030 §3.1、§3.2）
DATA_TOOLS: frozenset[str] = frozenset(
    {"list_my_data", "read_my_data", "read_my_table", "compute"}
)
KNOWBOT_TOOLS: frozenset[str] = DATA_TOOLS
# 清单 v2：knowbase 数据 MCP 的读工具（工作台中继，0048 第四节）、要数据、计算
DATA_MCP_TOOLS: frozenset[str] = frozenset(
    {
        "list_inputs",
        "read_input",
        "query_facts",
        "query_metric",
        "read_facts",
        "describe_items",
        "search_evidence",
        "lookup",
    }
)
V2_TOOLS: frozenset[str] = DATA_MCP_TOOLS | {"compute", "request_data"}
_PLATFORM = ToolPolicy()
_FROZEN = r"^expert:[A-Za-z0-9._-]{1,64}@[0-9][0-9.]{0,31}$"


class FrozenBinding(BaseModel):
    """清单 v2 里作者绑给这位专家、交付时冻结的一份（契约 knowbot-manifest/v2）。"""

    model_config = ConfigDict(extra="forbid", frozen=True)

    need: str | None = Field(default=None, pattern=r"^[a-z][a-z0-9_]{0,39}$")
    material_id: str = Field(min_length=1, max_length=64)
    version_no: int = Field(ge=1)
    sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    rendition_kind: Literal["text", "pages", "table", "index", "parse"] | None = None
    rendition_no: int | None = Field(default=None, ge=1)
    rendition_sha256: str | None = Field(default=None, pattern=r"^[0-9a-f]{64}$")
    basis_ref: str = Field(pattern=_FROZEN)


def parse_ref(value: str | None) -> tuple[str, str] | None:
    """`<分身>@<版本>`：knowmart 回跳带回来的。不合规则当作没带。"""
    found = REF.fullmatch(value or "")
    return (found.group(1), found.group(2)) if found else None


def profile_id(knowbot_id: str, version: str) -> str:
    return f"{PREFIX}{knowbot_id}@{version}"


def parse_profile(value: str | None) -> tuple[str, str] | None:
    found = PROFILE.fullmatch(value or "")
    return (found.group(1), found.group(2)) if found else None


def is_knowbot(value: str | None) -> bool:
    return (value or "").startswith(PREFIX)


def is_v2(manifest: object) -> bool:
    return isinstance(manifest, dict) and manifest.get("schema") == MANIFEST_SCHEMA_V2


def consumer_of(knowbot_id: str, version: str) -> str:
    """在 knowbase 那边这位专家这一版叫什么：解析的使用者、冻结绑定的依据都是它。"""
    return f"expert:{knowbot_id}@{version}"


def frozen_bindings(manifest: object, *, knowbot_id: str) -> tuple[FrozenBinding, ...]:
    """清单 v2 冻结的作者绑定；都要是这位专家这一版的（knowmart 交付时核过，这里再核）。"""
    raw = manifest.get("bindings") if isinstance(manifest, dict) else None
    if raw is None:
        return ()
    if not isinstance(raw, list) or len(raw) > 200:
        raise KnowbotInvalid(
            "bindings must be a list of at most 200", knowbot=knowbot_id
        )
    try:
        found = tuple(FrozenBinding.model_validate(b) for b in raw)
    except ValidationError:
        raise KnowbotInvalid(
            "a frozen binding is invalid", knowbot=knowbot_id
        ) from None
    version = str(manifest.get("version")) if isinstance(manifest, dict) else ""
    if any(b.basis_ref != consumer_of(knowbot_id, version) for b in found):
        raise KnowbotInvalid(
            "a binding is frozen for another expert version", knowbot=knowbot_id
        )
    return found


def manifest_digest(manifest: dict[str, Any]) -> str:
    """与 knowmart 同一算法：外层规范 JSON（键排序、紧凑分隔、UTF-8）的 sha256。"""
    body = json.dumps(
        manifest, ensure_ascii=False, sort_keys=True, separators=(",", ":")
    ).encode("utf-8")
    return "sha256:" + hashlib.sha256(body).hexdigest()


def _no_looser_than_platform(policy: ToolPolicy) -> list[str]:
    problems = []
    for name in ("file_change_in_project", "file_change_outside", "command", "network"):
        if TIER_ORDER[getattr(policy, name)] < TIER_ORDER[getattr(_PLATFORM, name)]:
            problems.append(f"tool_policy.{name} is looser than the platform")
    if _PLATFORM.questions == "deny" and policy.questions == "allow":
        problems.append("tool_policy.questions is looser than the platform")
    if policy.max_questions > _PLATFORM.max_questions:
        problems.append("tool_policy.max_questions is above the platform")
    if policy.command_allow:
        problems.append("a knowbot cannot allow commands by prefix")
    return problems


def pack_from_manifest(
    manifest: object, *, knowbot_id: str, version: str, profile: str | None = None
) -> ExpertPack:
    """核清单并取出专家包；不合格抛 KnowbotInvalid（只说哪里不对，不回显内容）。

    profile：测试中的草稿还没有分身 id，用测试自己的专家 id（test_runs.trial_profile_id）。
    """
    if not isinstance(manifest, dict):
        raise KnowbotInvalid("the knowbot manifest is not an object")
    problems: list[str] = []
    if manifest.get("schema") not in MANIFEST_SCHEMAS:
        problems.append(f"schema must be one of {', '.join(MANIFEST_SCHEMAS)}")
    if manifest.get("runtime") != RUNTIME:
        problems.append(f"runtime must be {RUNTIME}")
    if manifest.get("version") != version:
        problems.append("version differs from the one summoned")
    raw = manifest.get("pack")
    if not isinstance(raw, dict):
        problems.append("pack must be an object")
    if problems:
        raise KnowbotInvalid("; ".join(problems), knowbot=knowbot_id)
    assert isinstance(raw, dict)
    body = {
        **raw,
        # 身份与给用户看的名字以外层为准：分身的 id 不由清单自己说
        "profile_id": profile or profile_id(knowbot_id, version),
        "name": str(manifest.get("name") or raw.get("name") or ""),
        "tagline": str(manifest.get("tagline") or raw.get("tagline") or ""),
        "internal": False,
        "status": "ready",
        "preparing_note": "",
    }
    try:
        pack = ExpertPack.model_validate(body)
    except ValidationError as exc:
        where = sorted({".".join(str(p) for p in e["loc"]) for e in exc.errors()})
        raise KnowbotInvalid(
            "the knowbot pack is not a valid expert pack: " + ", ".join(where[:8]),
            knowbot=knowbot_id,
        ) from None
    if pack.version != version:
        problems.append("pack.version differs from the version")
    problems += _no_looser_than_platform(pack.tool_policy)
    allowed = V2_TOOLS if is_v2(manifest) else KNOWBOT_TOOLS
    for step in pack.workflow:
        if step.tool_policy is not None and step.tool_policy.command_allow:
            problems.append(f"step {step.step_id} allows commands by prefix")
        extra = set(step.tools) - allowed
        if extra:
            problems.append(f"step {step.step_id} uses tools a knowbot cannot use")
    if pack.requires_tools:
        problems.append("a knowbot cannot use the platform's data")
    if problems:
        raise KnowbotInvalid("; ".join(problems), knowbot=knowbot_id)
    return pack


def data_bindings(manifest: object) -> dict[str, tuple[str, str]]:
    """清单里这一版能读的用户数据：数据 id → （版本，sha256）。"""
    raw = manifest.get("data_bindings") if isinstance(manifest, dict) else None
    found: dict[str, tuple[str, str]] = {}
    for item in raw if isinstance(raw, list) else []:
        if isinstance(item, dict) and isinstance(item.get("dataset_id"), str):
            found[item["dataset_id"]] = (
                str(item.get("version")),
                str(item.get("sha256")),
            )
    return found
