"""PR-3 regression suite: Five-Element guided HTN, SEALED_INPUT budget guard (W03/#121),
MIN_HTN_TURN_SECONDS & zero-token stale_edit (W05/W06/#122), and bounded directory paging (W01).
"""

from __future__ import annotations

from copy import deepcopy
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from types import SimpleNamespace
from uuid import UUID, uuid4

import pytest
from test_hierarchical_planning import decision, route, setup

from app.application.workbench.formal_handoff import Handoff
from app.application.workbench.registry_catalog import SceneRegistry
from app.application.workbench.task_runtime import TaskRuntime
from app.domain.workbench.expert_lookup import LookupGuard
from app.domain.workbench.formal_method_generated import MethodRef
from app.domain.workbench.hierarchical_planning import (
    MIN_HTN_TURN_SECONDS,
    PROTOCOL,
    HierarchicalPlanner,
)
from app.domain.workbench.incubation_tools import IncubationGrant, IncubationGrants
from app.domain.workbench.method_search import PlanningQuery


def test_five_element_guidance_pre_discloses_top_k_and_preserves_shown_on_backtrack():
    task, _ = setup(author=True)
    ctx = task["original_input"]["input"]["incubation"]["selection_context"]
    base = deepcopy(ctx["methods"]["a"])
    out_port = next(iter(base["outputs"]))
    for idx in range(10):
        entry = deepcopy(base)
        entry["outputs"][out_port]["meaning"] = (
            {"domain_id": "table.normalized_rows"}
            if idx == 7
            else {"domain_id": "text.unrelated"}
        )
        ctx["methods"][f"cand_{idx}"] = entry

    # Unguided legacy mode: contracts empty on Turn 1.
    unguided = HierarchicalPlanner(deepcopy(task))
    assert not unguided.prompt()["contracts"]

    # Five-Element guided mode: Turn-1 Top-K pre-discloses direct_deliverable candidate.
    ctx["target_deliverables"] = [
        {
            "promise_id": "p_main",
            "summary": "标准化行集",
            "expected_domain_id": "table.normalized_rows",
        }
    ]
    ctx["allowed_capabilities"] = ["kb_read"]
    guided = HierarchicalPlanner(task)
    prompt = guided.prompt()
    assert "cand_7" in prompt["contracts"]
    assert (
        prompt["authorized_index"]["cand_7"]["reachability"] == "direct_deliverable"
    )
    assert prompt["five_element_guidance"]["allowed_capabilities"] == ["kb_read"]

    # Backtracking preserves shown contracts and records tabu_signatures.
    bt_task, _ = setup(author=True)
    bt_ctx = bt_task["original_input"]["input"]["incubation"]["selection_context"]
    for idx in range(4):
        bt_ctx["methods"][f"extra_{idx}"] = deepcopy(base)
    p = HierarchicalPlanner(bt_task)
    p.methods["first"]["outputs"]["receipt"]["meaning"] = {"unit": "cents"}
    leaf = route(p)
    shown_before = list(p.shown)
    assert shown_before
    assert p.advance(leaf) is None
    assert p.current.id == "n1"
    assert p.shown == shown_before
    assert "first" in p.current.tabu_signatures
    assert p.prompt()["tabu_methods"] == ["first"]


def test_domain_id_covariance_and_projection_lens_in_htn_contract_issue():
    task, _ = setup(author=True)
    p = HierarchicalPlanner(task)
    # Parent expects object {val: number} with domain_id "table.rows";
    # child produces wider object {val: number, extra: string} with narrower subtype
    # "table.rows.normalized", projected via ProjectionLens(pick_fields=["val"]).
    target_obj_schema = {
        "type": "object",
        "additionalProperties": False,
        "properties": {"val": {"type": "number"}},
        "required": ["val"],
    }
    p.methods["a"]["inputs"]["value"]["schema"] = deepcopy(target_obj_schema)
    p.methods["b"]["inputs"]["value"]["schema"] = deepcopy(target_obj_schema)
    p.methods["a"]["inputs"]["value"]["meaning"] = {"domain_id": "table.rows"}
    p.methods["b"]["inputs"]["value"]["meaning"] = {"domain_id": "table.rows"}
    p.methods["first"]["outputs"]["receipt"]["meaning"] = {
        "domain_id": "table.rows.normalized"
    }
    p.methods["first"]["outputs"]["receipt"]["schema"] = {
        "type": "object",
        "additionalProperties": False,
        "properties": {"val": {"type": "number"}, "extra": {"type": "string"}},
        "required": ["val", "extra"],
    }
    p.advance({"action": "inspect", "methods": ["a", "b", "first"]})
    first_step = {
        "id": "n1_1",
        "task": "摘取",
        "method": "first",
        "inputs": {
            k: {"source": "caller", "name": k}
            for k, slot in p.methods["first"]["inputs"].items()
            if slot["required"]
        },
    }
    branch_step = {
        "id": "n1",
        "task": "分派",
        "method": "a",
        "route_input": "kind",
        "cases": {"A": "a", "B": "b"},
        "inputs": {
            "kind": {"source": "caller", "name": "label"},
            "value": {
                "source": "step",
                "step": "prepare",
                "output": "receipt",
                "lens": {"pick_fields": ["val"]},
            },
        },
    }
    assert (
        p.advance(decision(branch_step, "branch", children={"prepare": "摘取原表"}))
        is None
    )
    out = p.advance(decision(first_step))
    assert out is not None
    assert out["selection"]["status"] == "ready"


def test_lookup_guard_interrupts_incubate_on_streaming_budget_and_tool_detour():
    guard = LookupGuard(Decimal("0.05"))
    guard.usage("0.03")
    assert guard.failure is None
    guard.usage("0.05")
    assert guard.failure == "budget_limit"

    tool_guard = LookupGuard(Decimal("1.00"))
    tool_guard.observe({"type": "commandExecution"})
    assert tool_guard.failure == "unexpected_tool"


@pytest.mark.asyncio
async def test_stale_edit_zero_token_short_circuit_and_min_htn_turn_timeout():
    assert MIN_HTN_TURN_SECONDS == 25.0
    task, _ = setup(author=True)
    task["original_input"]["input"]["planner"] = PROTOCOL
    p = HierarchicalPlanner(task)
    artifact = p.advance(route(p))["selection"]
    ctx = task["original_input"]["input"]["incubation"]
    ctx["selection_context"]["previous"] = artifact
    ctx["instruction"] = "[n1_1] 修改提取范围"
    # Mutate retained sibling method to trigger stale_edit.
    ctx["selection_context"]["methods"]["b"]["declaration"]["title"] = "已改版"

    stale_planner = HierarchicalPlanner(task)
    assert stale_planner.stale_edit is True

    class NeverDriver:
        async def run_turn(self, *args, **kwargs):
            raise AssertionError("stale_edit must short-circuit before LLM turn")

    put_calls: list[dict] = []
    events: list[dict] = []
    state = {
        "id": str(uuid4()),
        "session_id": str(uuid4()),
        "owner_actor_id": "owner-1",
        "profile_id": "INCUBATE",
        "profile_version": "2",
        "state": "RUNNING",
        "state_version": 1,
        "current_step": 0,
        "original_input": task["original_input"],
        "budget": {"limit": "10", "used": "0"},
        "cancel_requested_at": None,
    }

    class FakeTx:
        async def __aenter__(self):
            return self

        async def __aexit__(self, *args):
            return False

    class FakeRepo:
        def transaction(self):
            return FakeTx()

        async def get_task(self, tid, **kw):
            return dict(state)

        async def get_task_resolution(self, tid, **kw):
            return None

        async def list_task_data(self, tid):
            return []

        async def list_attempts(self, tid):
            return []

        async def latest_task_payload(self, tid, event_type):
            return None

        async def put_artifact(self, **kw):
            art = {"id": str(uuid4()), "version": 1, **kw}
            put_calls.append(art)
            return art

        async def cas_task(self, tid, *, expected_version, **updates):
            assert expected_version == state["state_version"]
            state.update(updates)
            state["state_version"] += 1
            return dict(state)

        async def append_event(self, **kw):
            events.append(kw)
            return kw

        async def commit(self):
            return None

    class Stores:
        def __call__(self):
            repo = FakeRepo()

            class Ctx:
                async def __aenter__(self):
                    return repo

                async def __aexit__(self, *args):
                    return False

            return Ctx()

    runtime = TaskRuntime(Stores(), NeverDriver())
    timeout_state = deepcopy(state)
    timeout_state["profile_id"] = "FIND_EXPERT"
    timeout_state["profile_version"] = "7"
    fe_task, _ = setup(author=False)
    fe_task["original_input"]["input"]["planner"] = PROTOCOL
    timeout_state["original_input"] = fe_task["original_input"]
    created_130s_ago = datetime.now(UTC) - timedelta(seconds=130)

    transitions: list[tuple] = []

    class TimeoutRepo(FakeRepo):
        async def get_task(self, tid, **kw):
            return dict(timeout_state)

        async def list_attempts(self, tid):
            return [
                {
                    "id": str(uuid4()),
                    "step_id": "find",
                    "status": "COMPLETED",
                    "failure_code": None,
                    "created_at": created_130s_ago,
                    "refs": {"planning_turn": 0},
                }
            ]

    from app.application.workbench import task_runtime as tr_mod

    orig_ledger = tr_mod.Ledger

    class StubLedger:
        def __init__(self, repo):
            self.repo = repo

        async def transition(self, tid, new_state, **kw):
            transitions.append((new_state, kw))
            state["state"] = new_state
            timeout_state["state"] = new_state
            return {"state": new_state}

    tr_mod.Ledger = StubLedger
    try:
        out_stale = await runtime._execute_next(state["id"])
        assert out_stale == "QUEUED"
        assert len(put_calls) == 1
        assert put_calls[0]["content"]["selection"]["status"] == "clarify"
        assert events[-1]["payload"]["short_circuit"] == "stale_edit"

        class TimeoutStores:
            def __call__(self):
                repo = TimeoutRepo()

                class Ctx:
                    async def __aenter__(self):
                        return repo

                    async def __aexit__(self, *args):
                        return False

                return Ctx()

        out_timeout = await TaskRuntime(TimeoutStores(), NeverDriver())._execute_next(
            timeout_state["id"]
        )
        assert out_timeout == "FAILED"
        assert transitions[-1][1]["reason"] == {"code": "selection_planning_timeout"}
    finally:
        tr_mod.Ledger = orig_ledger


@pytest.mark.asyncio
async def test_planning_versions_skips_full_market_scan_on_topics_and_caps_shared_tools():
    windows = 0
    inc_pages = 0

    class Market:
        async def planning_window(self, subject, offset, limit):
            nonlocal windows
            windows += 1
            return [
                MethodRef(
                    method_id=UUID(int=offset + i + 1),
                    version=1,
                    digest="sha256:" + "a" * 64,
                )
                for i in range(limit)
            ], True

        async def planning_formal_refs(self, subject, refs):
            return {MethodRef.model_validate(r) for r in refs}

        async def directory_labels(self, subject, refs):
            from app.domain.workbench.method_search import DirectoryLabels

            return DirectoryLabels.model_validate(
                {
                    "subject": subject,
                    "categories": {
                        "tool": [
                            dict(
                                group="data",
                                group_name="数据",
                                code="data.check",
                                name="核对",
                            )
                        ],
                        "expert": [],
                    },
                    "items": [
                        {"reference": refs[0], "kind": "tool", "category": "data.check"}
                    ]
                    if refs
                    else [],
                }
            )

        async def incubation_tools(
            self, subject, *, reference=None, after=None, limit=30, topic_terms=None
        ):
            nonlocal inc_pages
            inc_pages += 1
            cur = (UUID(after).int if after else 0) + 1
            return IncubationGrants(
                subject=subject,
                items=(
                    IncubationGrant(
                        owner_subject="other",
                        listing_id=uuid4(),
                        reference=MethodRef(
                            method_id=UUID(int=cur),
                            version=1,
                            digest="sha256:" + "b" * 64,
                        ),
                        scope="acquired",
                    ),
                ),
                next=UUID(int=cur) if cur < 20 else None,
            )

    asset_queries = 0

    class FakeRepo:
        def transaction(self, independent=False):
            class Tx:
                async def __aenter__(self):
                    return self

                async def __aexit__(self, *args):
                    return False

            return Tx()

        async def subject_of_actor(self, owner):
            return "subj-1"

        async def actor_for_subject(self, subject):
            return "owner-1"

        async def capability_assets(self, owner):
            nonlocal asset_queries
            asset_queries += 1
            return [{"id": uuid4(), "name": "conn", "detail": {}}]

        async def registry_method_candidates(self, owner, limit, offset, **kw):
            if kw.get("matching"):
                return [
                    {
                        "method_id": UUID(int=i + 1),
                        "latest_version": 1,
                        "digest": "sha256:" + "a" * 64,
                    }
                    for i in range(10)
                ]
            return []

    source_stub = SimpleNamespace(
        kind="mcp",
        digest="sha256:" + "c" * 64,
        license_basis="platform",
        asset_id=uuid4(),
        asset_revision=1,
    )
    methods_stub = SimpleNamespace(
        market=Market(),
        operations=(),
        now=lambda: datetime.now(UTC),
        _sources=lambda asset, subject, now: [source_stub],
    )
    registry = SceneRegistry(methods_stub, FakeRepo())
    page = await registry.planning_versions(
        "owner-1", 5, 0, query=PlanningQuery(stage="composition", question="核对明细")
    )
    assert len(page["items"]) == 5
    assert windows == 1

    shared = await registry.shared_tools("subj-1", goal="核对明细")
    assert inc_pages == 5
    assert shared["partial"] is True
    assert asset_queries == 1


def test_handoff_command_accepts_v2_htn_tree_and_draft_contract():
    cmd = Handoff(
        review_digest="sha256:" + "1" * 64,
        gap="缺少标准化表转换工具",
        htn_tree={"catalog_digest": "2" * 64, "nodes": []},
        draft_contract={"contract": "knowlab/method-draft/v2"},
    )
    dumped = cmd.model_dump(mode="json")
    assert dumped["htn_tree"]["catalog_digest"] == "2" * 64
