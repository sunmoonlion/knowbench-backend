"""一次委托在用户项目目录里写了哪些底稿（所有者 2026-10-14：按对话、按次分开存，不覆盖）。

文件在用户电脑上，是真源；这里只按规则算出它们该在哪儿，网页「工作底稿」栏拿去经本机回环口读。
算得出不等于写出来了：没写成的，网页读的时候会是「找不到」。
"""

from __future__ import annotations

from typing import Any

from app.application.ports.workbench import WorkbenchStore
from app.application.workbench.execution_plan import display_for_task
from app.domain.workbench.deliverables import run_files, run_folder


async def task_dossier(
    repo: WorkbenchStore, task: dict[str, Any]
) -> dict[str, Any] | None:
    """{folder, expert, files}；专家包找不到或这位专家不写文件时是 None。"""
    pack = await display_for_task(repo, task)
    if pack is None or not pack.deliverable_files() or not task.get("created_at"):
        return None
    expert = pack.name or pack.title
    folder = run_folder(str(task["session_id"]), task["created_at"], expert)
    return {
        "folder": folder,
        "expert": expert,
        "files": run_files(pack.deliverable_files(), folder),
    }
