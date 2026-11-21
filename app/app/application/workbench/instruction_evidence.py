"""Retain evidence of one bounded inference and its settled root-task charge."""

from decimal import Decimal

from app.domain.workbench.formal_execution import validate_values


def instruction_charge(task, attempt, entries):
    """Reconcile one provider receipt to its own debit and the shared root total."""
    if str(attempt["task_id"]) != str(task["id"]):
        return None
    charges = [row for row in entries if row["entry"] == "consume"]
    if any(str(row["task_id"]) != str(task["id"]) for row in charges):
        return None
    if sum(row["amount"] for row in charges) != Decimal(task["budget"]["used"]):
        return None
    own = [row for row in charges if str(row["attempt_id"]) == str(attempt["id"])]
    if len(own) != 1:
        return None
    charge = own[0]
    usage = attempt["refs"].get("instruction_usage")
    spent = attempt["budget_consumed"]
    if (
        not usage
        or charge.get("script_call_id") is not None
        or charge["amount"] != Decimal(usage["amount"])
        or charge["tokens"] != usage["tokens"]
        or spent.get("tokens") != usage["tokens"]
        or spent.get("amount") is None
        or Decimal(spent["amount"]) != charge["amount"]
    ):
        return None
    return charge


async def instruction_evidence(repo, task, attempt, request, outputs):
    refs = attempt["refs"]
    quote = refs.get("instruction_quote") or {}
    claim = refs.get("instruction_request") or {}
    usage = refs.get("instruction_usage") or {}
    gateway = refs.get("instruction_gateway") or {}
    dispatch = refs.get("instruction_thread") or {}
    turn = refs.get("runtime_turn_id")
    session = await repo.get_session(
        str(task["session_id"]), owner_actor_id=str(task["owner_actor_id"])
    )
    if (
        not turn
        or not claim.get("digest")
        or not usage.get("response_id")
        or claim.get("turn_id") != turn
        or usage.get("turn_id") != turn
        or attempt.get("turn_ids") != [turn]
        or not gateway.get("uid")
        or gateway.get("sandbox") != str(session["sandbox_id"])
        or not dispatch.get("worker")
        or not dispatch.get("thread")
        or "instruction_unit" not in refs
        or refs["instruction_unit"].get("protocol_error")
        or refs.get("permission_revocation")
        or refs.get("usage_pending")
        or quote.get("snapshot_digest") != task["pack_digest"]
        or quote.get("caller") != "knowlab"
        or not quote.get("credential_id")
        or quote.get("model") != attempt.get("model")
        or quote.get("provider") != attempt.get("model_provider")
    ):
        return None
    entries = await repo.ledger_list(str(task["id"]))
    charge = instruction_charge(task, attempt, entries)
    # A standalone author trial must account for the entire case itself.
    if (
        charge is None
        or sum(row["entry"] == "consume" for row in entries) != 1
        or Decimal(task["budget"]["reserved"]) != 0
    ):
        return None
    validate_values(request.draft.definition.procedure.outputs, outputs)
    return {
        "source_digest": request.draft.source.fingerprint(),
        "gateway": gateway,
        "thread": dispatch,
        "request": claim,
        "usage": usage,
        "quote": quote,
        "ledger_id": str(charge["id"]),
    }
