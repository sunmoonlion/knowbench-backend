"""从专家入口请专家（PRD/apps/investment.md 7.3、F-PROJ-05）。

新建一段工作对话并立刻交给专家：建对话、建委托、交出方向盘、排上驾驶的命令，在同一次提交里。
哪一步不成，整个都不留下：不会剩一段空的对话（AT-INV-08）。

写了数据需求声明的专家（SDD 0042）：建委托之前先按声明核用户选的数据、签运行授权
（data_choice.DataChooser），钉住的数据随委托在同一次提交里写下；建委托没成就交还授权。
同一把幂等钥匙重发时不再选、不再签。
"""

from __future__ import annotations

from collections.abc import Callable
from decimal import Decimal
from typing import Any

from app.application.ports.workbench import WorkbenchStore
from app.application.workbench.catalog import (
    builtin_on_offer,
    builtin_on_view,
    offerable_expert,
    pinned_pack,
)
from app.application.workbench.data_choice import (
    DataChooser,
    Prepared,
    UserToken,
    choice_of,
    last_choices,
)
from app.application.workbench.ledger import Ledger
from app.application.workbench.session_service import SessionService
from app.domain.workbench.data_needs import DataNeed
from app.domain.workbench.errors import DataChoiceInvalid, IdempotencyConflict
from app.domain.workbench.knowbots import is_knowbot, parse_profile
from app.domain.workbench.models import HandoverRequest
from app.domain.workbench.projects import default_title

# 只看内置专家的老入口（同步、不认分身），保留给已有的调用方
expert_on_view = builtin_on_view
expert_on_offer = builtin_on_offer


class DelegationService:
    def __init__(self, repo: WorkbenchStore):
        self.repo = repo

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
        data: list[dict[str, Any]] | None = None,
        chooser: DataChooser | None = None,
        user_token: UserToken | None = None,
    ) -> dict[str, Any]:
        # 内置专家，或这个人请来并钉住的分身（SDD 0027 P4）
        pack = await offerable_expert(
            self.repo, owner_actor_id=owner_actor_id, profile_id=profile_id
        )
        choices = [choice_of(c) for c in data or []]

        def request(session_id: str) -> HandoverRequest:
            return HandoverRequest(
                idempotency_key=idempotency_key,
                session_id=session_id,
                profile_id=pack.profile_id,
                original_input={"text": question},
                budget_limit=budget_limit,
                budget_currency=budget_currency,
                client_context={"entry": "expert"},
                data=choices,
            )

        prepared = await self._prepare(
            owner_actor_id=owner_actor_id,
            profile_id=pack.profile_id,
            idempotency_key=idempotency_key,
            tenant=tenant,
            choices=choices,
            chooser=chooser,
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
                pins=prepared.pins,
            )
        except BaseException:
            if chooser is not None:
                await chooser.release(prepared)
            raise
        if not made["created"] and chooser is not None:
            await chooser.release(
                prepared
            )  # 并发重发：另一份已经建好，这一份的授权不要
        return made

    async def handover(
        self,
        req: HandoverRequest,
        *,
        owner_actor_id: str,
        tenant: str = "default",
        chooser: DataChooser | None = None,
        user_token: UserToken | None = None,
    ) -> dict[str, Any]:
        """对话里把方向盘交给专家（工作台接口 /sessions/{id}/handover）：同上先选数据再建委托。"""
        choices = [choice_of(c) for c in req.data]
        req = req.model_copy(update={"data": choices})
        prepared = await self._prepare(
            owner_actor_id=owner_actor_id,
            profile_id=req.profile_id,
            idempotency_key=req.idempotency_key,
            tenant=tenant,
            choices=choices,
            chooser=chooser,
            user_token=user_token,
        )
        try:
            made = await Ledger(self.repo).handover(
                req,
                owner_actor_id=owner_actor_id,
                tenant=tenant,
                pins=prepared.pins or None,
            )
        except BaseException:
            if chooser is not None:
                await chooser.release(prepared)
            raise
        if not made["created"] and chooser is not None:
            await chooser.release(prepared)
        return made

    async def data_needs(
        self, *, owner_actor_id: str, profile_id: str, project_id: str | None
    ) -> dict[str, Any]:
        """选数据的弹框要的：声明，与同一项目上次请这位专家时选的那几份（默认选上）。"""
        needs = await self.needs_of(
            owner_actor_id=owner_actor_id, profile_id=profile_id
        )
        ref = parse_profile(profile_id)
        last = (
            await self.repo.last_task_data(
                owner_actor_id=owner_actor_id, project_id=project_id, knowbot_id=ref[0]
            )
            if needs and project_id and ref
            else []
        )
        keys = {n.key for n in needs}
        return {
            "needs": [n.model_dump(mode="json") for n in needs],
            "last": [c for c in last_choices(last) if c["need"] in keys],
        }

    async def needs_of(self, *, owner_actor_id: str, profile_id: str) -> list[DataNeed]:
        """这位专家的数据需求声明；内置专家、旧格式的分身是空的。"""
        if not is_knowbot(profile_id):
            return []
        pinned = await pinned_pack(
            self.repo, owner_actor_id=owner_actor_id, profile_id=profile_id
        )
        return list(pinned.needs)

    async def _prepare(
        self,
        *,
        owner_actor_id: str,
        profile_id: str,
        idempotency_key: str,
        tenant: str,
        choices: list[dict[str, Any]],
        chooser: DataChooser | None,
        user_token: UserToken | None,
    ) -> Prepared:
        """按声明核选的数据、签授权；同一把钥匙已经建过委托就不再做（交给后面回原来那份）。"""
        needs = await self.needs_of(
            owner_actor_id=owner_actor_id, profile_id=profile_id
        )
        if not needs and not choices:
            return Prepared()
        prior = await self.repo.find_task_by_idempotency(
            tenant=tenant,
            owner_actor_id=owner_actor_id,
            profile_id=profile_id,
            idempotency_key=idempotency_key,
        )
        if prior is not None:
            return Prepared()
        if chooser is None or user_token is None:
            raise DataChoiceInvalid("data cannot be chosen here")
        identity = await self.repo.get_relay_identity(owner_actor_id)
        return await chooser.prepare(
            needs=needs,
            choices=choices,
            relay_user=str(identity["relay_user"]) if identity else None,
            user_token=user_token,
            ref=f"knowbench:{idempotency_key}"[:255],
        )

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
            made = await Ledger(self.repo).handover(
                request(session_id),
                owner_actor_id=owner_actor_id,
                tenant=tenant,
                pins=pins or None,
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
