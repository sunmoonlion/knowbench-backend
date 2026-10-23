"""我的机器：本地代理连着会合点时，把它报上来的机器登记到账上，并维护在线、离线。

代理不直接找工作台：它只出站连会合点，hello 里带上机器名、白名单目录和它自己定的上限。
会合点记着现在谁在线；这里隔一会儿问它一次，对一遍账：

- 在线而且报了机器名的：报了设备编号（0.5.2 起）按编号认，没报按名字认；有就更新、没有就建，置为在线；这个人名下别的机器置为离线
  （一个人同一时刻只有一个代理连得上，新的会把旧的顶掉）。
- 不在线的：这个人名下的机器全部置为离线。
- 在线但没报机器名的（老代理）：不动。它的机器是手工登记的，分不清是哪一台。

会合点问不到时什么都不改：宁可显示旧状态，也不把所有人的机器一齐标成离线。
白名单目录在这里只是显示和建项目时的核对；真正拦住越界的是代理自己。
"""

from __future__ import annotations

import asyncio
import logging
import re
from typing import Any

from app.application.ports.workbench import RelayAdmin, WorkbenchStores
from app.application.workbench.agent_reports import record_agent_reports
from app.domain.workbench.errors import WorkbenchError

log = logging.getLogger(__name__)

SANDBOX_MODES = ("read-only", "workspace-write", "danger-full-access")
MAX_ROOTS = 64
DEVICE_ID = re.compile(r"[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}")


def _absolute(path: Any) -> bool:
    """绝对路径才算白名单目录：`/home/u/x`，或 Windows 的 `C:\\x`、`C:/x`、`\\\\server\\share`。"""
    if not isinstance(path, str) or not path:
        return False
    if path.startswith("/") or path.startswith("\\\\"):
        return True
    return len(path) >= 3 and path[0].isalpha() and path[1] == ":" and path[2] in "\\/"


def machine_report(agent: dict[str, Any]) -> dict[str, Any] | None:
    """从会合点给的一条在线代理里取出能登记的机器；没报名字就是没有。"""
    machine = agent.get("machine")
    if not isinstance(machine, dict):
        return None
    name = str(machine.get("name") or "").strip()[:128]
    if not name:
        return None
    roots_value = machine.get("roots")
    roots = roots_value if isinstance(roots_value, list) else []
    ceiling_value = machine.get("ceiling")
    reported = ceiling_value if isinstance(ceiling_value, dict) else {}
    ceiling: dict[str, Any] = {}
    if reported.get("sandbox") in SANDBOX_MODES:
        ceiling["sandbox"] = reported["sandbox"]
    if isinstance(reported.get("network"), bool):
        ceiling["network"] = reported["network"]
    device = machine.get("device_id")
    return {
        "name": name,
        "roots": [r[:1024] for r in roots if _absolute(r)][:MAX_ROOTS],
        "ceiling": ceiling,
        "agent_version": str(agent.get("software") or "")[:64] or None,
        "codex_version": str(agent.get("codex") or "")[:64] or None,
        # 0.5.2 起（账 143）：按设备编号认这份本地 link；主机名与系统用来发现配置被拷到别的电脑
        "device_id": device
        if isinstance(device, str) and DEVICE_ID.fullmatch(device)
        else None,
        "host": str(machine.get("host") or "")[:128] or None,
        "os": str(machine.get("os") or "")[:64] or None,
    }


class MachineSync:
    def __init__(
        self,
        stores: WorkbenchStores,
        relay_admin: RelayAdmin,
        *,
        interval_seconds: float = 10.0,
    ) -> None:
        self.stores = stores
        self.relay_admin = relay_admin
        self.interval_seconds = interval_seconds

    async def sync_once(self) -> dict[str, int]:
        agents = await self.relay_admin.agents()
        counts = {"online": 0, "offline": 0, "untouched": 0}
        receipts: list[dict[str, str]] = []
        async with self.stores() as repo:
            async with repo.transaction():
                for identity in await repo.list_relay_identities():
                    owner = str(identity["owner_actor_id"])
                    agent = agents.get(str(identity["relay_user"]))
                    if agent is None:
                        counts["offline"] += await repo.set_environments_offline(owner)
                        continue
                    report = machine_report(agent)
                    if report is None:
                        counts["untouched"] += 1
                        continue
                    env_id = await repo.report_environment(
                        owner_actor_id=owner,
                        name=report["name"],
                        agent_version=report["agent_version"],
                        codex_version=report["codex_version"],
                        roots=report["roots"],
                        ceiling=report["ceiling"],
                        device_id=report["device_id"],
                        host=report["host"],
                        os=report["os"],
                    )
                    counts["online"] += 1
                    counts["offline"] += await repo.set_environments_offline(
                        owner, except_id=env_id
                    )
                    # 在线心跳只排执行环境探测，不直接把 Task 改成可运行。
                    # runner 核对 app-server 的环境已 ready 后才经账房恢复。
                    await repo.queue_environment_recovery(env_id, owner_actor_id=owner)
                    receipts.extend(
                        await record_agent_reports(
                            repo,
                            owner=owner,
                            environment_id=str(env_id),
                            reports=agent.get("permission_reports", []),
                        )
                    )
        # A rollback or failed commit must never acknowledge permission reports.
        # Lost replies are retried from the relay queue and de-duplicated in DB.
        if receipts:
            await self.relay_admin.permission_receipts(receipts)
        return counts

    async def run_forever(self, stop: asyncio.Event) -> None:
        log.info("machine sync starting interval=%ss", self.interval_seconds)
        failing = False
        while not stop.is_set():
            try:
                await self.sync_once()
                if failing:
                    log.info("machine sync recovered")
                failing = False
            except WorkbenchError as exc:
                # 会合点问不到：只在刚坏的时候说一次，不刷屏
                if not failing:
                    log.warning("machine sync cannot reach the relay: %s", exc)
                failing = True
            except Exception:  # noqa: BLE001
                log.exception("machine sync iteration failed")
                failing = True
            try:
                await asyncio.wait_for(stop.wait(), timeout=self.interval_seconds)
            except TimeoutError:
                pass
