"""作为运行方跑分身测试（SDD 0027 §3.4，契约 knowbot-test-run/v1，knowlab 定）。

这里是工作台这一侧的解析模型与纯函数：收到的请求怎么读、用例的期望怎么判、一条用例的
结果怎么从步骤轨算出来。契约的真源在 knowlab（`knowlab-app/contracts/knowbot-test-run/v1/`），
本仓只持锁（`knowbench-app/contracts/knowlab-test-run-lock.json`），测试核样例合提供方 schema。

有人值守：跑到要人决定的地方（工作台的「待人回答」交互）就报成待回答的问题，knowlab 那边
运营答跑法、用户答投资判断，答完接着跑。结果只有运行正确与否，不含分析内容（C-L5）。
"""

from __future__ import annotations

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


class RunCase(Strict):
    case_id: str = Field(pattern=CASE_ID)
    input: CaseInput
    expectations: tuple[Expectation, ...] = Field(default=(), max_length=64)


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

    @model_validator(mode="after")
    def _unique_cases(self) -> RunRequest:
        ids = [c.case_id for c in self.cases]
        if len(ids) != len(set(ids)):
            raise ValueError("case ids must be unique")
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
    answered_by: Literal["operator", "user"]


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
    """每步验收通过与否、没过的验收名与原因、期望满足与否。不放交回物的内容（C-L5）。

    验收没能做（读不到用户的数据这类平台的事）记成用例出错，不记成没过（审读第 3 条）。
    reason：委托停下的原因（最后一次状态变化里记的）。平台收尾的（推它的程序重启、崩了，
    `platform` 为真）记成出错；别的失败把原因写进说明，不只写「失败了」（K19 第 2 稿 c1）。
    """
    lines = []
    platform: list[str] = []
    for step in steps:
        accepted = step.get("status") == "accepted"
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
