"""作为运行方跑分身测试（SDD 0027 §3.4，契约 knowbot-test-run/v1，knowlab 定）。

这里是工作台这一侧的解析模型与纯函数：收到的请求怎么读、用例的期望怎么判、一条用例的
结果怎么从步骤轨算出来。契约的真源在 knowlab（`knowlab-app/contracts/knowbot-test-run/v1/`），
本仓只持锁（`knowbench-app/contracts/knowlab-test-run-lock.json`），测试核样例合提供方 schema。

都在作者（数据的所有者）的账号下、他接入的电脑上、用他的模型 key 跑（`runner`，decisions
⑯：平台测试也一样，平台的测试账号与测试电脑不再用于测试）。

有人值守：跑到要人决定的地方（工作台的「待人回答」交互）就报成待回答的问题，knowlab 那边
运营答跑法、用户答投资判断，答完接着跑。结果只有运行正确与否，不含分析内容；成稿与各步交回物
走单独的产出（SDD 0037，`case_outputs`）：运营看来验证这一稿，用户看自己的作验收依据。
"""

from __future__ import annotations

import copy
import hashlib
import json
import re
from decimal import Decimal, InvalidOperation
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator

from app.domain.workbench.states import PLATFORM_FAILURE

CONTRACT = "knowbot-test-run/v1"
RUN_ID = r"^[A-Za-z0-9._:-]{8,128}$"
CASE_ID = r"^[A-Za-z0-9._-]{1,64}$"
FINAL = frozenset({"passed", "failed", "error", "timeout", "cancelled"})


class Strict(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)


class Expectation(Strict):
    artifact: str = Field(min_length=1, max_length=128)
    path: str = Field(min_length=1, max_length=256)
    kind: Literal["equals", "approx", "contains"]
    value: str | int | float | bool
    tolerance: float | None = Field(default=None, ge=0, le=1)


class CaseInput(Strict):
    question: str = Field(min_length=1, max_length=4000)
    answers: dict[str, str] = Field(default_factory=dict, max_length=64)


class Runner(Strict):
    """谁跑这次测试（SDD 0046 第三节，decisions ⑯）：只有用户本人——跑在他接入的电脑、
    他选的工作区根目录、他的模型 key，他出钱。平台测试也在作者的账号下跑，平台的测试
    账号与测试电脑不再用于测试（10-28 起必填）。"""

    kind: Literal["user"]
    environment_id: str = Field(pattern=r"^[0-9a-f-]{36}$")
    workspace_root: str = Field(min_length=1, max_length=1024)


class PinnedInput(Strict):
    """测试集里钉住的一份（清单 v2，SDD 0043 第 2.9 节）：资料、内容版本、读法与摘要、
    权利依据。"""

    material_id: str = Field(min_length=1, max_length=64)
    version_no: int = Field(ge=1)
    sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    rendition_kind: str | None = Field(
        default=None, max_length=16, exclude_if=lambda value: value is None
    )
    rendition_no: int | None = Field(
        default=None, ge=1, exclude_if=lambda value: value is None
    )
    rendition_sha256: str | None = Field(
        default=None,
        pattern=r"^[0-9a-f]{64}$",
        exclude_if=lambda value: value is None,
    )
    basis: Literal["display", "reference", "public", "frozen_binding"]
    basis_ref: str = Field(min_length=1, max_length=160)
    name: str = Field(min_length=1, max_length=255)
    media_type: str = Field(default="", max_length=128)
    size_bytes: int = Field(default=0, ge=0)


class CaseData(Strict):
    """一条用例用的数据（清单 v2，SDD 0046 第三节）：knowbase 签的这条用例的测试运行
    授权与钉住的几份。用例带了就不跑第 0 步；回答 `data` 问题时也是这个形状（几份都
    不要时 items 为空、不带授权）。"""

    grant_id: str | None = Field(
        default=None,
        min_length=1,
        max_length=64,
        exclude_if=lambda value: value is None,
    )
    items: tuple[PinnedInput, ...] = Field(default=(), max_length=200)

    @model_validator(mode="after")
    def _grant_with_items(self) -> CaseData:
        if bool(self.items) != (self.grant_id is not None):
            raise ValueError("pinned items come with their grant, and only then")
        return self


class RunCase(Strict):
    case_id: str = Field(pattern=CASE_ID)
    input: CaseInput
    expectations: tuple[Expectation, ...] = Field(default=(), max_length=64)
    # 清单 v2：这条用例的测试集（带了就钉上、不跑第 0 步）；没有时不进摘要
    data: CaseData | None = Field(default=None, exclude_if=lambda value: value is None)


class Budget(Strict):
    limit: str = Field(pattern=r"^[0-9]{1,6}(\.[0-9]{1,2})?$")
    currency: Literal["CNY"] = "CNY"


class RunRequest(Strict):
    contract: Literal["knowbot-test-run/v1"]
    run_id: str = Field(pattern=RUN_ID)
    owner_subject: str = Field(pattern=r"^[A-Za-z0-9._:@-]{1,512}$")
    manifest: dict[str, Any]
    cases: tuple[RunCase, ...] = Field(min_length=1, max_length=50)
    budget: Budget
    timeout_seconds: int = Field(ge=60, le=86400)
    max_wait_seconds: int = Field(default=259200, ge=60, le=604800)
    # 谁跑：用户本人，在他的电脑上（SDD 0046 第三节，decisions ⑯）；没有的请求不合契约
    runner: Runner

    @model_validator(mode="after")
    def _unique_cases(self) -> RunRequest:
        ids = [c.case_id for c in self.cases]
        if len(ids) != len(set(ids)):
            raise ValueError("case ids must be unique")
        if self.manifest.get("schema") == "knowbot-manifest/v3":
            pack = self.manifest.get("pack")
            planning = pack.get("planning") if isinstance(pack, dict) else None
            finals = planning.get("final_units") if isinstance(planning, dict) else None
            if (
                not isinstance(finals, (list, tuple))
                or not finals
                or any(not isinstance(v, str) for v in finals)
            ):
                raise ValueError(
                    "dynamic test manifests require declared final methods"
                )
            if any(
                e.artifact not in finals for c in self.cases for e in c.expectations
            ):
                raise ValueError(
                    "dynamic expectations must name a declared final method"
                )
        return self

    def digest(self) -> str:
        """同一个 run_id 重复提交：内容相同回原状态，不同则 409。"""
        body = json.dumps(
            self.model_dump(mode="json"),
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
        )
        return hashlib.sha256(body.encode("utf-8")).hexdigest()


class Answer(Strict):
    question_id: str = Field(pattern=r"^[A-Za-z0-9._:-]{1,128}$")
    decision: Literal["approve", "deny"] | None = None
    option_id: str | None = Field(default=None, max_length=64)
    text: str | None = Field(default=None, min_length=1, max_length=4000)
    # 回答 data 问题：作者确认的这条用例的数据（几份都不要时 items 为空）
    data: CaseData | None = Field(default=None, exclude_if=lambda value: value is None)
    answered_by: Literal["operator", "user"]

    @model_validator(mode="after")
    def _data_by_the_user(self) -> Answer:
        if self.data is not None and self.answered_by != "user":
            raise ValueError("only the user confirms the data")
        return self


class Answers(Strict):
    contract: Literal["knowbot-test-run/v1"]
    answers: tuple[Answer, ...] = Field(min_length=1, max_length=50)


def trial_profile_id(run_id: str) -> str:
    """测试用的专家 id：不进用户的专家清单、不进钉表，只在这次测试的委托上。"""
    return "knowlab-test:" + hashlib.sha256(run_id.encode("utf-8")).hexdigest()[:32]


# ---------- 期望 ----------
_SEGMENT = re.compile(r"^[A-Za-z0-9_\-一-鿿]+$")


def _walk(content: Any, path: str) -> tuple[bool, Any]:
    """点号路径；数字段按列表下标取。走不到就是没有。"""
    current = content
    for part in path.split("."):
        if isinstance(current, dict) and part in current:
            current = current[part]
        elif isinstance(current, list) and part.isdigit() and int(part) < len(current):
            current = current[int(part)]
        else:
            return False, None
    return True, current


def _number(value: Any) -> Decimal | None:
    """带百分号的不当数（与 knowlab 读表一致，审读第 8 条）：要比就写成字符串期望。"""
    if isinstance(value, bool):
        return None
    text = str(value).strip()
    if text.endswith("%"):
        return None
    try:
        number = Decimal(text)
    except (InvalidOperation, ValueError):
        return None
    return number if number.is_finite() else None


def expectation_met(expectation: Expectation, artifacts: dict[str, Any]) -> bool:
    """在最后的交回物里，这一项是不是用户说的那样。"""
    if expectation.artifact not in artifacts:
        return False
    found, value = _walk(artifacts[expectation.artifact], expectation.path)
    if not found:
        return False
    want = expectation.value
    if expectation.kind == "equals":
        if isinstance(want, bool) or isinstance(value, bool):
            # 交回物里写成 "true" 也算（审读第 12 条）
            return str(value).strip().lower() == str(want).lower()
        if isinstance(want, int | float):
            got = _number(value)
            return got is not None and got == Decimal(str(want))
        return str(value).strip() == str(want).strip()
    if expectation.kind == "approx":
        got, target = _number(value), _number(want)
        if got is None or target is None or expectation.tolerance is None:
            return False
        if target == 0:
            return abs(got) <= Decimal(str(expectation.tolerance))
        return abs(got - target) <= abs(target) * Decimal(str(expectation.tolerance))
    # contains
    if isinstance(value, list):
        return any(str(item).strip() == str(want).strip() for item in value)
    return str(want) in str(value)


# ---------- 一条用例的结果 ----------
DETAIL_CHARS = 200


def _why(reason: dict[str, Any] | None) -> str:
    """委托停下的原因，一句话：错误、代码、预算、谁停的，有哪个写哪个。"""
    reason = reason or {}
    for key in ("error", "code", "budget", "by"):
        if reason.get(key):
            return str(reason[key])[:DETAIL_CHARS]
    return "no reason recorded"


def _detail(check: dict[str, Any]) -> str:
    """没过的验收为什么没过：哪一格、说了多少、实际多少（审读第 3 条）。

    数是用户自己数据里的，不是分析内容（C-L5）。"""
    text = (
        f"{check.get('label') or check.get('rule') or ''}: {check.get('detail') or ''}"
    )
    return text.strip(": ")[:DETAIL_CHARS]


def case_result(
    *,
    case: RunCase,
    task: dict[str, Any] | None,
    steps: list[dict[str, Any]],
    artifacts: dict[str, Any],
    needs_human: bool = False,
    error: str | None = None,
    reason: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """每步验收通过与否、没过的验收名与原因、期望满足与否。不放交回物的内容：内容走
    `case_outputs`（SDD 0037），结果每次刷新整份拉，要小。

    验收没能做（读不到用户的数据这类平台的事）记成用例出错，不记成没过（审读第 3 条）。
    reason：委托停下的原因（最后一次状态变化里记的）。平台收尾的（推它的程序重启、崩了，
    `platform` 为真）记成出错；别的失败把原因写进说明，不只写「失败了」（K19 第 2 稿 c1）。
    """
    lines = []
    platform: list[str] = []
    for step in steps:
        accepted = step.get("status") == "accepted"
        if "current_result" in step and step["current_result"] is None:
            accepted = False
        failed: list[dict[str, Any]] = []
        for attempt in reversed(step.get("attempts") or []):
            if attempt.get("outcome") in ("accepted", "rejected"):
                failed = [
                    c for c in attempt.get("checks") or [] if c.get("pass") is False
                ]
                break
        platform += [_detail(c) for c in failed if c.get("platform")]
        if any(
            a.get("failure") == PLATFORM_FAILURE for a in step.get("attempts") or []
        ):
            platform.append(
                f"{step.get('step_id')}: the platform failed during this step"
            )
        if step.get("attempts"):
            lines.append(
                {
                    "step_id": str(step.get("step_id")),
                    "accepted": accepted,
                    "failed_checks": [str(c.get("label") or "") for c in failed][:64],
                    "details": [_detail(c) for c in failed][:64],
                }
            )
    met = [expectation_met(e, artifacts) for e in case.expectations]
    state = (task or {}).get("state")
    if error is not None:
        status, note = "error", error
    elif state == "FAILED" and (reason or {}).get("platform"):
        status = "error"
        note = f"the platform stopped this run: {_why(reason)}"
        if platform:  # 哪一步出的事也写上
            note += f"; {platform[0]}"
    elif platform and not (state == "SUCCEEDED" and all(met)):
        status = "error"
        note = f"the platform could not check a step: {platform[0]}"
    elif needs_human:
        status, note = "needs_human", "a question waited too long or the run stopped"
    elif state == "SUCCEEDED" and all(met):
        status, note = "passed", None
    elif state == "SUCCEEDED":
        status, note = "failed", "the final result differs from the expectations"
    else:
        status = "failed"
        note = f"the run ended as {str(state).lower()}" if state else "not run"
        if state and reason:
            note += f": {_why(reason)}"
    return {
        "case_id": case.case_id,
        "status": status,
        "steps": lines,
        "expectations": met,
        "note": note,
    }


# ---------- 一条用例的产出（SDD 0037，契约 test-run-case-outputs）----------
DELIVERABLE_CHARS = 200_000  # 成稿最多这么多字，超了截断
ARTIFACT_BYTES = 200_000  # 一步交回物序列化后最多这么多字节，超了不带、注明多大
RAW_CHARS = 20_000  # 没过的那次交回原文（同工作台排查用的上限）
OUTPUTS_BYTES = 2 * 1024 * 1024  # 一条用例的产出合计
OUTPUT_OUTCOMES = frozenset(
    {"accepted", "rejected", "failed", "over_budget", "cancelled", "running"}
)


def _json_bytes(value: Any) -> int:
    return len(
        json.dumps(value, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
    )


def step_output(step: dict[str, Any]) -> dict[str, Any]:
    """工作台单步视图（带交回物与过程的那种）里一步的最后一次尝试 → 产出里的一步。

    通过的带交回物；没过的带最后一次交回的内容与原文、逐条验收；没开始的 not_reached。"""
    head: dict[str, Any] = {
        "step_id": str(step.get("step_id") or "")[:128],
        "title": str(step.get("title") or "")[:200],
    }
    if "current_result" in step:
        head["current"] = step["current_result"] is not None
    attempts = step.get("attempts") or []
    if not attempts:
        return {**head, "outcome": "not_reached", "checks": []}
    last = attempts[-1]
    outcome = last.get("outcome")
    out: dict[str, Any] = {
        **head,
        "outcome": outcome if outcome in OUTPUT_OUTCOMES else "running",
        "checks": [
            {
                "label": str(c.get("label") or "")[:200],
                "passed": c.get("pass"),
                "message": str(c.get("message") or "")[:1000],
            }
            for c in last.get("checks") or []
        ][:64],
    }
    returned = last.get("returned") or {}
    content = returned.get("content")
    if content is not None:
        size = _json_bytes(content)
        if size > ARTIFACT_BYTES:
            out["artifact_omitted_bytes"] = size
        else:
            out["artifact"] = content
    if outcome == "rejected" and returned.get("raw"):
        out["returned_raw"] = str(returned["raw"])[:RAW_CHARS]
    return out


def case_outputs(
    *,
    run_id: str,
    case_id: str,
    steps: list[dict[str, Any]],
    deliverable: dict[str, Any] | None,
    deliverable_note: str | None,
    error: str | None = None,
    plan_version: int | None = None,
    final_outputs: list[dict[str, Any]] | None = None,
) -> dict[str, Any]:
    """一条用例的产出，合计不超过 OUTPUTS_BYTES（留出余量给对方补默认字段）。

    超了就一级一级让：先拿掉最大的几步交回物，再截、再拿掉没过的原文，再只留没过的验收，
    再截、再拿掉成稿，最后只留各步结局。每一级只走一遍，不会转圈（审读 10-19 第 1 条）。"""
    out: dict[str, Any] = {
        "contract": CONTRACT,
        "run_id": run_id,
        "case_id": case_id,
        "deliverable": deliverable,
        "deliverable_note": deliverable_note[:500] if deliverable_note else None,
        "steps": [step_output(s) for s in steps][:200],
        "error": error[:500] if error else None,
    }
    if final_outputs:
        if plan_version is None:
            raise ValueError("final outputs require an accepted plan version")
        out.update(
            plan_version=plan_version, final_outputs=copy.deepcopy(final_outputs)
        )
        for final in out["final_outputs"]:
            if "artifact" in final and _json_bytes(final["artifact"]) > ARTIFACT_BYTES:
                final["artifact_omitted_bytes"] = _json_bytes(final.pop("artifact"))
            file = final.get("deliverable")
            if file and len(file["text"]) > DELIVERABLE_CHARS:
                final["deliverable"] = {
                    **file,
                    "text": file["text"][:DELIVERABLE_CHARS],
                    "truncated": True,
                }
    if deliverable is not None and len(deliverable["text"]) > DELIVERABLE_CHARS:
        out["deliverable"] = {
            **deliverable,
            "text": deliverable["text"][:DELIVERABLE_CHARS],
            "truncated": True,
        }
    for give_way in _GIVE_WAY:
        if _json_bytes(out) <= _BUDGET:
            break
        give_way(out)
    return out


_BUDGET = OUTPUTS_BYTES - 64 * 1024  # 对方的模型会补上没写的默认字段


def _drop_artifacts(out: dict[str, Any]) -> None:
    for step in sorted(
        (s for s in out["steps"] if "artifact" in s),
        key=lambda s: _json_bytes(s["artifact"]),
        reverse=True,
    ):
        if _json_bytes(out) <= _BUDGET:
            return
        step["artifact_omitted_bytes"] = _json_bytes(step.pop("artifact"))


def _drop_final_contents(out: dict[str, Any]) -> None:
    """Keep every final identity even when its content cannot fit the response."""
    for final in sorted(out.get("final_outputs") or [], key=_json_bytes, reverse=True):
        if _json_bytes(out) <= _BUDGET:
            return
        if "artifact" in final:
            final["artifact_omitted_bytes"] = _json_bytes(final.pop("artifact"))
        if final.get("deliverable"):
            final["deliverable"] = None
            final["note"] = "这份成稿太大，本次只保留已验成果引用"


def _cut_raw(out: dict[str, Any]) -> None:
    for step in out["steps"]:
        if step.get("returned_raw"):
            step["returned_raw"] = step["returned_raw"][:2_000]


def _drop_raw(out: dict[str, Any]) -> None:
    for step in out["steps"]:
        step.pop("returned_raw", None)


def _failed_checks_only(out: dict[str, Any]) -> None:
    for step in out["steps"]:
        step["checks"] = [
            {**c, "message": c["message"][:200]}
            for c in step.get("checks") or []
            if c.get("passed") is False
        ]


def _cut_deliverable(out: dict[str, Any]) -> None:
    if out["deliverable"] and len(out["deliverable"]["text"]) > 20_000:
        out["deliverable"] = {
            **out["deliverable"],
            "text": out["deliverable"]["text"][:20_000],
            "truncated": True,
        }


def _drop_deliverable(out: dict[str, Any]) -> None:
    if out["deliverable"]:
        out["deliverable"] = None
        out["deliverable_note"] = "成稿太大，这次没带回"


def _outcomes_only(out: dict[str, Any]) -> None:
    out["steps"] = [
        {
            "step_id": s["step_id"],
            "title": s["title"],
            "outcome": s["outcome"],
            "checks": [],
            **({"current": s["current"]} if "current" in s else {}),
        }
        for s in out["steps"]
    ]
    out["error"] = "产出太大，只带回各步的结局"


_GIVE_WAY = (
    _drop_artifacts,
    _drop_final_contents,
    _cut_raw,
    _drop_raw,
    _failed_checks_only,
    _cut_deliverable,
    _drop_deliverable,
    _outcomes_only,
)
