"""Audit synthetic ledger facts; these unit vectors grant no real qualification."""

import json
from copy import deepcopy
from types import SimpleNamespace
from unittest.mock import AsyncMock
from uuid import uuid4

import pytest
from test_capabilities_db import OPERATION, OWNER, URL
from test_formal_method_admission_db import validation_request

from app.application.workbench.formal_model_evidence import collect_model_provenance
from app.domain.workbench.capabilities import digest
from app.domain.workbench.formal_execution import FORMAL_RUNTIME, FormalTaskSnapshot
from app.domain.workbench.formal_method_execution_generated import (
    FormalMethodExecutionRequest,
    FormalTestSuiteDraft,
)
from app.domain.workbench.formal_model import (
    PROBE_CONFIG,
    PROBE_ISOLATION,
    PROBE_SERVER,
    PROBE_TOOL,
    model_call_key,
    model_intent_digest,
)


def facts():
    source = dict(
        kind="mcp",
        source=URL,
        version="1",
        digest="sha256:" + OPERATION.descriptor_digest,
        entry="send",
        asset_id=str(uuid4()),
        asset_revision=1,
        license_basis="synthetic-unit-only",
    )
    admission = validation_request(source).model_dump(mode="json", by_alias=True)
    admission["draft"]["allowed_callers"] = ["model"]
    suite = FormalTestSuiteDraft.model_validate(
        {
            "suite_id": str(uuid4()),
            "version": 1,
            "cases": [
                {
                    "case_id": "model",
                    "caller": "model",
                    "input": {
                        "values": {"to": "test@example.test"},
                        "model_prompt": "Invoke candidate",
                    },
                    "repetitions": 1,
                    "minimum_passes": 1,
                    "expected": {
                        "outcome": "succeeded",
                        "assertions": [
                            {"kind": "equals", "pointer": "/receipt", "value": "ok"}
                        ],
                    },
                }
            ],
        }
    ).freeze()
    request = FormalMethodExecutionRequest.model_validate(
        {
            **admission,
            "contract": FORMAL_RUNTIME,
            "execution": {
                "suite": suite.model_dump(mode="json"),
                "runner": {
                    "kind": "user",
                    "environment_id": str(uuid4()),
                    "workspace_root": "/workspace",
                },
                "budget": {"limit": "1", "currency": "CNY"},
                "timeout_seconds": 60,
                "max_wait_seconds": 60,
                "model_probe": {"max_turns": 1, "max_method_calls": 1},
            },
        }
    )
    case = request.execution.suite.cases[0]
    snapshot = FormalTaskSnapshot(
        runtime_contract=FORMAL_RUNTIME,
        run_id=request.run_id,
        case_id=case.case_id,
        repetition=1,
        request_digest=request.fingerprint(),
        method_id=request.method_id,
        draft=request.draft,
        arguments=case.input.values,
        definition_digest=request.draft.definition.digest(),
        caller="model",
        model_prompt=case.input.model_prompt,
        model_probe=request.execution.model_probe,
    )
    task = {
        "id": str(uuid4()),
        "session_id": str(uuid4()),
        "owner_actor_id": OWNER,
        "thread_id": "thread",
        "pack_source": "formal-test",
        "pack_snapshot": snapshot.document(),
        "pack_digest": digest(snapshot.document()),
        "budget": {"limit": "1", "used": "0.1", "reserved": "0"},
    }
    parent = {"id": str(uuid4())}
    attempt = {
        "id": str(uuid4()),
        "turn_ids": ["turn"],
        "refs": {"runtime_turn_id": "turn"},
    }
    tokens = {
        "input": 10,
        "cached_input": 0,
        "cache_write": 0,
        "output": 5,
        "total": 15,
    }
    attempt["budget_consumed"] = {"amount": "0.1", "tokens": tokens}
    context = {
        "task_id": task["id"],
        "attempt_id": attempt["id"],
        "snapshot_digest": task["pack_digest"],
        "slot": "direct",
        "method_id": str(request.method_id),
        "run_id": str(request.run_id),
        "case_id": case.case_id,
        "repetition": 1,
        "source_digest": request.draft.source.fingerprint(),
    }
    invocation = {
        "session_id": task["session_id"],
        "thread_id": "thread",
        "turn_id": "turn",
        "call_id": "model-call",
        "server": PROBE_SERVER,
    }
    invocation["key"] = digest(
        [task["session_id"], "thread", "turn", PROBE_SERVER, "model-call"]
    )
    call = {
        "id": str(uuid4()),
        "kind": "tool",
        "origin": "managed",
        "owner_actor_id": OWNER,
        "session_id": task["session_id"],
        "task_id": task["id"],
        "attempt_id": attempt["id"],
        "parent_call_id": parent["id"],
        "call_key": model_call_key(task["id"]),
        "status": "succeeded",
        "detail": {
            "execution_kind": "formal_probe",
            "formal_probe": context,
            "invocation": invocation,
            "arguments": case.input.values,
            "selection_revision": 1,
            "runtime": "gateway-token-hash",
            "result": {"structuredContent": {"receipt": "ok"}},
        },
    }
    call["digest"] = model_intent_digest(call["detail"])
    schema = {
        "type": "object",
        "properties": {"to": {"type": "string"}},
        "required": ["to"],
        "additionalProperties": False,
    }
    isolation = {
        "version": PROBE_ISOLATION,
        "codex_version": "0.170.0",
        "tools": [PROBE_TOOL],
        "startup_features": {
            "multi_agent": False,
            "multi_agent_v2": False,
            "goals": False,
        },
        "disabled_servers": [],
        "applied_config": dict(PROBE_CONFIG),
        "scope_id": "scope",
        "selection_revision": 1,
        "thread_id": "thread",
        "attempt_id": attempt["id"],
        "snapshot_digest": task["pack_digest"],
        "inventory": [
            {
                "name": PROBE_SERVER,
                "tools": [{"name": PROBE_TOOL, "inputSchema": schema}],
                "resources": [],
                "resourceTemplates": [],
                "runtimeStatus": "connected",
                "toolsError": None,
            }
        ],
    }
    probe = {
        "state": "finished",
        "snapshot_digest": task["pack_digest"],
        "tool_name": PROBE_TOOL,
        "call_id": call["id"],
        "turn_id": "turn",
        "invocation_key": invocation["key"],
        "usage_known": True,
        "cost": "0.1",
        "tokens": tokens,
        "not_started": False,
        "stop_confirmed": True,
        "error": None,
        "isolation": isolation,
    }
    attempt["refs"]["formal_model_probe"] = probe
    selected = {
        "revision": 1,
        "runtime": {
            "scope_id": "scope",
            "token_hash": "gateway-token-hash",
            "turn_id": "turn",
            "status": "completed",
            "aborted": False,
            "formal_probe": {
                "task_id": task["id"],
                "attempt_id": attempt["id"],
                "snapshot_digest": task["pack_digest"],
            },
            "tools": {PROBE_TOOL: {}},
            "skills": [],
            "packages": {},
        },
    }
    item = {
        "type": "mcpToolCall",
        "id": "model-call",
        "server": PROBE_SERVER,
        "tool": PROBE_TOOL,
        "arguments": case.input.values,
        "status": "inProgress",
        "result": None,
        "error": None,
    }
    done = {
        **item,
        "status": "completed",
        "result": {
            "content": [
                {
                    "type": "text",
                    "text": json.dumps(
                        {
                            "call_id": call["id"],
                            "status": "succeeded",
                            "result": call["detail"]["result"],
                        }
                    ),
                }
            ],
            "structuredContent": None,
            "_meta": None,
        },
    }
    events = []
    for index, (kind, payload) in enumerate(
        [
            (
                "turn/started",
                {"threadId": "thread", "turn": {"id": "turn", "status": "inProgress"}},
            ),
            ("item/started", {"threadId": "thread", "turnId": "turn", "item": item}),
            ("item/completed", {"threadId": "thread", "turnId": "turn", "item": done}),
            (
                "thread/tokenUsage/updated",
                {
                    "threadId": "thread",
                    "turnId": "turn",
                    "cost": {
                        "priced": True,
                        "currency": "CNY",
                        "model": "unit-model",
                        "call": "0.1",
                        "turn": "0.1",
                        "call_tokens": tokens,
                        "turn_tokens": tokens,
                    },
                    "tokenUsage": {
                        "last": {
                            "inputTokens": 10,
                            "outputTokens": 5,
                            "totalTokens": 15,
                        },
                        "total": {
                            "inputTokens": 10,
                            "outputTokens": 5,
                            "totalTokens": 15,
                        },
                    },
                },
            ),
            (
                "turn/completed",
                {
                    "threadId": "thread",
                    "turn": {"id": "turn", "status": "completed", "error": None},
                },
            ),
        ]
    ):
        events.append(
            {
                "id": str(uuid4()),
                "cursor": index + 1,
                "type": kind,
                "payload": payload,
                "session_id": task["session_id"],
                "task_id": None,
                "attempt_id": None,
            }
        )
    events.append(
        {
            "id": str(uuid4()),
            "cursor": 0,
            "type": "session/thread_started",
            "payload": {"thread_id": "thread", "model": "unit-model"},
            "session_id": task["session_id"],
        }
    )
    ledger = [
        {
            "id": str(uuid4()),
            "entry": "consume",
            "attempt_id": attempt["id"],
            "amount": "0.1",
            "tokens": tokens,
        }
    ]
    result = {
        "caller": "model",
        "model_trace": {
            "thread_id": "thread",
            "turn_id": "turn",
            "tool_call_id": "model-call",
            "method_call_id": parent["id"],
        },
    }
    repo = SimpleNamespace(
        list_record_events=AsyncMock(return_value=events),
        capability_selection=AsyncMock(return_value=selected),
        ledger_list=AsyncMock(return_value=ledger),
    )
    return SimpleNamespace(
        repo=repo,
        request=request,
        case=case,
        task=task,
        attempt=attempt,
        call=call,
        parent=parent,
        result=result,
        events=events,
        ledger=ledger,
        selected=selected,
        probe=probe,
        isolation=isolation,
    )


async def collect(world):
    return await collect_model_provenance(
        world.repo,
        world.request,
        world.case,
        world.task,
        world.attempt,
        world.call,
        world.parent,
        world.result,
    )


@pytest.mark.parametrize("version", ["0.155.1", "0.170.0"])
async def test_previous_probe_profile_only_qualifies_its_recorded_version(version):
    world = facts()
    world.isolation.update(
        version=f"codex-{version}/formal-probe-v1", codex_version=version
    )
    del world.isolation["applied_config"]["features.view_image"]
    proof = await collect(world)
    assert proof["validated"] is (version == "0.155.1")


async def test_completed_provenance_reconciles_events_budget_and_isolation_without_secrets():
    world = facts()
    proof = await collect(world)
    assert proof["validated"], proof["errors"]
    assert proof["cost"] == "0.1" and proof["tokens"]["total"] == 15
    assert len(proof["turn_events"]) == len(proof["call_events"]) == 2
    assert "gateway-token-hash" not in json.dumps(proof)
    assert "Invoke candidate" not in json.dumps(proof)
    world.repo.list_record_events.assert_awaited_once()


@pytest.mark.parametrize(
    "mutation",
    [
        "trace",
        "turns",
        "unknown",
        "pending_usage",
        "scope",
        "thread",
        "source",
        "arguments",
        "missing_event",
        "wrong_event_args",
        "other_tool",
        "duplicate_call",
        "extra_turn",
        "bad_return",
        "failed_turn",
        "no_usage",
        "unpriced",
        "cost",
        "tokens",
        "unsettled",
        "budget_limit",
        "ledger",
        "feature",
        "version",
        "inventory",
        "schema",
        "resources",
        "config",
        "native_tool",
        "unknown_item",
        "raw_usage",
        "model_identity",
    ],
)
async def test_missing_or_changed_model_facts_never_qualify(mutation):
    w = facts()
    if mutation == "trace":
        w.result["model_trace"]["turn_id"] = "other"
    elif mutation == "turns":
        w.attempt["turn_ids"].append("other")
    elif mutation == "unknown":
        w.probe["state"] = "unknown"
    elif mutation == "pending_usage":
        w.attempt["refs"]["usage_pending"] = {"turn_id": "turn"}
    elif mutation == "scope":
        w.selected["runtime"]["scope_id"] = "other"
    elif mutation == "thread":
        w.task["thread_id"] = "other"
    elif mutation == "source":
        w.call["detail"]["formal_probe"]["source_digest"] = "changed"
    elif mutation == "arguments":
        w.call["detail"]["arguments"] = {"to": "other"}
    elif mutation == "missing_event":
        w.events.pop(2)
    elif mutation == "wrong_event_args":
        w.events[1]["payload"]["item"]["arguments"] = {}
    elif mutation == "other_tool":
        w.events[1]["payload"]["item"]["tool"] = "raw_mcp"
    elif mutation == "duplicate_call":
        w.events.append(deepcopy(w.events[1]))
    elif mutation == "extra_turn":
        event = deepcopy(w.events[0])
        event["payload"]["turn"]["id"] = "other"
        w.events.append(event)
    elif mutation == "bad_return":
        w.events[2]["payload"]["item"]["result"]["content"][0]["text"] = "{}"
    elif mutation == "failed_turn":
        w.events[4]["payload"]["turn"]["status"] = "failed"
    elif mutation == "no_usage":
        w.events.pop(3)
    elif mutation == "unpriced":
        w.events[3]["payload"]["cost"]["priced"] = False
    elif mutation == "cost":
        w.events[3]["payload"]["cost"]["call"] = "0.2"
    elif mutation == "tokens":
        w.probe["tokens"] = {
            "input": 10,
            "cached_input": 0,
            "cache_write": 0,
            "output": 5,
            "total": 10,
        }
    elif mutation == "unsettled":
        w.task["budget"]["reserved"] = "0.1"
    elif mutation == "budget_limit":
        w.task["budget"]["limit"] = "0.1"
    elif mutation == "ledger":
        w.ledger.clear()
    elif mutation == "feature":
        w.isolation["startup_features"]["multi_agent"] = True
    elif mutation == "version":
        w.isolation["codex_version"] = "0.156.0"
    elif mutation == "inventory":
        w.isolation["inventory"].append(
            {
                "name": "other",
                "tools": [{"name": "x"}],
                "resources": [],
                "resourceTemplates": [],
            }
        )
    elif mutation == "schema":
        w.isolation["inventory"][0]["tools"][0]["inputSchema"][
            "additionalProperties"
        ] = True
    elif mutation == "resources":
        w.isolation["inventory"][0]["resources"] = [{"uri": "secret"}]
    elif mutation == "config":
        w.isolation["applied_config"]["features.shell_tool"] = True
    elif mutation == "raw_usage":
        w.events[3]["payload"]["tokenUsage"]["last"]["inputTokens"] = 11
    elif mutation == "model_identity":
        w.events[-1]["payload"]["model"] = "another-model"
    else:
        event = deepcopy(w.events[1])
        event["payload"]["item"] = {
            "type": "unrecognizedItem"
            if mutation == "unknown_item"
            else "commandExecution"
        }
        w.events.append(event)
    proof = await collect(w)
    assert not proof["validated"] and proof["errors"], mutation


async def test_approval_denial_can_finish_the_model_turn_without_successful_tool_output():
    world = facts()
    world.call["status"] = "declined"
    world.call["detail"].pop("result")
    world.events[2]["payload"]["item"].update(
        status="failed", result=None, error={"message": "Operation denied"}
    )
    proof = await collect(world)
    assert proof["validated"], proof["errors"]
    # Approval truth is verified by collect_case_evidence, not this provenance helper.


async def test_usage_after_completion_matches_catalog_and_collector():
    from app.application.workbench.formal_method_catalog import _verify_model_case
    from app.domain.workbench.formal_execution import snapshot_for_task

    world = facts()
    usage = next(
        event for event in world.events if event["type"] == "thread/tokenUsage/updated"
    )
    usage["cursor"] = max(event["cursor"] for event in world.events) + 1
    world.events.sort(key=lambda event: event["cursor"])
    proof = await collect(world)
    assert proof["validated"], proof["errors"]
    item = {
        "model_provenance": proof,
        "model_turn_ids": ["turn"],
        "snapshot_digest": world.task["pack_digest"],
    }
    result = SimpleNamespace(
        attempt_id=world.attempt["id"],
        model_trace=SimpleNamespace(**world.result["model_trace"]),
    )
    _verify_model_case(
        world.request, snapshot_for_task(world.task), item, result, set()
    )
