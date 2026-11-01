"""工作区与项目（PRD/apps/investment.md 第二、七节）。只管账；用户机器上的目录由代理去建（0024），这里登记前问一声在不在。"""

from __future__ import annotations

import logging
from typing import Any

from app.application.ports.workbench import WorkbenchStore
from app.application.workbench.agent_service import AgentRefused, AgentService
from app.application.workbench.catalog import expert_name
from app.domain.workbench.errors import (
    ProjectArchived,
    ProjectBusy,
    ProjectDirectoryMissing,
    RootOutsideWhitelist,
    WorkbenchError,
)
from app.domain.workbench.projects import (
    clean_relative_path,
    clean_title,
    default_project_title,
    project_dir,
    split_under_roots,
)
from app.domain.workbench.states import TASK_TERMINAL
from app.domain.workbench.step_view import TERMINAL_WORDS, money

ONLINE = "online"
log = logging.getLogger(__name__)


def plain(row: dict[str, Any]) -> dict[str, Any]:
    """账里取出来的一行，变成能直接给接口的样子。"""
    out: dict[str, Any] = {}
    for key, value in row.items():
        if hasattr(value, "hex") and not isinstance(value, (bytes, str)):
            out[key] = str(value)
        else:
            out[key] = value
    return out


class ProjectService:
    def __init__(self, repo: WorkbenchStore, agent: AgentService | None = None):
        self.repo = repo
        self.agent = agent

    async def directory_state(self, owner_actor_id: str, directory: str) -> str | None:
        """经会合点服务流问代理这个目录：dir / file / other / missing；问不到（没配通道、没配对、代理离线或旧版）给 None。"""
        if self.agent is None:
            return None
        identity = await self.repo.get_relay_identity(owner_actor_id)
        relay_user = identity.get("relay_user") if identity else None
        if not relay_user:
            return None
        try:
            result = await self.agent.call(
                str(relay_user), "fs/getMetadata", {"path": directory}
            )
        except AgentRefused as exc:
            return "missing" if exc.code == "not_found" else None
        except WorkbenchError:
            return None
        return str(result.get("type") or "") or None

    async def workspaces(self, *, owner_actor_id: str) -> list[dict[str, Any]]:
        """我的工作区：每台机器的每个根目录。增减在用户机器上的本地代理里做，这里只读。"""
        found = []
        for env in await self.repo.list_environments(owner_actor_id=owner_actor_id):
            for root in env.get("roots") or []:
                found.append(
                    {
                        "environment_id": str(env["id"]),
                        "environment_name": env["name"],
                        "online": env.get("status") == ONLINE,
                        "root": root,
                    }
                )
        return found

    def _describe(
        self, project: dict[str, Any], env: dict[str, Any] | None
    ) -> dict[str, Any]:
        row = plain(project)
        row["directory"] = project_dir(project["workspace_root"], project["path"])
        row["archived"] = project.get("archived_at") is not None
        # 机器被移除（账 179）后项目还挂着它：字段不能缺，缺了前端整份清单校验不过
        row["environment_name"] = env["name"] if env is not None else "已移除的机器"
        row["online"] = env is not None and env.get("status") == ONLINE
        return row

    async def create(
        self,
        *,
        owner_actor_id: str,
        environment_id: str,
        workspace_root: str,
        path: str | None,
        title: str | None = None,
    ) -> dict[str, Any]:
        relative = clean_relative_path(path)
        env = await self.repo.get_environment(
            environment_id, owner_actor_id=owner_actor_id
        )
        if workspace_root not in list(env.get("roots") or []):
            raise RootOutsideWhitelist(
                "this workspace is not one of the machine's whitelisted roots",
                workspace_root=workspace_root,
            )
        # 先建后记（0024）：网页先请代理建好或拉好，这里再核一次。明确不在就不登记；问不到照登记（代理新版装上前不至于建不了）
        directory = project_dir(workspace_root, relative)
        state = await self.directory_state(owner_actor_id, directory)
        if state is None:
            log.warning("project directory not checked", extra={"code": "unchecked"})
        elif state != "dir":
            raise ProjectDirectoryMissing(
                "this folder is not on the computer; create it from the new-project dialog",
                directory=directory,
                found=state,
            )
        async with self.repo.transaction():
            env = await self.repo.get_environment(
                environment_id, owner_actor_id=owner_actor_id
            )
            if workspace_root not in list(env.get("roots") or []):
                raise RootOutsideWhitelist(
                    "this workspace is not one of the machine's whitelisted roots",
                    workspace_root=workspace_root,
                )
            project_id = await self.repo.create_project(
                owner_actor_id=owner_actor_id,
                environment_id=environment_id,
                workspace_root=workspace_root,
                path=relative,
                title=clean_title(title)
                or default_project_title(relative, workspace_root),
            )
            return self._describe(await self.repo.get_project(project_id), env)

    async def ensure_for_directory(
        self, *, owner_actor_id: str, environment_id: str, directory: str
    ) -> dict[str, Any]:
        """按完整目录找项目，没有就建。给仍按完整目录建会话的旧入口用。"""
        env = await self.repo.get_environment(
            environment_id, owner_actor_id=owner_actor_id
        )
        roots: list[str] = list(env.get("roots") or [])
        found = split_under_roots(directory, roots)
        if found is None:
            if roots:
                raise RootOutsideWhitelist(
                    "project root is outside the environment's whitelisted roots",
                    project_root=directory,
                )
            found = (directory, "")  # 这台机器没有登记白名单：目录自己当工作区
        root, relative = found
        existing = await self.repo.find_project(
            owner_actor_id=owner_actor_id,
            environment_id=environment_id,
            workspace_root=root,
            path=relative,
        )
        if existing is not None:
            return existing
        project_id = await self.repo.create_project(
            owner_actor_id=owner_actor_id,
            environment_id=environment_id,
            workspace_root=root,
            path=relative,
            title=default_project_title(relative, root),
        )
        return await self.repo.get_project(project_id)

    async def listing(
        self, *, owner_actor_id: str, include_archived: bool = False
    ) -> list[dict[str, Any]]:
        projects = await self.repo.list_projects(
            owner_actor_id=owner_actor_id, include_archived=include_archived
        )
        envs = {
            str(env["id"]): env
            for env in await self.repo.list_environments(owner_actor_id=owner_actor_id)
        }
        # 清单不含已移除的机器（账 179）：项目还引用它，补查回来，名字照显示、状态算离线
        for env_id in {str(p["environment_id"]) for p in projects} - set(envs):
            try:
                envs[env_id] = await self.repo.get_environment(env_id)
            except WorkbenchError:
                pass
        return [
            self._describe(project, envs.get(str(project["environment_id"])))
            for project in projects
        ]

    async def view(self, project_id: str, *, owner_actor_id: str) -> dict[str, Any]:
        """一个项目，连同它的对话与委托的摘要。"""
        project = await self.repo.get_project(project_id, owner_actor_id=owner_actor_id)
        env = await self.repo.get_environment(str(project["environment_id"]))
        active = await self.repo.active_task_of_project(str(project["id"]))
        sessions = await self.repo.list_sessions(
            owner_actor_id=owner_actor_id, project_id=str(project["id"])
        )
        tasks = await self.repo.list_tasks(
            owner_actor_id=owner_actor_id, project_id=str(project["id"])
        )
        return {
            "project": self._describe(project, env),
            "active_task_id": str(active["id"]) if active else None,
            "conversations": [
                {
                    "id": str(s["id"]),
                    "kind": s["kind"],
                    "title": s["title"],
                    "wheel": s["wheel"],
                    "active_task_id": (
                        str(s["active_task_id"]) if s["active_task_id"] else None
                    ),
                    "last_active_at": s["last_active_at"],
                }
                for s in sessions
            ],
            "tasks": [
                {
                    "id": str(t["id"]),
                    "session_id": str(t["session_id"]),
                    "profile_id": t["profile_id"],
                    # 给页面看的是专家的名字和终态的白话，不是代号
                    "expert": _expert_name(t),
                    "state": t["state"],
                    "state_word": TERMINAL_WORDS.get(t["state"]),
                    "question": _question(t),
                    "budget": t["budget"],
                    "spent": money((t["budget"] or {}).get("used")),
                    "currency": (t["budget"] or {}).get("currency") or "CNY",
                    "created_at": t["created_at"],
                    "ended_at": t["updated_at"]
                    if t["state"] in TASK_TERMINAL
                    else None,
                }
                for t in tasks
            ],
        }

    async def rename(
        self, project_id: str, *, owner_actor_id: str, title: str
    ) -> dict[str, Any]:
        cleaned = clean_title(title)
        if cleaned is None:
            raise ValueError("title must not be empty")
        async with self.repo.transaction():
            await self.repo.get_project(
                project_id, owner_actor_id=owner_actor_id, for_update=True
            )
            await self.repo.update_project(project_id, title=cleaned)
        return (await self.view(project_id, owner_actor_id=owner_actor_id))["project"]

    async def archive(
        self, project_id: str, *, owner_actor_id: str, archived: bool
    ) -> dict[str, Any]:
        async with self.repo.transaction():
            await self.repo.get_project(
                project_id, owner_actor_id=owner_actor_id, for_update=True
            )
            if archived and await self.repo.active_task_of_project(project_id):
                raise ProjectBusy(
                    "the expert is still working in this project; it cannot be archived now"
                )
            await self.repo.update_project(project_id, archived=archived)
        return (await self.view(project_id, owner_actor_id=owner_actor_id))["project"]

    async def usable(self, project_id: str, *, owner_actor_id: str) -> dict[str, Any]:
        """要在里面开对话、请专家的项目：必须是自己的、没有归档的。"""
        project = await self.repo.get_project(project_id, owner_actor_id=owner_actor_id)
        if project.get("archived_at") is not None:
            raise ProjectArchived("this project is archived; restore it first")
        return project


def _expert_name(task: dict[str, Any]) -> str:
    return expert_name(task)


def _question(task: dict[str, Any]) -> str | None:
    original = task.get("original_input") or {}
    given = original.get("input") if isinstance(original, dict) else None
    if isinstance(given, dict):
        text = given.get("text") or given.get("question")
        return str(text) if text else None
    return str(given) if given else None
