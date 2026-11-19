"""Audit completed model probes against retained runner and budget facts.

Live invocation lookup deliberately excludes completed turns. This collector uses
the historical event ledger without weakening that online authorization boundary.
Only safe projections and event digests are retained, never gateway credentials.
"""

import json
from decimal import Decimal, InvalidOperation

from app.domain.workbench.capabilities import digest
from app.domain.workbench.errors import WorkbenchError
from app.domain.workbench.formal_execution import direct_procedure, snapshot_for_task
from app.domain.workbench.formal_model import (
    PROBE_SERVER,
    PROBE_TOOL,
    validate_model_call,
    verified_probe_profile,
)
from app.domain.workbench.pricing import cumulative_of, fresh_usage
from app.domain.workbench.skill_execution import public_result

EVENTS = (
    "turn/started",
    "turn/completed",
    "item/started",
    "item/completed",
    "thread/tokenUsage/updated",
    "error",
    "item/tool/requestUserInput",
    "session/thread_started",
)
TOKEN_KEYS = ("input", "cached_input", "cache_write", "output")


def _tokens(value):
    if (
        not isinstance(value, dict)
        or set(value) != {*TOKEN_KEYS, "total"}
        or any(
            type(value[k]) is not int or value[k] < 0 for k in (*TOKEN_KEYS, "total")
        )
        or value["total"] != sum(value[k] for k in TOKEN_KEYS)
    ):
        raise ValueError("Invalid retained token accounting")
    return value


def _amount(value):
    amount = Decimal(str(value))
    if not amount.is_finite() or amount < 0:
        raise ValueError("Invalid retained model price")
    return amount


def _reported_usage(value):
    if not isinstance(value, dict):
        raise ValueError("Missing actual token usage report")
    for key in ("last", "total"):
        row = value.get(key)
        if not isinstance(row, dict) or any(
            type(row.get(k)) is not int or row[k] < 0
            for k in ("inputTokens", "outputTokens", "totalTokens")
        ):
            raise ValueError("Incomplete actual token usage report")
        if row["totalTokens"] != row["inputTokens"] + row["outputTokens"]:
            raise ValueError("Actual token total is inconsistent")
        extras = [row.get(k, 0) for k in ("cachedInputTokens", "cacheWriteInputTokens")]
        if (
            any(type(v) is not int or v < 0 for v in extras)
            or sum(extras) > row["inputTokens"]
        ):
            raise ValueError("Actual input token split is inconsistent")
    return value


def _turn(payload):
    return payload.get("turnId") or (payload.get("turn") or {}).get("id")


def _event_ref(event):
    return {
        "id": str(event["id"]),
        "cursor": event["cursor"],
        "type": event["type"],
        "payload_digest": digest(event["payload"]),
    }


def _isolation(value, task, attempt, call, snapshot):
    procedure = direct_procedure(snapshot.draft, caller="model")
    schema = {
        "type": "object",
        "properties": {name: slot.schema_ for name, slot in procedure.inputs.items()},
        "required": [
            name
            for name, slot in procedure.inputs.items()
            if slot.required or slot.on_missing != "omit"
        ],
        "additionalProperties": False,
    }
    if (
        not isinstance(value, dict)
        or not verified_probe_profile(value)
        or value.get("tools") != [PROBE_TOOL]
        or value.get("startup_features")
        != {"multi_agent": False, "multi_agent_v2": False, "goals": False}
        or value.get("thread_id") != task["thread_id"]
        or value.get("attempt_id") != str(attempt["id"])
        or value.get("snapshot_digest") != task["pack_digest"]
        or not isinstance(value.get("scope_id"), str)
        or not value["scope_id"]
        or value.get("selection_revision") != call["detail"].get("selection_revision")
        or not isinstance(value.get("disabled_servers"), list)
        or any(
            not isinstance(s, str) or s == PROBE_SERVER
            for s in value["disabled_servers"]
        )
    ):
        raise ValueError("Missing model isolation identity")
    inventory = value.get("inventory")
    if not isinstance(inventory, list) or not inventory:
        raise ValueError("Missing actual tool inventory")
    names = set()
    for server in inventory:
        name = server["name"]
        if not isinstance(name, str) or not name or name in names:
            raise ValueError("Invalid tool inventory identity")
        names.add(name)
        if server.get("resources") != [] or server.get("resourceTemplates") != []:
            raise ValueError("Unexpected model resources")
        if name == PROBE_SERVER:
            if (
                server.get("tools") != [{"name": PROBE_TOOL, "inputSchema": schema}]
                or server.get("toolsError")
                or server.get("runtimeStatus") not in (None, "connected")
            ):
                raise ValueError("Candidate tool differs from the frozen interface")
        elif server.get("tools") != [] or name not in value["disabled_servers"]:
            raise ValueError("Another model tool remains available")
    if PROBE_SERVER not in names:
        raise ValueError("Candidate was not exposed")
    # Enumerate the retained projection instead of copying arbitrary future keys.
    return {
        key: value[key]
        for key in (
            "version",
            "codex_version",
            "tools",
            "startup_features",
            "disabled_servers",
            "inventory",
            "applied_config",
            "scope_id",
            "selection_revision",
            "thread_id",
            "attempt_id",
            "snapshot_digest",
        )
    }


async def collect_model_provenance(
    repo, request, case, task, attempt, call, parent, result
):
    """Return provenance/errors; the common collector separately checks governance."""
    evidence = {"validated": False, "errors": []}
    errors = evidence["errors"]

    def require(condition, code):
        if not condition:
            errors.append(code)

    try:
        validate_model_call(task, attempt, call)
        snapshot = snapshot_for_task(task)
        refs = attempt.get("refs") or {}
        probe = refs.get("formal_model_probe") or {}
        invocation = call["detail"]["invocation"]
        thread, turn, tool_call = (
            invocation[k] for k in ("thread_id", "turn_id", "call_id")
        )
        require(
            case.caller == snapshot.caller == result.get("caller") == "model"
            and snapshot.model_prompt == case.input.model_prompt
            and snapshot.model_probe == request.execution.model_probe
            and attempt.get("turn_ids") == [turn]
            and refs.get("runtime_turn_id") == probe.get("turn_id") == turn
            and probe.get("snapshot_digest") == task["pack_digest"]
            and probe.get("tool_name") == PROBE_TOOL
            and probe.get("call_id") == str(call["id"])
            and probe.get("state") == "finished"
            and probe.get("usage_known") is True
            and probe.get("not_started") is False
            and probe.get("stop_confirmed") is True
            and not probe.get("error")
            and not refs.get("usage_pending"),
            "model_turn_or_completion_unproved",
        )
        require(
            result.get("model_trace")
            == {
                "thread_id": thread,
                "turn_id": turn,
                "tool_call_id": tool_call,
                "method_call_id": str(parent["id"]),
            },
            "model_trace_mismatch",
        )
        isolation = _isolation(probe.get("isolation"), task, attempt, call, snapshot)
        selected = await repo.capability_selection(
            str(task["owner_actor_id"]), str(task["session_id"])
        )
        runtime = selected.get("runtime") or {}
        require(
            selected.get("revision") == isolation["selection_revision"]
            and runtime.get("scope_id") == isolation["scope_id"]
            and runtime.get("token_hash") == call["detail"].get("runtime")
            and runtime.get("turn_id") == turn
            and runtime.get("status") == "completed"
            and runtime.get("aborted") is False
            and runtime.get("formal_probe")
            == {
                "task_id": str(task["id"]),
                "attempt_id": str(attempt["id"]),
                "snapshot_digest": task["pack_digest"],
            }
            and set(runtime.get("tools", {})) == {PROBE_TOOL}
            and not runtime.get("packages")
            and not runtime.get("skills"),
            "model_scope_mismatch",
        )
        events = await repo.list_record_events(
            session_id=str(task["session_id"]), types=EVENTS
        )
        model_records = [
            e
            for e in events
            if e["type"] == "session/thread_started"
            and e["payload"].get("thread_id") == thread
        ]
        relevant = []
        for event in events:
            payload = event["payload"]
            if _turn(payload) != turn:
                # A dedicated probe session is not allowed an extra model turn.
                if (
                    event["type"] in ("turn/started", "turn/completed")
                    and payload.get("threadId") == thread
                ):
                    errors.append("extra_model_turn")
                continue
            require(
                str(event["session_id"]) == str(task["session_id"])
                and payload.get("threadId") == thread
                and event.get("task_id") in (None, str(task["id"]))
                and event.get("attempt_id") in (None, str(attempt["id"])),
                "model_event_identity_mismatch",
            )
            relevant.append(event)
        starts = [e for e in relevant if e["type"] == "turn/started"]
        finishes = [e for e in relevant if e["type"] == "turn/completed"]
        require(len(starts) == len(finishes) == 1, "model_turn_events_missing")
        if len(starts) != 1 or len(finishes) != 1:
            return evidence
        require(
            (finishes[0]["payload"].get("turn") or {}).get("status") == "completed",
            "model_turn_not_completed",
        )
        require(
            not (finishes[0]["payload"].get("turn") or {}).get("error"),
            "model_turn_error",
        )
        started, finished = starts[0]["cursor"], finishes[0]["cursor"]
        require(started < finished, "model_turn_event_order")
        calls = {"item/started": [], "item/completed": []}
        usage = []
        for event in relevant:
            kind, payload = event["type"], event["payload"]
            if kind in calls:
                item = payload.get("item") or {}
                require(started < event["cursor"] < finished, "model_item_outside_turn")
                if item.get("type") in (
                    "commandExecution",
                    "webSearch",
                    "fileChange",
                    "collabAgentToolCall",
                    "dynamicToolCall",
                ):
                    errors.append("unexpected_model_tool")
                if item.get("type") != "mcpToolCall":
                    continue
                require(
                    item.get("id") == tool_call
                    and item.get("server") == PROBE_SERVER
                    and item.get("tool") == PROBE_TOOL
                    and digest(item.get("arguments")) == digest(snapshot.arguments),
                    "model_tool_event_mismatch",
                )
                calls[kind].append(event)
            elif kind == "thread/tokenUsage/updated":
                usage.append(event)
            elif (
                kind == "item/tool/requestUserInput"
                or kind == "error"
                and not payload.get("willRetry")
            ):
                errors.append("model_turn_error_or_question")
        require(
            len(calls["item/started"]) == len(calls["item/completed"]) == 1,
            "model_tool_call_count",
        )
        if len(calls["item/started"]) != 1 or len(calls["item/completed"]) != 1:
            return evidence
        begun, ended = calls["item/started"][0], calls["item/completed"][0]
        require(begun["cursor"] < ended["cursor"], "model_tool_event_order")
        item = ended["payload"]["item"]
        returned = item.get("result")
        delivered = None
        if isinstance(returned, dict):
            content = returned.get("content") or []
            require(
                len(content) == 1 and content[0].get("type") == "text",
                "model_tool_result_missing",
            )
            delivered = json.loads(content[0]["text"]) if len(content) == 1 else None
            require(
                delivered
                == {
                    "call_id": str(call["id"]),
                    "status": call["status"],
                    "result": public_result(
                        call["detail"].get("result"), str(call["id"])
                    ),
                },
                "model_delivered_result_mismatch",
            )
            require(
                item.get("status") == "completed" and item.get("error") is None,
                "model_tool_not_completed",
            )
        else:
            # Codex 0.155.1 raises MCP isError to item.error, not item.result.
            # The common collector still requires the actual declined/failed
            # operation and approval facts; an arbitrary native denial cannot pass.
            require(
                call["status"] in ("declined", "expired", "failed")
                and item.get("status") == "failed"
                and isinstance((item.get("error") or {}).get("message"), str)
                and bool(item["error"]["message"]),
                "model_tool_result_missing",
            )

        require(bool(usage), "model_usage_events_missing")
        cost, totals, models, usage_refs = (
            Decimal("0"),
            dict.fromkeys((*TOKEN_KEYS, "total"), 0),
            set(),
            [],
        )
        previous_usage = None
        for event in usage:
            price = event["payload"].get("cost") or {}
            require(
                price.get("priced") is True
                and price.get("currency") == request.execution.budget.currency
                and isinstance(price.get("model"), str)
                and bool(price["model"]),
                "model_price_unverified",
            )
            models.add(price.get("model"))
            call_tokens, turn_tokens = (
                _tokens(price.get("call_tokens")),
                _tokens(price.get("turn_tokens")),
            )
            reported = _reported_usage(event["payload"].get("tokenUsage"))
            observed_usage = fresh_usage(reported, previous_usage)
            cumulative = cumulative_of(reported)
            require(observed_usage.as_json() == call_tokens, "model_raw_usage_mismatch")
            if previous_usage is None:
                require(
                    cumulative is not None and cumulative.as_json() == call_tokens,
                    "model_prior_usage_unaccounted",
                )
            previous_usage = cumulative
            for key in totals:
                totals[key] += call_tokens[key]
            cost += _amount(price.get("call"))
            require(
                _amount(price.get("turn")) == cost and turn_tokens == totals,
                "model_usage_accumulation_mismatch",
            )
            usage_refs.append(
                {
                    **_event_ref(event),
                    "cost": {
                        key: price[key]
                        for key in (
                            "priced",
                            "currency",
                            "model",
                            "call",
                            "turn",
                            "call_tokens",
                            "turn_tokens",
                        )
                    },
                }
            )
        require(
            len(models) == 1
            and cost == _amount(probe.get("cost"))
            and totals == _tokens(probe.get("tokens")),
            "model_probe_usage_mismatch",
        )
        require(
            bool(model_records)
            and all(e["payload"].get("model") in models for e in model_records),
            "model_identity_unproved",
        )
        consumed = attempt.get("budget_consumed") or {}
        budget = task["budget"]
        require(
            cost == _amount(consumed.get("amount", "0")) == _amount(budget.get("used"))
            and cost < _amount(budget.get("limit"))
            and _amount(budget.get("reserved")) == 0
            and (not cost or _tokens(consumed.get("tokens")) == totals),
            "model_budget_unsettled",
        )
        ledger = await repo.ledger_list(str(task["id"]))
        consumed_rows = [
            r
            for r in ledger
            if r["entry"] == "consume"
            and str(r.get("attempt_id")) == str(attempt["id"])
        ]
        require(
            sum((_amount(r["amount"]) for r in consumed_rows), Decimal("0")) == cost,
            "model_cost_ledger_mismatch",
        )
        evidence.update(
            invocation={
                key: invocation[key]
                for key in (
                    "key",
                    "session_id",
                    "thread_id",
                    "turn_id",
                    "call_id",
                    "server",
                )
            },
            isolation=isolation,
            turn_events=[_event_ref(starts[0]), _event_ref(finishes[0])],
            model_events=[_event_ref(e) for e in model_records],
            call_events=[_event_ref(begun), _event_ref(ended)],
            result_digest=digest(delivered),
            usage=usage_refs,
            cost=str(cost),
            tokens=totals,
            budget_consumed={"amount": str(cost), "tokens": totals},
            ledger=[
                {"id": str(r["id"]), "amount": str(r["amount"])} for r in consumed_rows
            ],
        )
        evidence["validated"] = not errors
    except (
        WorkbenchError,
        ValueError,
        TypeError,
        KeyError,
        IndexError,
        AttributeError,
        InvalidOperation,
    ):
        errors.append("missing_or_invalid_model_provenance")
    return evidence
