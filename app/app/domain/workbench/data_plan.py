"""清单 v2 的专家：按 knowbase 的解析清单定这件委托用哪几份（SDD 0043 第 3.2–3.6 节）。

纯函数，不碰网络。输入是解析清单（knowbase 回的字典）、这件委托提出的几样（第 0 步或
运行中 `request_data` 提的，用户可能改过，decisions ⑭）、作者冻结的绑定、用户确认时
换过的（条目 → 资料与版本）与去掉的条目；输出：

- pins：要钉进委托、签进运行授权的那几份（每份带依据）；
- confirm：要用户确认、交 knowfeed 补采的条目（待确认的）；
- problems：确认不了的原因（有歧义没选、换选的不在候选里……），白话；
- incomplete：缺了的、用户去掉的（照做、记「数据不全」）。

规矩：已定的默认用 knowbase 挑的那份，用户可以换成候选里的另一份；有歧义的必须选；
待确认的在补采开着时进确认（0043 第 4.1 节「knowfeed 切合上线之前，待确认一律回暂不能
补采」）。数据是用户的，由用户定（第 0 步与运行中提的都这样，decisions ⑭）：哪一样
都能去掉，标了必需的缺了也照做，记「数据不全」；有歧义的仍要选一份或去掉。
"""

from __future__ import annotations

import hashlib
import json
import uuid
from dataclasses import dataclass, field
from typing import Any

from app.domain.workbench.data_request import DataRequest
from app.domain.workbench.knowbots import FrozenBinding

APP_BASIS_REF = "app:knowbench"
PUBLIC_OWNER = "public:disclosure"
# 一条运行授权最多这么多份（knowbase MAX_PINS）
MAX_PINS = 200

_REASONS = {
    "not_shown": "在 knowbase 里还没放开给 knowbench",
    "not_collectable": "采不来，要本人到 knowbase 上传",
    "unusable": "这份资料不能用（已禁用或许可不允许）",
    "ambiguous": "有几份对得上，要选一份",
    "waiting": "采集状态尚未核实，请重新核对采单",
    "to_confirm": "暂不能补采",
}


def gap(item: str, required: bool) -> dict[str, Any]:
    """缺的一条：白话（同 incomplete 里那条）与是不是必需的。"""
    return {"item": item, "required": bool(required)}


def short_of_required(gaps: list[dict[str, Any]]) -> bool:
    """「数据不全」：必需的缺了（用户选了照做）；可选的去掉或缺了不算（0043 第 3.6 节）。"""
    return any(g.get("required") for g in gaps)


def item_ref(item: dict[str, Any]) -> str:
    """清单里一个条目的编号：哪一样 + 描述键（同一修订里唯一，跨修订稳定）。"""
    body = json.dumps(item.get("key") or {}, sort_keys=True, ensure_ascii=False)
    digest = hashlib.sha256(body.encode("utf-8")).hexdigest()[:12]
    return f"{item.get('need_key')}:{digest}"


@dataclass
class Plan:
    pins: list[dict[str, Any]] = field(default_factory=list)
    confirm: list[dict[str, Any]] = field(default_factory=list)
    problems: list[str] = field(default_factory=list)
    incomplete: list[str] = field(default_factory=list)
    # 补采结束后还没取到的（也在 incomplete 里）：等数据那条线据此停下问用户
    missing: list[str] = field(default_factory=list)
    # incomplete、missing 的每一条带上是不是必需的（{item, required}）：只有必需的缺了、
    # 用户选了照做，产出才标「数据不全」（0043 第 3.6 节；可选的去掉或缺了不算）
    gaps: list[dict[str, Any]] = field(default_factory=list)
    missing_gaps: list[dict[str, Any]] = field(default_factory=list)

    @property
    def grant_items(self) -> list[dict[str, Any]]:
        """签运行授权要的那几项（knowbase GrantItemIn 的形状）。"""
        out = []
        for p in self.pins:
            item = {
                "material_id": p["item_id"],
                "version_no": p["version_no"],
                "sha256": p["sha256"],
                "basis": p["basis"],
                "basis_ref": p["basis_ref"],
            }
            if p.get("rendition_kind"):
                item["rendition_kind"] = p["rendition_kind"]
                item["rendition_no"] = p["rendition_no"]
                item["rendition_sha256"] = p["rendition_sha256"]
            out.append(item)
        return out


def plan(
    resolution: dict[str, Any],
    needs: list[DataRequest],
    bindings: tuple[FrozenBinding, ...] = (),
    *,
    picks: dict[str, dict[str, Any]] | None = None,
    dropped: list[str] | None = None,
    supply_enabled: bool = False,
    after_supply: bool = False,
) -> Plan:
    """after_supply：补采已经结束后的重解析（还没定的就是没取到，不再进确认）。"""
    picks = picks or {}
    gone = set(dropped or ())
    by_key = {n.key: n for n in needs}
    out = Plan()
    seen: set[tuple[str, int]] = set()
    known_refs = {item_ref(i) for i in resolution.get("items") or []}
    for ref in list(picks) + list(gone):
        if ref not in known_refs:
            out.problems.append("换选或去掉的条目不在这张清单里")
            break
    for item in resolution.get("items") or []:
        ref = item_ref(item)
        need = by_key.get(str(item.get("need_key")))
        if need is None:
            out.problems.append("清单里有专家没声明的条目")
            continue
        label = _label(need, item)
        if ref in gone:
            out.incomplete.append(f"{label}：你没要")
            out.gaps.append(gap(f"{label}：你没要", need.required))
            continue
        status = str(item.get("status"))
        chosen = _chosen(item, picks.get(ref))
        if chosen is None:
            out.problems.append(f"{label}：换选的那一份不在候选里")
            continue
        if status == "resolved" or (status == "ambiguous" and chosen):
            for candidate in chosen:
                pin = _pin(need.key, candidate)
                key = (pin["item_id"], pin["version_no"])
                if key not in seen:
                    seen.add(key)
                    out.pins.append(pin)
            continue
        if status == "waiting" and not after_supply:
            # 旧解析可能只有确认记录、根本没有 demand；不能据此跳过补采或默认缺数照做。
            out.problems.append(f"{label}：{_REASONS['waiting']}")
            continue
        if status == "to_confirm" and supply_enabled and not after_supply:
            out.confirm.append(
                {
                    "key": item.get("key"),
                    "need_key": need.key,
                    "required": need.required,
                    "ref": ref,
                    "label": label,
                }
            )
            continue
        reason = (
            "没取到" if after_supply and status in ("to_confirm", "waiting") else None
        )
        if reason:
            out.missing.append(f"{label}：{reason}")
            out.missing_gaps.append(gap(f"{label}：{reason}", need.required))
        if status == "not_collectable" and item.get("reason") == "no_subject":
            reason = reason or "不限主体的没找到，也补采不了"
        reason = reason or _REASONS.get(status) or str(item.get("reason") or status)
        if status == "ambiguous":
            out.problems.append(f"{label}：{reason}")
        else:
            out.incomplete.append(f"{label}：{reason}")
            out.gaps.append(gap(f"{label}：{reason}", need.required))
    for binding in bindings:
        key = (binding.material_id, binding.version_no)
        if key in seen:
            continue
        seen.add(key)
        out.pins.append(_binding_pin(binding))
    if len(out.pins) > MAX_PINS:
        out.problems.append(f"一次最多用 {MAX_PINS} 份")
    return out


def _name(need: DataRequest) -> str:
    return need.name


def _label(need: DataRequest, item: dict[str, Any]) -> str:
    key = item.get("key") or {}
    parts = [need.name, str(key.get("subject") or "")]
    if key.get("period_end"):
        parts.append(f"{key.get('period_start')}–{key.get('period_end')}")
    return " ".join(p for p in parts if p)


def _chosen(
    item: dict[str, Any], pick: dict[str, Any] | None
) -> list[dict[str, Any]] | None:
    """用户换选的那一份（要在候选或已挑里）；没换就是 knowbase 挑的。"""
    chosen = list(item.get("chosen") or [])
    if pick is None:
        return chosen
    pool = chosen + list(item.get("candidates") or [])
    found = next(
        (
            c
            for c in pool
            if c.get("material_id") == pick.get("material_id")
            and int(c.get("version_no") or 0) == int(pick.get("version_no") or 0)
        ),
        None,
    )
    return [found] if found is not None else None


def _pin(need_key: str, c: dict[str, Any]) -> dict[str, Any]:
    basis = str(c.get("basis") or "display")
    basis_ref = {
        "display": APP_BASIS_REF,
        "reference": "reference",
        "public": PUBLIC_OWNER,
    }.get(basis, APP_BASIS_REF)
    return {
        "dataset_id": str(uuid.uuid4()),
        "slot": need_key,
        "source": "knowbase",
        "item_id": str(c["material_id"]),
        "version_no": int(c["version_no"]),
        "sha256": str(c["sha256"]),
        "rendition_kind": c.get("rendition_kind"),
        "rendition_no": c.get("rendition_no"),
        "rendition_sha256": c.get("rendition_sha256"),
        "basis": basis,
        "basis_ref": basis_ref,
        "name": str(c.get("name") or c["material_id"])[:255],
        "media_type": str(c.get("media_type") or "")[:128],
        "size_bytes": int(c.get("size_bytes") or 0),
        "path": None,
    }


def _binding_pin(b: FrozenBinding) -> dict[str, Any]:
    return {
        "dataset_id": str(uuid.uuid4()),
        "slot": None,
        "source": "knowbase",
        "item_id": b.material_id,
        "version_no": b.version_no,
        "sha256": b.sha256,
        "rendition_kind": b.rendition_kind,
        "rendition_no": b.rendition_no,
        "rendition_sha256": b.rendition_sha256,
        "basis": "frozen_binding",
        "basis_ref": b.basis_ref,
        # 作者绑的：请的人看不到内容（0043 第 4.4 节），只列「专家自带的资料」
        "name": "专家自带的资料",
        "media_type": "",
        "size_bytes": 0,
        "path": None,
    }
