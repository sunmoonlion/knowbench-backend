"""Project the existing test engine into knowlab, without another execution path."""

from __future__ import annotations

from collections import Counter
from typing import Any

from app.application.ports.workbench import WorkbenchStore
from app.application.workbench.catalog import SOURCE_TEST
from app.application.workbench.expert_desk import ExpertDesk
from app.domain.workbench.errors import NotFound
from app.domain.workbench.test_runs import case_outputs, trial_profile_id


async def execution_view(
    repo: WorkbenchStore,
    run_id: str,
    *,
    owner_subject: str,
    case_id: str | None = None,
) -> dict[str, Any]:
    run = await repo.get_test_run(run_id)
    if run is None or (run.get("request") or {}).get("owner_subject") != owner_subject:
        raise NotFound("no such test run")
    actor = await repo.actor_for_subject(owner_subject)
    if actor is None:
        raise NotFound("no such test owner")
    cases = await repo.list_test_cases(run_id)
    if case_id is not None and not any(row["case_id"] == case_id for row in cases):
        raise NotFound("no such test case")
    desk = ExpertDesk(repo)
    shown: list[dict[str, Any]] = []
    for row in cases:
        if case_id is not None and row["case_id"] != case_id:
            continue
        result: dict[str, Any] = {
            "case_id": row["case_id"],
            "state": "PENDING",
            "spent": "0",
            "steps": [],
        }
        if not row.get("task_id"):
            shown.append(result)
            continue
        task_id = str(row["task_id"])
        task = await repo.get_task(task_id, owner_actor_id=actor)
        if task.get("pack_source") != SOURCE_TEST or task.get(
            "profile_id"
        ) != trial_profile_id(run_id):
            raise NotFound("test task binding differs")
        view = await desk.steps(task_id, owner_actor_id=actor)
        now = view.get("now") or {}
        sheet = view["task"]
        result.update(
            state=sheet["state"],
            waiting_reason=sheet.get("waiting_reason"),
            title=(now.get("step") or {}).get("title", ""),
            activity=(now.get("doing") or {}).get("text", ""),
            since=now.get("since"),
            last_event_at=now.get("last_event_at"),
            spent=sheet["budget"]["spent"],
            plan={
                key: sheet["plan"].get(key) for key in ("version", "planning", "reason")
            }
            if sheet.get("plan")
            else None,
        )
        full = []
        for step in view["steps"]:
            if case_id is not None:
                step = (await desk.step(task_id, step["index"], owner_actor_id=actor))[
                    "step"
                ]
                full.append(step)
            tools: Counter[str] = Counter()
            activity = []
            for attempt in step["attempts"]:
                tools.update(attempt.get("tools") or {})
                activity.extend(attempt.get("process") or [])
            result["steps"].append(
                {
                    "step_id": step["step_id"],
                    "index": step["index"],
                    "title": step["title"],
                    "status": step["status"],
                    "selection_reason": step.get("selection_reason"),
                    "attempts": step["times"],
                    "spent": step["spent"],
                    "tools": dict(tools),
                    # Never proxy raw command arguments/results or model private context.
                    "latest_activity": [
                        {
                            "at": item.get("at"),
                            "name": item["name"],
                            "ok": bool(item["ok"]),
                        }
                        for item in activity[-20:]
                    ],
                }
            )
        if case_id is not None:
            # For terminal cases prefer the archived file, never reread a shared path.
            saved = await repo.get_test_case_outputs(run_id, case_id)
            result["outputs"] = (
                dict(saved)
                if saved
                else case_outputs(
                    run_id=run_id,
                    case_id=case_id,
                    steps=full,
                    deliverable=None,
                    deliverable_note=(
                        "测试已结束，成稿尚未归入测试记录，请重新读取。"
                        if sheet["state"] in {"SUCCEEDED", "FAILED", "CANCELLED"}
                        else "测试仍在执行，成稿在完成后保存。"
                    ),
                )
            )
        shown.append(result)
    return {
        "contract": "knowbot-test-execution/v1",
        "run_id": run_id,
        "status": run["status"],
        "cases": shown,
    }
