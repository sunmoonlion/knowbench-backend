"""工作台的对象与契约（task-contract.md、objects.md）。pydantic 模型只做形状与校验，不做持久化。"""

from __future__ import annotations

import hashlib
import json
from decimal import Decimal
from typing import Any

from pydantic import BaseModel, ConfigDict, Field

from app.domain.workbench.states import AttemptRole, TaskState, Wheel


class Strict(BaseModel):
    model_config = ConfigDict(extra="forbid")


class Budget(Strict):
    """预算账的快照：币种、上限、已预留、已用。金额用字符串化的 Decimal 存 JSON。"""

    currency: str = "CNY"
    # 上限可以不给：不给就没有上限，花多少记多少，停不停由用户看着金额自己定
    # （所有者 2026-10-04：页面上实时显示金额加一个随时能点的停止，不靠预算限额）
    limit: Decimal | None = Field(default=None, gt=0)
    reserved: Decimal = Decimal("0")
    used: Decimal = Decimal("0")

    @property
    def available(self) -> Decimal:
        if self.limit is None:
            return Decimal("Infinity")
        return self.limit - self.reserved - self.used

    def as_json(self) -> dict[str, str | None]:
        return {
            "currency": self.currency,
            "limit": None if self.limit is None else str(self.limit),
            "reserved": str(self.reserved),
            "used": str(self.used),
        }


class HandoverRequest(Strict):
    """提交信封（求助 = 交出方向盘）。"""

    idempotency_key: str = Field(min_length=8, max_length=128)
    session_id: str
    profile_id: str = Field(min_length=1, max_length=128)
    profile_version: str | None = None
    original_input: dict[str, Any]
    attachments: list[str] = Field(default_factory=list)
    budget_limit: Decimal | None = Field(default=None, gt=0)
    budget_currency: str = "CNY"
    requested_deadline: str | None = None
    client_context: dict[str, Any] = Field(default_factory=dict)

    def digest(self) -> str:
        body = {
            "session_id": self.session_id,
            "profile_id": self.profile_id,
            "profile_version": self.profile_version,
            "original_input": self.original_input,
            "attachments": self.attachments,
            "budget_limit": str(self.budget_limit),
            "budget_currency": self.budget_currency,
        }
        return hashlib.sha256(
            json.dumps(body, sort_keys=True, ensure_ascii=False).encode()
        ).hexdigest()


class Session(Strict):
    id: str
    owner_actor_id: str
    environment_id: str
    sandbox_id: str
    project_root: str
    thread_id: str | None
    wheel: Wheel
    state_version: int
    active_task_id: str | None


class Task(Strict):
    id: str
    session_id: str
    owner_actor_id: str
    tenant: str
    idempotency_key: str
    request_digest: str
    profile_id: str
    profile_version: str
    expert_pack_version: str | None
    state: TaskState
    state_version: int
    current_step: int
    waiting_reason: str | None
    active_attempt_id: str | None
    active_interaction_id: str | None
    budget: Budget
    thread_id: str | None
    environment_id: str
    project_root: str


class Attempt(Strict):
    id: str
    task_id: str
    session_id: str
    role: AttemptRole
    arm: int
    step_id: str | None
    status: str
    turn_ids: list[str]


class InteractionPrompt(Strict):
    """审查面必须表达的要素（approval.md「Task 级审查必须表达」）。"""

    title: str
    question: str
    options: list[dict[str, Any]] = Field(default_factory=list)
    subject: dict[str, Any] = Field(default_factory=dict)
    evidence: list[dict[str, Any]] = Field(default_factory=list)
    unknowns: list[str] = Field(default_factory=list)


class InteractionResponse(Strict):
    token: str = Field(min_length=16)
    decision: str | None = None
    answer: dict[str, Any] | None = None
    subject_digest: str | None = None
