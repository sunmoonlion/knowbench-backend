"""专家读用户自己的数据（SDD 0027 §3.5、SDD 0042 第五节）：工作台这一道核，核过再读。

工具是 `list_my_data`、`read_my_data` 与按格子读的 `read_my_table`（0030 §3.1），挂在读项目记录的
同一个工具服务上，每次带委托编号：
- 委托是令牌里这个用户的、没结束、方向盘在专家手里（同读项目记录）；
- 这件委托钉住了数据（请专家时用户从本地、云端知识库选的）：只读那几份，读时核摘要
  （pinned_data）；
- 没钉的是旧格式的专家，照旧经 knowlab 读作者绑的（留到「财务分析与预测」第 1 版停用）：
  委托的专家是用户请来的分身，或 knowlab 测试中的草稿；要读的数据在这一版清单的
  `data_bindings` 里（分身看钉表里的清单，摘要要和委托上的一样；测试看测试请求里的清单）；
  knowlab 那边按自己的草稿与交付记录再核一次。
委托一结束，这里就不再读。
"""

from __future__ import annotations

from typing import Any

from app.application.ports.workbench import KnowbotDataSource, WorkbenchStore
from app.application.workbench.catalog import SOURCE_KNOWMART, SOURCE_TEST
from app.application.workbench.pinned_data import LOCAL, pinned_purpose
from app.application.workbench.records import ProjectRecords, as_id
from app.application.workbench.test_runs import ACTIVE
from app.domain.workbench import knowbots
from app.domain.workbench.errors import NotFound, RecordsRefused

NO_DATA = "this expert has no data of the user to read"


class KnowbotData:
    def __init__(self, records: ProjectRecords, source: KnowbotDataSource) -> None:
        self.records = records
        self.repo = records.repo
        self.source = source

    async def list(self, task: Any) -> dict[str, Any]:
        working, purpose, bound = await self._scope(task)
        found = [d for d in await self.source.list(purpose) if same_binding(d, bound)]
        await self.records._noted(working, "list_my_data")
        return {"datasets": found}

    async def read(self, task: Any, dataset: Any, page: Any = 1) -> dict[str, Any]:
        working, purpose, bound = await self._scope(task)
        dataset_id = as_id(dataset, "data")
        if dataset_id not in bound:
            raise NotFound("data not found")
        try:
            number = max(1, int(page or 1))
        except (TypeError, ValueError):
            number = 1
        result = await self.source.read(purpose, dataset_id, number)
        if not same_binding(result.get("citation") or {}, bound):
            raise NotFound("data not found")
        await self.records._noted(working, "read_my_data", item=dataset_id, page=number)
        return result

    async def table(
        self, task: Any, dataset: Any, sheet: Any = None, page: Any = 1
    ) -> dict[str, Any]:
        working, purpose, bound = await self._scope(task)
        dataset_id = as_id(dataset, "data")
        if dataset_id not in bound:
            raise NotFound("data not found")
        name = str(sheet)[:512] if isinstance(sheet, str) and sheet else None
        try:
            number = max(1, int(page or 1))
        except (TypeError, ValueError):
            number = 1
        result = await self.source.table(purpose, dataset_id, name, number)
        if not same_binding(result.get("citation") or {}, bound):
            raise NotFound("data not found")
        await self.records._noted(
            working,
            "read_my_table",
            item=dataset_id,
            sheet=result.get("sheet"),
            page=number,
        )
        return result

    async def _scope(
        self, task: Any
    ) -> tuple[dict[str, Any], dict[str, Any], dict[str, tuple[str, str]]]:
        """（委托，给 knowlab 的用途，这一版能读的数据）。"""
        working = await self.records._working(task)
        purpose, bound = await data_scope(self.repo, self.records.owner, working)
        return working, purpose, bound


async def data_scope(
    repo: WorkbenchStore, owner: str, working: dict[str, Any]
) -> tuple[dict[str, Any], dict[str, tuple[str, str]]]:
    """这个委托能读哪些数据：（给数据源的用途，数据 id → （版本，sha256））。

    委托本身该不该读（是不是这个用户的、方向盘在不在专家手里）由调用方先核。
    钉住的数据「版本」一栏是来源（cloud / local）。
    """
    pins = await repo.list_task_data(str(working["id"]))
    if pins:
        relay_user = None
        if any(p["source"] == LOCAL for p in pins):
            identity = await repo.get_relay_identity(owner)
            relay_user = str(identity["relay_user"]) if identity else None
        return pinned_purpose(pins, relay_user)
    source = working.get("pack_source")
    if source == SOURCE_KNOWMART:
        ref = knowbots.parse_profile(working.get("profile_id"))
        pin = (
            await repo.get_knowbot_pin(
                owner_actor_id=owner, knowbot_id=ref[0], version=ref[1]
            )
            if ref
            else None
        )
        if pin is None or pin["digest"] != working.get("pack_digest"):
            raise RecordsRefused(NO_DATA)
        purpose = {
            "kind": "knowbot",
            "owner_subject": pin["owner_subject"],
            "knowbot_id": ref[0] if ref else "",
            "version": ref[1] if ref else "",
            "digest": pin["digest"],
        }
        bound = knowbots.data_bindings(pin["manifest"])
    elif source == SOURCE_TEST:
        run = await repo.test_run_for_task(str(working["id"]))
        if run is None or run["status"] not in ACTIVE:
            raise RecordsRefused(NO_DATA)
        if (run["request"] or {}).get("data_grant"):
            # 带了测试运行授权却没钉上：只凭授权读，不退回经 knowlab 读（Astra 评审 F02）
            raise RecordsRefused(NO_DATA)
        purpose = {"kind": "test", "run_id": run["run_id"]}
        bound = knowbots.data_bindings((run["request"] or {}).get("manifest"))
    else:
        raise RecordsRefused(NO_DATA)
    if not bound:
        raise RecordsRefused(NO_DATA)
    return purpose, bound


def same_binding(entry: dict[str, Any], bound: dict[str, tuple[str, str]]) -> bool:
    """读回来的就是能读的那一份：数据 id、版本、sha256 都一样。"""
    found = bound.get(str(entry.get("dataset_id")))
    return found is not None and found == (
        str(entry.get("version")),
        str(entry.get("sha256")),
    )
