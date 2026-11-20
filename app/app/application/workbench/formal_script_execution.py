"""Metered host dispatch. Collector reads one Job and never resubmits input."""

from datetime import UTC, datetime, timedelta
from decimal import Decimal

from app.application.workbench.script_budget import settle
from app.domain.workbench.capabilities import ScriptNotProvisioned
from app.domain.workbench.errors import WorkbenchError
from app.domain.workbench.formal_execution import (
    dependency_for_step,
    execution_steps,
    snapshot_for_task,
)
from app.domain.workbench.script_pricing import rated_usage
from app.domain.workbench.skill_execution import method_script_input


async def dispatch(svc, owner, call, intent, asset):
    repo = svc.repo
    host = intent["formal_host"]
    try:
        await svc._check_before_send(owner, call, intent)
        task = await repo.get_task(host["task_id"], owner_actor_id=owner)
        snapshot = snapshot_for_task(task)
        draft = snapshot.draft
        if snapshot.dependencies:
            step = execution_steps(snapshot)[int(task["current_step"])]
            draft = dependency_for_step(snapshot, step).draft
        resources = await repo.skill_package_files(owner, asset["id"])
        payload = method_script_input(asset, draft, intent["arguments"], resources)
    except Exception:
        await settle(
            repo,
            owner,
            call["id"],
            amount=Decimal("0"),
            evidence={"run_id": call["id"], "stage": "before_provisioning"},
        )
        raise
    try:
        await svc.script_executor.prepare(owner, call["id"], payload)
        await svc._check_before_send(owner, call, intent)
        await svc.script_executor.start(owner, call["id"], payload)
    except ScriptNotProvisioned as exc:
        await settle(
            repo,
            owner,
            call["id"],
            amount=Decimal("0"),
            evidence={"run_id": call["id"], "stage": exc.stage},
        )
        raise
    except Exception:
        # The reservation request itself can start a billable container.
        # Keep its budget until the collector obtains authoritative usage.
        try:
            await svc.script_executor.stop_metered(owner, call["id"])
        except Exception:
            pass
        raise


async def request_stop(svc, owner, call):
    async with svc.repo.transaction():
        await svc._locked_call_session(owner, call)
        current = await svc.repo.capability_call(owner, call["id"], lock=True)
        if current["status"] != "dispatching":
            return
        detail = current["detail"]
        if not (detail.get("result") or {}).get("stop_requested"):
            await svc.repo.update_call(
                owner,
                call["id"],
                "dispatching",
                {
                    **detail,
                    "result": {"stop_requested": True, "message": "Stop requested"},
                },
            )
    # The runner also consumes this durable request after an outage or restart.
    if svc.script_executor:
        try:
            await svc.script_executor.stop_metered(owner, call["id"])
        except Exception:
            pass


async def reconcile(svc):
    for call in await svc.repo.pending_metered_scripts():
        try:
            await _collect(svc, call)
        except (WorkbenchError, ValueError):
            # Incomplete/conflicting evidence cannot authorize a debit or release.
            continue
        except Exception:
            # A temporary provisioner outage leaves this exact call recoverable.
            continue


async def _collect(svc, call):
    owner, cid, intent = call["owner_actor_id"], call["id"], call["detail"]
    executor = svc.script_executor
    receipt = await executor.status(owner, cid)
    authorized = call["status"] == "dispatching"
    if authorized:
        try:
            await svc._check_before_send(owner, call, intent)
        except WorkbenchError:
            authorized = False
    delayed = datetime.now(UTC) - call["updated_at"] > timedelta(minutes=2)
    if receipt["state"] in ("running", "absent"):
        if receipt["state"] == "running" and (not authorized or delayed):
            await executor.stop_metered(owner, cid)
        if (delayed or not authorized) and not (intent.get("result") or {}).get(
            "stop_requested"
        ):
            async with svc.repo.transaction():
                current = await svc.repo.capability_call(owner, cid, lock=True)
                if current["status"] == "dispatching":
                    await svc.repo.update_call(
                        owner,
                        cid,
                        "unknown" if authorized else "failed",
                        {
                            **intent,
                            "result": {
                                "isError": True,
                                "message": "Script receipt pending reconciliation"
                                if authorized
                                else "Script execution authority ended",
                            },
                        },
                    )
        return
    meter = await executor.container_meter(owner, cid)
    amount, evidence = rated_usage(intent["script_quote"], cid, meter)
    async with svc.repo.transaction():
        await svc.repo.get_task(
            str(call["task_id"]), owner_actor_id=owner, for_update=True
        )
        current = await svc.repo.capability_call(owner, cid, lock=True)
        if current["status"] == "dispatching":
            try:
                await svc._check_before_send(owner, current, intent)
            except WorkbenchError:
                authorized = False
        await settle(svc.repo, owner, cid, amount=amount, evidence=evidence)
        if current["status"] == "dispatching":
            result = (
                receipt.get("result")
                if authorized and receipt["state"] == "finished"
                else None
            )
            if result is None:
                result = {
                    "isError": True,
                    "message": receipt.get("error")
                    or "Script execution authority ended",
                }
            else:
                result = {
                    **result,
                    "isError": result["exit_code"] != 0 or result["timed_out"],
                }
            status = "failed" if result["isError"] else "succeeded"
            await svc.repo.update_call(owner, cid, status, {**intent, "result": result})
            await svc.repo.append_event(
                session_id=call["session_id"],
                task_id=call["task_id"],
                attempt_id=call["attempt_id"],
                kind="tool",
                event_type="capability/action_completed",
                payload={"call_id": cid, "status": status, "script_cost": str(amount)},
            )
    await executor.cancel(owner, cid)
