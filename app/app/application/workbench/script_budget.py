"""Reserve and settle one script invocation in the existing root Task budget."""

from decimal import Decimal

from app.domain.workbench.errors import BudgetExhausted, IdempotencyConflict
from app.domain.workbench.models import Budget


async def _locked(repo, owner, call_id):
    call = await repo.capability_call(owner, call_id)
    if not call.get("task_id") or not call.get("attempt_id"):
        raise BudgetExhausted("A metered script requires a root task budget")
    task = await repo.get_task(
        str(call["task_id"]), owner_actor_id=owner, for_update=True
    )
    attempt = await repo.get_attempt(str(call["attempt_id"]))
    if str(attempt["task_id"]) != str(task["id"]):
        raise IdempotencyConflict("The metered attempt belongs to a different root")
    call = await repo.capability_call(owner, call_id, lock=True)
    return task, call, await repo.script_budget_entries(call_id)


async def _entry(repo, task, call, kind, amount, evidence):
    await repo.ledger_append(
        task_id=str(task["id"]),
        attempt_id=str(call["attempt_id"]),
        entry=kind,
        amount=amount,
        script_call_id=str(call["id"]),
        script_evidence=evidence,
        note="isolated script execution",
    )


async def reserve(repo, owner, call_id, quote):
    """The approved quote and reservation commit before the first billable action."""
    async with repo.transaction():
        task, call, entries = await _locked(repo, owner, call_id)
        if entries:
            if entries["reserve"]["script_evidence"] != quote:
                raise IdempotencyConflict("Script price changed after reservation")
            return
        if (
            call["status"] != "dispatching"
            or call["detail"].get("execution_kind")
            not in ("formal_script", "registry_script")
            or call["detail"].get("script_quote") != quote
        ):
            raise IdempotencyConflict(
                "Script reservation differs from approved dispatch"
            )
        budget = Budget.model_validate(task["budget"])
        maximum = Decimal(quote["maximum"])
        if (
            not maximum.is_finite()
            or maximum < 0
            or budget.currency != quote["currency"]
            or maximum > budget.available
        ):
            raise BudgetExhausted("Root budget cannot cover the approved script price")
        await _entry(repo, task, call, "reserve", maximum, quote)
        budget.reserved += maximum
        await repo.cas_task(
            str(task["id"]),
            expected_version=task["state_version"],
            budget=budget.as_json(),
        )


async def settle(repo, owner, call_id, *, amount, evidence):
    """A missing receipt keeps its reservation. Duplicate receipts debit once."""
    async with repo.transaction():
        task, call, entries = await _locked(repo, owner, call_id)
        held = entries.get("reserve")
        if (
            held is None
            or not amount.is_finite()
            or not Decimal(0) <= amount <= held["amount"]
        ):
            raise IdempotencyConflict(
                "Script usage exceeds or lacks its approved reservation"
            )
        previous = entries.get("consume")
        if previous is not None:
            if previous["amount"] != amount or previous["script_evidence"] != evidence:
                raise IdempotencyConflict("Conflicting final script usage receipt")
            return
        budget = Budget.model_validate(task["budget"])
        if budget.reserved < held["amount"]:
            raise IdempotencyConflict("Root budget reservation is missing")
        await _entry(repo, task, call, "consume", amount, evidence)
        if held["amount"] > amount:
            await _entry(repo, task, call, "release", held["amount"] - amount, evidence)
        budget.reserved -= held["amount"]
        budget.used += amount
        await repo.cas_task(
            str(task["id"]),
            expected_version=task["state_version"],
            budget=budget.as_json(),
        )


def verify_proof(proof, call_id, quote):
    from app.domain.workbench.script_contract_generated import ScriptContainerMeter
    from app.domain.workbench.script_pricing import rated_usage

    if not proof or proof["quote"] != quote:
        raise ValueError("Approved tariff and metering proof differ")
    meter = ScriptContainerMeter.model_validate(proof["usage"]["meter"])
    amount, usage = rated_usage(quote, call_id, meter)
    if proof["usage"] != usage or amount != Decimal(proof["amount"]):
        raise ValueError(
            "Retained script consumption differs from the measured interval"
        )
    return amount


async def cost_proof(repo, call, subject):
    quote = call["detail"]["script_quote"]
    if subject.get("script_quote") != quote:
        raise ValueError("Approved price was not shown for this call")
    rows = await repo.script_budget_entries(call["id"])
    if "reserve" not in rows or "consume" not in rows:
        raise ValueError("Script consumption is unresolved")
    hold, charged = rows["reserve"], rows["consume"]
    if any(
        str(r["task_id"]) != str(call["task_id"])
        or str(r["attempt_id"]) != str(call["attempt_id"])
        for r in rows.values()
    ):
        raise ValueError("Script was charged to another task or attempt")
    release = rows.get("release", {}).get("amount", Decimal(0))
    if (
        hold["amount"] != Decimal(quote["maximum"])
        or hold["amount"] != charged["amount"] + release
    ):
        raise ValueError("Script reservation did not settle")
    proof = {
        "quote": hold["script_evidence"],
        "amount": str(charged["amount"]),
        "usage": charged["script_evidence"],
    }
    verify_proof(proof, call["id"], quote)
    return proof


def case_script_cost(case):
    # The recursive presenter also lists leaves in steps; count each call once.
    records = [case.get("governance")] + [
        step.get("governance") for step in case.get("steps", [])
    ]
    return sum(
        (
            Decimal(g["script_cost"]["amount"])
            for g in records
            if g and g.get("script_cost")
        ),
        Decimal(0),
    )


async def task_script_cost(repo, task_id):
    return sum(
        (
            r["amount"]
            for r in await repo.ledger_list(str(task_id))
            if r["entry"] == "consume" and r.get("script_call_id")
        ),
        Decimal(0),
    )
