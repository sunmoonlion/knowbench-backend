"""从专家入口请专家（PRD/apps/investment.md 7.3、F-PROJ-05）。

新建一段工作对话并立刻交给专家：建对话、建委托、交出方向盘、排上驾驶的命令，在同一次提交里。
哪一步不成，整个都不留下：不会剩一段空的对话（AT-INV-08）。

分身（清单 v2，SDD 0043）：建委托之前建空的解析清单、本人签作者挑选的数据
（expert_data.ExpertData），钉住的数据随委托在同一次提交里写下；建委托没成就交还授权。
要什么数据由委托第 0 步按问题提出、用户确认（decisions ⑭）。同一把幂等钥匙重发时不再签。
"""

from __future__ import annotations

from collections.abc import Awaitable, Callable
from decimal import Decimal
from typing import Any

from app.application.ports.workbench import WorkbenchStore
from app.application.workbench.catalog import (
    builtin_on_offer,
    builtin_on_view,
    offerable_expert,
    pinned_pack,
    require_dynamic_owner,
)
from app.application.workbench.expert_data import ExpertData, Prepared, refusal
from app.application.workbench.ledger import Ledger
from app.application.workbench.session_service import SessionService
from app.domain.workbench.errors import (
    DataChoiceInvalid,
    IdempotencyConflict,
    WorkbenchError,
)
from app.domain.workbench.knowbots import (
    MANIFEST_SCHEMA_V2,
    MANIFEST_SCHEMA_V3,
    is_knowbot,
)
from app.domain.workbench.models import HandoverRequest
from app.domain.workbench.projects import default_title

# 只看内置专家的老入口（同步、不认分身），保留给已有的调用方
expert_on_view = builtin_on_view
expert_on_offer = builtin_on_offer

UserToken = Callable[[], Awaitable[str]]


class DelegationService:
    def __init__(
        self,
        repo: WorkbenchStore,
        *,
        dynamic_method_owners: frozenset[str] = frozenset(),
    ):
        self.repo = repo
        self.dynamic_method_owners = dynamic_method_owners

    async def from_expert_entry(
        self,
        *,
        owner_actor_id: str,
        project_id: str,
        profile_id: str,
        question: str,
        budget_limit: Decimal | None = None,
        budget_currency: str = "CNY",
        idempotency_key: str,
        tenant: str = "default",
        user_token: UserToken | None = None,
        expert_data: ExpertData | None = None,
    ) -> dict[str, Any]:
        # 内置专家，或这个人请来并钉住的分身（SDD 0027 P4）
        pack = await offerable_expert(
            self.repo, owner_actor_id=owner_actor_id, profile_id=profile_id
        )

        def request(session_id: str) -> HandoverRequest:
            return HandoverRequest(
                idempotency_key=idempotency_key,
                session_id=session_id,
                profile_id=pack.profile_id,
                original_input={"text": question},
                budget_limit=budget_limit,
                budget_currency=budget_currency,
                client_context={"entry": "expert"},
            )

        prepared = await self._prepare(
            owner_actor_id=owner_actor_id,
            profile_id=pack.profile_id,
            idempotency_key=idempotency_key,
            tenant=tenant,
            expert_data=expert_data,
            user_token=user_token,
        )
        try:
            made = await self._from_expert_entry(
                owner_actor_id=owner_actor_id,
                project_id=project_id,
                profile_id=pack.profile_id,
                question=question,
                idempotency_key=idempotency_key,
                tenant=tenant,
                request=request,
                pins=prepared.pins if prepared else [],
                resolution=prepared.resolution_row if prepared else None,
            )
        except BaseException:
            await self._release(expert_data, prepared)
            raise
        if not made["created"]:
            # 并发重发：另一份已经建好，这一份的授权不要
            await self._release(expert_data, prepared)
        return made

    async def handover(
        self,
        req: HandoverRequest,
        *,
        owner_actor_id: str,
        tenant: str = "default",
        user_token: UserToken | None = None,
        expert_data: ExpertData | None = None,
    ) -> dict[str, Any]:
        """对话里把方向盘交给专家（工作台接口 /sessions/{id}/handover）：同上先签再建委托。"""
        prepared = await self._prepare(
            owner_actor_id=owner_actor_id,
            profile_id=req.profile_id,
            idempotency_key=req.idempotency_key,
            tenant=tenant,
            expert_data=expert_data,
            user_token=user_token,
        )
        try:
            made = await Ledger(
                self.repo, dynamic_method_owners=self.dynamic_method_owners
            ).handover(
                req,
                owner_actor_id=owner_actor_id,
                tenant=tenant,
                pins=(prepared.pins if prepared else None) or None,
                resolution=prepared.resolution_row if prepared else None,
            )
        except BaseException:
            await self._release(expert_data, prepared)
            raise
        if not made["created"]:
            await self._release(expert_data, prepared)
        return made

    async def data_needs(
        self, *, owner_actor_id: str, profile_id: str
    ) -> dict[str, Any]:
        """请专家页要的：分身回清单的版本与作者挑选的数据的份数。没有数据需求声明，要什么
        数据由委托第 0 步按问题提出、用户确认（decisions ⑭），请专家页上不用选；内置专家
        不用数据，`contract` 是空的。"""
        if not is_knowbot(profile_id):
            return {"contract": None, "bound": 0}
        pinned = await pinned_pack(
            self.repo, owner_actor_id=owner_actor_id, profile_id=profile_id
        )
        return {
            "contract": MANIFEST_SCHEMA_V3 if pinned.catalog else MANIFEST_SCHEMA_V2,
            "bound": len(pinned.bindings),
        }

    async def _prepare(
        self,
        *,
        owner_actor_id: str,
        profile_id: str,
        idempotency_key: str,
        tenant: str,
        expert_data: ExpertData | None,
        user_token: UserToken | None,
    ) -> Prepared | None:
        """分身：建空清单、签作者挑选的数据（本人在场）；内置专家不用数据，回 None。
        同一把钥匙已经建过委托就不再做（交给后面回原来那份）。"""
        if not is_knowbot(profile_id):
            return None
        pinned = await pinned_pack(
            self.repo, owner_actor_id=owner_actor_id, profile_id=profile_id
        )
        prior = await self.repo.find_task_by_idempotency(
            tenant=tenant,
            owner_actor_id=owner_actor_id,
            profile_id=profile_id,
            idempotency_key=idempotency_key,
        )
        if prior is not None:
            return Prepared()
        subject = await self.repo.subject_of_actor(owner_actor_id)
        require_dynamic_owner(
            pinned, subject=subject, allowed=self.dynamic_method_owners
        )
        if expert_data is None or user_token is None:
            raise DataChoiceInvalid("data cannot be chosen here")
        if subject is None:
            raise DataChoiceInvalid("this account is not known to the data service")
        try:
            # 运行授权的 ref 由 prepare 在服务端生成（每件委托唯一），不用幂等钥匙拼
            return await expert_data.prepare(pinned, user_token=user_token)
        except WorkbenchError:
            raise
        except Exception as exc:
            translated = refusal(exc)
            if translated is None:
                raise
            raise translated from None

    @staticmethod
    async def _release(
        expert_data: ExpertData | None, prepared: Prepared | None
    ) -> None:
        if prepared is not None and expert_data is not None:
            await expert_data.release(prepared)

    async def _from_expert_entry(
        self,
        *,
        owner_actor_id: str,
        project_id: str,
        profile_id: str,
        question: str,
        idempotency_key: str,
        tenant: str,
        request: Callable[[str], HandoverRequest],
        pins: list[dict[str, Any]],
        resolution: dict[str, Any] | None = None,
    ) -> dict[str, Any]:

        async with self.repo.transaction():
            prior = await self.repo.find_task_by_idempotency(
                tenant=tenant,
                owner_actor_id=owner_actor_id,
                profile_id=profile_id,
                idempotency_key=idempotency_key,
            )
            if prior is not None:
                # 同一把钥匙重发：是同一件事就还给原来的那一份，不再建一段对话
                same = request(str(prior["session_id"])).digest()
                if prior["request_digest"] != same or (
                    str(prior.get("project_id")) != project_id
                ):
                    raise IdempotencyConflict(
                        "same idempotency key with a different request",
                        task_id=str(prior["id"]),
                    )
                context = (prior.get("original_input") or {}).get(
                    "client_context"
                ) or {}
                return {
                    "task_id": str(prior["id"]),
                    "session_id": str(prior["session_id"]),
                    "project_id": project_id,
                    "state": prior["state"],
                    "created": False,
                    "queued_behind": context.get("queued_behind"),
                }
            started = await SessionService(self.repo).start(
                owner_actor_id=owner_actor_id,
                kind="expert",
                project_id=project_id,
                title=default_title(question),
            )
            session_id = started["session_id"]
            made = await Ledger(
                self.repo, dynamic_method_owners=self.dynamic_method_owners
            ).handover(
                request(session_id),
                owner_actor_id=owner_actor_id,
                tenant=tenant,
                pins=pins or None,
                resolution=resolution,
            )
            session = await self.repo.get_session(session_id)
            if not made.get("queued_behind"):
                # 这段对话还没有线：驾驶的命令会先把线起好（runner），所以只排这一条
                await self.repo.enqueue_command(
                    session_id=session_id,
                    sandbox_id=str(session["sandbox_id"]),
                    kind="task.drive",
                    payload={"task_id": made["task_id"]},
                )
            return {
                "task_id": made["task_id"],
                "session_id": session_id,
                "project_id": project_id,
                "state": made["state"],
                "created": True,
                "queued_behind": made.get("queued_behind"),
            }
