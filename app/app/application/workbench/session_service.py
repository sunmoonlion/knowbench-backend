"""会话与方向盘：建 Session、谁能发 turn、记事件。与 app-server 的往来由 runner 里的客户端做，这里只管账。"""

from __future__ import annotations

from typing import Any

from app.application.ports.workbench import WorkbenchStore
from app.application.workbench.project_service import ProjectService
from app.domain.workbench.errors import (
    ConversationChangeRefused,
    NoSandbox,
    ProjectBusy,
    ProjectHeldByExpert,
    ProjectRequired,
    WheelHeldByOther,
)
from app.domain.workbench.pricing import PriceList, conversation_usage
from app.domain.workbench.projects import (
    HANDS_ON,
    ConversationKind,
    clean_title,
    default_title,
    project_dir,
)
from app.domain.workbench.states import Wheel


class SessionService:
    def __init__(self, repo: WorkbenchStore):
        self.repo = repo

    async def create(
        self,
        *,
        owner_actor_id: str,
        environment_id: str,
        sandbox_id: str,
        project_root: str,
        thread_settings: dict | None = None,
    ) -> dict[str, Any]:
        """旧入口：按完整目录建一段「工作」。目录对应的项目没有就建一个。"""
        async with self.repo.transaction():
            await self.repo.get_sandbox(sandbox_id, owner_actor_id=owner_actor_id)
            project = await ProjectService(self.repo).ensure_for_directory(
                owner_actor_id=owner_actor_id,
                environment_id=environment_id,
                directory=project_root,
            )
            sid = await self.repo.create_session(
                owner_actor_id=owner_actor_id,
                environment_id=environment_id,
                sandbox_id=sandbox_id,
                project_root=project_root,
                thread_settings=thread_settings
                or {"approvalPolicy": "on-request", "sandbox": "workspace-write"},
                kind=ConversationKind.WORK.value,
                project_id=str(project["id"]),
            )
            await self.repo.append_event(
                session_id=sid,
                kind="session",
                event_type="session/created",
                payload={
                    "environment_id": environment_id,
                    "sandbox_id": sandbox_id,
                    "project_root": project_root,
                    "kind": ConversationKind.WORK.value,
                    "project_id": str(project["id"]),
                },
            )
            return {"session_id": sid, "project_id": str(project["id"])}

    async def start(
        self,
        *,
        owner_actor_id: str,
        kind: str,
        project_id: str | None = None,
        title: str | None = None,
        origin: str | None = None,
    ) -> dict[str, Any]:
        """开一段对话。聊天可以不属于项目；工作必须属于（F-PROJ-02）。沙箱由后端取。
        origin：怎么来的（direct_answer：找专家没找到、用户让自己的模型直接答）。"""
        chosen = ConversationKind(kind)
        async with self.repo.transaction():
            sandbox = await self.repo.current_sandbox(owner_actor_id)
            if sandbox is None:
                raise NoSandbox("no sandbox yet; register a model key and start one")
            project = None
            if project_id is not None:
                project = await ProjectService(self.repo).usable(
                    project_id, owner_actor_id=owner_actor_id
                )
            elif chosen in HANDS_ON:
                raise ProjectRequired("work and expert need a project")
            sid = await self.repo.create_session(
                owner_actor_id=owner_actor_id,
                environment_id=str(project["environment_id"]) if project else None,
                sandbox_id=str(sandbox["id"]),
                project_root=(
                    project_dir(project["workspace_root"], project["path"])
                    if project
                    else None
                ),
                thread_settings={},
                kind=chosen.value,
                project_id=str(project["id"]) if project else None,
                title=clean_title(title),
                origin=origin,
            )
            await self.repo.append_event(
                session_id=sid,
                kind="session",
                event_type="session/created",
                payload={
                    "kind": chosen.value,
                    "project_id": str(project["id"]) if project else None,
                    "sandbox_id": str(sandbox["id"]),
                },
            )
            return {
                "session_id": sid,
                "kind": chosen.value,
                "project_id": str(project["id"]) if project else None,
            }

    async def attach_project(
        self, session_id: str, *, owner_actor_id: str, project_id: str
    ) -> dict[str, Any]:
        """把不属于项目的聊天放进一个项目。放进去之后不能换、不能拿出来。"""
        async with self.repo.transaction():
            session = await self.repo.get_session(
                session_id, owner_actor_id=owner_actor_id, for_update=True
            )
            if session["project_id"] is not None:
                if str(session["project_id"]) == project_id:
                    return session
                raise ConversationChangeRefused(
                    "this conversation already belongs to a project"
                )
            project = await ProjectService(self.repo).usable(
                project_id, owner_actor_id=owner_actor_id
            )
            directory = project_dir(project["workspace_root"], project["path"])
            await self.repo.attach_session_project(
                session_id,
                project_id=str(project["id"]),
                environment_id=str(project["environment_id"]),
                project_root=directory,
            )
            await self.repo.append_event(
                session_id=session_id,
                kind="session",
                event_type="session/project_attached",
                payload={"project_id": str(project["id"]), "project_root": directory},
            )
            return await self.repo.get_session(session_id)

    async def to_work(self, session_id: str, *, owner_actor_id: str) -> dict[str, Any]:
        """聊天转为工作：同一条线，之前聊的还在。工作不转回聊天。"""
        async with self.repo.transaction():
            session = await self.repo.get_session(
                session_id, owner_actor_id=owner_actor_id, for_update=True
            )
            if session["kind"] in HANDS_ON:
                return session
            if session["project_id"] is None:
                raise ProjectRequired("put this chat into a project first")
            if session["wheel"] != Wheel.user:
                raise WheelHeldByOther(
                    "the expert is working on this conversation",
                    session_id=session_id,
                )
            await self._refuse_while_expert_works(session, becoming_work=True)
            await self.repo.set_session_kind(session_id, ConversationKind.WORK.value)
            await self.repo.append_event(
                session_id=session_id,
                kind="session",
                event_type="session/kind_changed",
                payload={"from": ConversationKind.CHAT.value, "to": "work"},
            )
            return await self.repo.get_session(session_id)

    async def mark_expert(self, session: dict[str, Any]) -> None:
        """交给专家 = 这段对话从此是专家对话（原地转，同一条线；0017「转建」）。调用方持有事务与行锁。"""
        if session["kind"] == ConversationKind.EXPERT:
            return
        await self.repo.set_session_kind(
            str(session["id"]), ConversationKind.EXPERT.value
        )
        await self.repo.append_event(
            session_id=str(session["id"]),
            kind="session",
            event_type="session/kind_changed",
            payload={"from": str(session["kind"]), "to": ConversationKind.EXPERT.value},
        )

    async def rename(
        self, session_id: str, *, owner_actor_id: str, title: str | None
    ) -> dict[str, Any]:
        async with self.repo.transaction():
            await self.repo.get_session(
                session_id, owner_actor_id=owner_actor_id, for_update=True
            )
            await self.repo.set_session_title(session_id, clean_title(title))
            return await self.repo.get_session(session_id)

    async def archive(
        self, session_id: str, *, owner_actor_id: str, archived: bool = True
    ) -> dict[str, Any]:
        """侧边栏的「删除」：从工作台移除这段对话。专家正在这段对话里干活就不许。"""
        async with self.repo.transaction():
            session = await self.repo.get_session(
                session_id, owner_actor_id=owner_actor_id, for_update=True
            )
            if archived and session.get("active_task_id") is not None:
                raise ProjectBusy("the advisor is still working in this conversation")
            if archived and session["project_id"] is not None:
                active = await self.repo.active_task_of_project(
                    str(session["project_id"])
                )
                if active is not None and str(active["session_id"]) == str(
                    session["id"]
                ):
                    raise ProjectBusy(
                        "the expert is still working in this conversation; "
                        "it cannot be removed now"
                    )
            await self.repo.archive_session(session_id, archived=archived)
            return await self.repo.get_session(session_id)

    async def _refuse_while_expert_works(
        self, session: dict[str, Any], *, becoming_work: bool = False
    ) -> None:
        """专家在这个项目里干活期间，别的对话可以聊天，不可以工作（所有者 2026-09-29 定）。"""
        if session["project_id"] is None:
            return
        if session["kind"] not in HANDS_ON and not becoming_work:
            return
        active = await self.repo.active_task_of_project(
            str(session["project_id"]), lane="execution"
        )
        if active is not None and str(active["session_id"]) != str(session["id"]):
            raise ProjectHeldByExpert(
                "the expert is working in this project; you can chat, and work again "
                "when it hands back",
                project_id=str(session["project_id"]),
                active_task_id=str(active["id"]),
            )

    async def assert_user_may_drive(
        self, session_id: str, *, owner_actor_id: str
    ) -> dict[str, Any]:
        """用户发 turn 前的门（F-WHEEL-02）。"""
        session = await self.repo.get_session(session_id, owner_actor_id=owner_actor_id)
        if session["wheel"] != Wheel.user:
            raise WheelHeldByOther(
                "the advisor holds the wheel; you can watch, answer interactions or cancel",
                session_id=session_id,
                active_task_id=str(session["active_task_id"]),
            )
        await self._refuse_while_expert_works(session)
        return session

    async def assert_advisor_may_drive(
        self, session_id: str, *, task_id: str
    ) -> dict[str, Any]:
        session = await self.repo.get_session(session_id)
        if (
            session["wheel"] != Wheel.advisor
            or str(session["active_task_id"]) != task_id
        ):
            raise WheelHeldByOther("the user holds the wheel", session_id=session_id)
        return session

    async def record_user_turn_requested(
        self, session_id: str, *, owner_actor_id: str, text: str, request_id: str
    ) -> dict[str, Any]:
        """把用户的 turn 请求记成事件（runner 消费 Outbox 里的这条去发 turn/start）。"""
        async with self.repo.transaction():
            await self.assert_user_may_drive(session_id, owner_actor_id=owner_actor_id)
            await self.repo.touch_session(session_id)
            await self.repo.set_session_title_if_empty(session_id, default_title(text))
            return await self.repo.append_event(
                session_id=session_id,
                kind="turn",
                event_type="turn/requested",
                payload={"request_id": request_id, "text": text, "by": "user"},
            )

    async def usage(
        self, session_id: str, *, owner_actor_id: str, prices: PriceList
    ) -> dict[str, Any]:
        """这段对话到现在用了多少、花了多少（估算）。"""
        await self.repo.get_session(session_id, owner_actor_id=owner_actor_id)
        events = await self.repo.list_record_events(
            session_id=session_id, types=("thread/tokenUsage/updated",)
        )
        return conversation_usage(events, prices)

    async def view(self, session_id: str, *, owner_actor_id: str) -> dict[str, Any]:
        s = await self.repo.get_session(session_id, owner_actor_id=owner_actor_id)
        pending = await self.repo.list_interactions(
            session_id=session_id, status="pending"
        )
        for d in (s, *pending):
            for k, v in list(d.items()):
                if hasattr(v, "hex") and not isinstance(v, (bytes, str)):
                    d[k] = str(v)
            d.pop("token_hash", None)
        return {"session": s, "pending_interactions": pending}
