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
        bound_source = None
        if snapshot.dependencies:
            step = execution_steps(snapshot)[int(task["current_step"])]
            leaf = dependency_for_step(snapshot, step)
            draft = leaf.draft
            bound_source = snapshot.execution_source(leaf)
        source = bound_source or draft.source
        if source is not None and source.kind == "registered_script":
            from app.domain.workbench.source_tasks import script_input

            revision = await svc.formal_sources.registry.check_direct_source(
                owner, source
            )
            payload = script_input(revision, revision.content, intent["arguments"])
        else:
            resources = await repo.skill_package_files(owner, asset["id"])
            payload = method_script_input(
                asset, draft, intent["arguments"], resources, bound_source=bound_source
            )
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
    if intent.get("script_cleanup_pending"):
        await _release_job(svc, owner, cid)
        return
    delayed = datetime.now(UTC) - call["updated_at"] > timedelta(minutes=2)
    try:
        receipt = await executor.status(owner, cid)
    except Exception:
        if delayed:
            await _missing_receipt(svc, call)
        raise
    authorized = call["status"] == "dispatching"
    if authorized:
        try:
            await svc._check_before_send(owner, call, intent)
        except WorkbenchError:
            authorized = False
    if receipt["state"] in ("running", "absent"):
        if delayed or (receipt["state"] == "absent" and not authorized):
            await _missing_receipt(svc, call)
        if receipt["state"] == "running" and (not authorized or delayed):
            await executor.stop_metered(owner, cid)
        return
    try:
        meter = await executor.container_meter(owner, cid)
    except Exception:
        if delayed:
            await _missing_receipt(svc, call)
        raise
    amount, evidence = rated_usage(intent["script_quote"], cid, meter)
    grants = {}
    if authorized:
        try:
            grants = await svc._dispatch_grants(owner, intent)
        except WorkbenchError:
            authorized = False
    async with svc.repo.transaction():
        await svc._locked_call_session(owner, call)
        current = await svc.repo.capability_call(owner, cid, lock=True)
        if "consume" in await svc.repo.script_budget_entries(cid):
            return
        intent = current["detail"]
        if current["status"] == "dispatching":
            try:
                await svc._check_before_send(owner, current, intent, grants=grants)
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
            intent = {**intent, "result": result}
            await svc.repo.append_event(
                session_id=call["session_id"],
                task_id=call["task_id"],
                attempt_id=call["attempt_id"],
                kind="tool",
                event_type="capability/action_completed",
                payload={"call_id": cid, "status": status, "script_cost": str(amount)},
            )
        else:
            status = current["status"]
            await svc.repo.append_event(
                session_id=call["session_id"],
                task_id=call["task_id"],
                attempt_id=call["attempt_id"],
                kind="tool",
                event_type="capability/script_cost_settled",
                payload={"call_id": cid, "status": status, "script_cost": str(amount)},
            )
        await svc.repo.update_call(
            owner, cid, status, {**intent, "script_cleanup_pending": True}
        )
    await _release_job(svc, owner, cid)


async def _missing_receipt(svc, call):
    owner, cid = call["owner_actor_id"], call["id"]
    async with svc.repo.transaction():
        await svc._locked_call_session(owner, call)
        current = await svc.repo.capability_call(owner, cid, lock=True)
        if current["status"] != "dispatching" or current["detail"] != call["detail"]:
            return
        await svc.repo.update_call(
            owner,
            cid,
            "unknown",
            {
                **current["detail"],
                "result": {
                    "isError": True,
                    "message": "Script receipt pending reconciliation",
                },
            },
        )
        await svc.repo.append_event(
            session_id=call["session_id"],
            task_id=call["task_id"],
            attempt_id=call["attempt_id"],
            kind="tool",
            event_type="capability/action_completed",
            payload={"call_id": cid, "status": "unknown"},
        )


async def _release_job(svc, owner, cid):
    await svc.script_executor.cancel(owner, cid)
    async with svc.repo.transaction():
        current = await svc.repo.capability_call(owner, cid, lock=True)
        detail = dict(current["detail"])
        if detail.pop("script_cleanup_pending", False):
            await svc.repo.update_call(owner, cid, current["status"], detail)
