"""Validate marketplace references, frozen input bindings and v3 method catalogs."""

from __future__ import annotations

import hashlib
import json
import re
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, ValidationError

from app.domain.workbench.errors import KnowbotInvalid
from app.domain.workbench.method_catalog import MethodCatalog, parse_catalog
from app.domain.workbench.tool_policy import TIER_ORDER, ToolPolicy

PREFIX = "knowbot:"
MANIFEST_SCHEMA_V2 = "knowbot-manifest/v2"
MANIFEST_SCHEMA_V3 = "knowbot-manifest/v3"
RUNTIME = "knowbench"
KNOWBOT_ID = r"[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}"
VERSION = r"[1-9][0-9]{0,5}"
REF = re.compile(rf"^({KNOWBOT_ID})@({VERSION})$")
PROFILE = re.compile(rf"^{PREFIX}({KNOWBOT_ID})@({VERSION})$")
# 分身能用的工具：可在当前步骤选择的 KnowBase 资料函数、要数据、查主体、计算
EXPERT_DATA_TOOLS: frozenset[str] = frozenset(
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
V2_TOOLS: frozenset[str] = EXPERT_DATA_TOOLS | {
    "compute",
    "request_data",
    "find_subject",
    "discover_source_tools",
}
_PLATFORM = ToolPolicy()
_FROZEN = r"^expert:[A-Za-z0-9._-]{1,64}@[0-9][0-9.]{0,31}$"


class FrozenBinding(BaseModel):
    """清单 v2 里作者挑选、交付时冻结的一份（契约 knowbot-manifest/v2）。"""

    model_config = ConfigDict(extra="forbid", frozen=True)

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


def consumer_of(knowbot_id: str, version: str) -> str:
    """在 knowbase 那边这位专家这一版叫什么：解析的使用者、冻结绑定的依据都是它。"""
    return f"expert:{knowbot_id}@{version}"


def no_declarations(manifest: object, *, knowbot_id: str) -> None:
    """清单 v2 不带数据需求声明（decisions ⑭）；带了就是不认得的清单。"""
    if isinstance(manifest, dict) and "data_needs" in manifest:
        raise KnowbotInvalid("a v2 manifest declares no data needs", knowbot=knowbot_id)


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


def catalog_from_manifest(
    manifest: object, *, knowbot_id: str, version: str
) -> MethodCatalog:
    """Validate a frozen catalog without admitting it to the fixed-step runner.

    The execution entry point stays fail-closed until planning attempts, artifact
    bindings, final checks and all runtime views use the accepted task plan.
    """
    if not isinstance(manifest, dict):
        raise KnowbotInvalid("the knowbot manifest is not an object")
    if manifest.get("schema") == MANIFEST_SCHEMA_V2:
        raise KnowbotInvalid("Fixed v2 expert execution has been retired")
    if (
        manifest.get("schema") != MANIFEST_SCHEMA_V3
        or manifest.get("runtime") != RUNTIME
        or manifest.get("version") != version
    ):
        raise KnowbotInvalid(
            "unsupported catalog envelope or version", knowbot=knowbot_id
        )
    if set(manifest) - {
        "schema",
        "runtime",
        "version",
        "pack",
        "name",
        "tagline",
        "bindings",
        "rule_sources",
    }:
        raise KnowbotInvalid("unknown catalog envelope fields", knowbot=knowbot_id)
    try:
        catalog = parse_catalog(manifest.get("pack"))
    except ValidationError as exc:
        where = sorted({".".join(str(p) for p in e["loc"]) for e in exc.errors()})
        raise KnowbotInvalid(
            "invalid method catalog: " + ", ".join(where[:8]), knowbot=knowbot_id
        ) from None
    if catalog.version != version:
        raise KnowbotInvalid("catalog version differs", knowbot=knowbot_id)
    for unit in catalog.method_units:
        step = unit.contract
        if set(step.tools) - V2_TOOLS:
            raise KnowbotInvalid(
                "method uses tools a knowbot cannot use", knowbot=knowbot_id
            )
        # Reuse the same effective platform policy as fixed experts.
        from app.domain.workbench.tool_policy import merge_policy

        try:
            effective = merge_policy(_PLATFORM, step.tool_policy)
        except ValueError:
            raise KnowbotInvalid(
                "method tool policy is looser than the platform", knowbot=knowbot_id
            ) from None
        if _no_looser_than_platform(effective):
            raise KnowbotInvalid(
                "method tool policy is looser than the platform", knowbot=knowbot_id
            )
    frozen_bindings(manifest, knowbot_id=knowbot_id)
    return catalog
