"""PR-4 regression suite: FrontierState (SKIPPED_BY_GUARD + ProjectionLens + CollectPack),
V0 runtime gate (evaluate_v0_checks), EnvelopeAuthorization, multi-account slot_sources,
and formal_expert_intake batch review & graceful candidate degradation (W07).
"""

from __future__ import annotations

from copy import deepcopy
from types import SimpleNamespace
from uuid import uuid4

import pytest
from test_formal_orchestration_admission import shape

from app.application.workbench.formal_expert_intake import confirm, review
from app.application.workbench.formal_host_calls import FormalHostCalls, HostContext
from app.application.workbench.ordinary_material import declared_material_slots
from app.application.workbench.registry_catalog import SceneRegistry
from app.domain.workbench.errors import NotFound
from app.domain.workbench.formal_execution import (
    execution_steps,
    orchestration_plan,
)
from app.domain.workbench.formal_method_admission_generated import (
    FormalMethodDraft,
    FormalMethodSource,
)
from app.domain.workbench.formal_method_generated import (
    ApprovalRule,
    FormalMethodDefinition,
    MethodRef,
)
from app.domain.workbench.formal_method_plan_generated import (
    bind_step_inputs,
    evaluate_artifact_guard,
    evaluate_v0_checks,
    export_outputs,
)
from app.domain.workbench.ordinary_expert import ConsumerMethod, ConsumerPlan


def _leaf_tool_definition(title: str = "Leaf") -> FormalMethodDefinition:
    root, _, _, deps = shape()
    leaf = next(iter(deps.values()))
    doc = leaf.draft.definition.document()
    doc["capability_declaration"]["title"] = title
    doc["procedure"]["inputs"] = {
        "x": {
            "description": "输入",
            "schema": {"type": "number"},
            "required": True,
            "meaning": {"domain_id": "metric.amount"},
            "on_missing": "fail",
        }
    }
    doc["procedure"]["outputs"] = {
        "out": {
            "description": "输出",
            "schema": {
                "type": "object",
                "additionalProperties": False,
                "required": ["val", "extra"],
                "properties": {
                    "val": {"type": "number"},
                    "extra": {"type": "string"},
                },
            },
            "required": True,
            "meaning": {"domain_id": "metric.amount.net"},
            "on_missing": "fail",
        }
    }
    doc["how_to_validate"][0]["outputs"] = ["out"]
    return FormalMethodDefinition.model_validate(doc)


def test_frontier_guard_skip_projection_lens_and_collect_pack_execution():
    leaf_def = _leaf_tool_definition("Producer")
    leaf_ref = MethodRef(method_id=uuid4(), version=1, digest=leaf_def.digest())

    pack_leaf_doc = leaf_def.document()
    pack_leaf_doc["procedure"]["inputs"] = {
        "items": {
            "description": "收集包",
            "schema": {
                "type": "array",
                "items": {
                    "type": "object",
                    "additionalProperties": False,
                    "required": ["tag", "status", "value"],
                    "properties": {
                        "tag": {"type": "string"},
                        "status": {"type": "string", "enum": ["present", "skipped"]},
                        "value": {
                            "anyOf": [
                                {
                                    "type": "object",
                                    "additionalProperties": False,
                                    "required": ["val"],
                                    "properties": {"val": {"type": "number"}},
                                },
                                {"type": "null"},
                            ]
                        },
                    },
                },
            },
            "required": True,
            "meaning": {"domain_id": "collection.pack"},
            "on_missing": "fail",
        }
    }
    pack_leaf_def = FormalMethodDefinition.model_validate(pack_leaf_doc)
    pack_ref = MethodRef(method_id=uuid4(), version=1, digest=pack_leaf_def.digest())

    root, _, _, _ = shape()
    orch_doc = root.draft.definition.document()
    narrow_slot = {
        "description": "投影对象",
        "schema": {
            "type": "object",
            "additionalProperties": False,
            "required": ["val"],
            "properties": {"val": {"type": "number"}},
        },
        "required": True,
        "meaning": {"domain_id": "metric.amount"},
        "on_missing": "fail",
    }
    wide_out = deepcopy(leaf_def.document()["procedure"]["outputs"]["out"])
    orch_doc["access_requirements"] = leaf_def.document()["access_requirements"]
    orch_doc["what_requires_approval"] = leaf_def.document()["what_requires_approval"]
    orch_doc["procedure"] = {
        "kind": "orchestration",
        "instructions": "先探测再按条件分支汇聚",
        "max_depth": 2,
        "max_expansions": 8,
        "max_steps": 8,
        "max_replans": 0,
        "steps": [
            {
                "id": "s_probe",
                "select_when": "执行初检探测",
                "required_when": "始终执行",
                "depends_on": [],
                "candidates": [leaf_ref.model_dump(mode="json")],
                "inputs": {
                    "x": {
                        "slot": deepcopy(
                            leaf_def.document()["procedure"]["inputs"]["x"]
                        ),
                        "binding": {"source": "caller", "name": "amount"},
                    }
                },
                "outputs": {"out": deepcopy(wide_out)},
                "always_required": True,
                "on_failure": "fail",
            },
            {
                "id": "s_guarded",
                "select_when": "探测超阈值时执行深查",
                "required_when": "初检值大于100时执行",
                "depends_on": ["s_probe"],
                "candidates": [leaf_ref.model_dump(mode="json")],
                "artifact_guard": {
                    "step": "s_probe",
                    "output": "out",
                    "pointer": "/val",
                    "op": "gt",
                    "value": 100,
                },
                "inputs": {
                    "x": {
                        "slot": deepcopy(
                            leaf_def.document()["procedure"]["inputs"]["x"]
                        ),
                        "binding": {"source": "caller", "name": "amount"},
                    }
                },
                "outputs": {"out": deepcopy(narrow_slot)},
                "always_required": False,
                "on_failure": "fail",
            },
            {
                "id": "s_join",
                "select_when": "汇聚分支结果",
                "required_when": "始终执行",
                "depends_on": ["s_probe", "s_guarded"],
                "candidates": [pack_ref.model_dump(mode="json")],
                "inputs": {
                    "items": {
                        "slot": deepcopy(pack_leaf_doc["procedure"]["inputs"]["items"]),
                        "binding": {
                            "source": "collect_pack",
                            "items": [
                                {
                                    "tag": "guarded_branch",
                                    "step": "s_guarded",
                                    "output": "out",
                                    "allow_skipped": True,
                                }
                            ],
                        },
                    }
                },
                "outputs": {"out": deepcopy(wide_out)},
                "always_required": True,
                "on_failure": "fail",
            },
        ],
        "exports": {
            "final": {"step": "s_join", "output": "out", "always_present": True}
        },
    }
    orch_doc["how_to_validate"] = [
        {
            "promise_id": next(iter(orch_doc["capability_declaration"]["promises"])),
            "outputs": ["final"],
            "check": "结构化核对",
            "evidence": "s_join",
            "on_failure": "fail",
            "v0_checks": [
                {
                    "kind": "numeric_identity",
                    "lhs_pointer": "/outputs/final/val",
                    "rhs_pointer": "/inputs/amount",
                    "tolerance": 0.01,
                },
                {"kind": "prohibited_terms", "pointer": "/final/extra"},
            ],
        }
    ]
    orch_def = FormalMethodDefinition.model_validate(orch_doc)

    accepted = {"s_probe": {"out": {"val": 50, "extra": "clean"}}}
    guard = orch_def.procedure.steps[1].artifact_guard
    assert guard is not None
    assert evaluate_artifact_guard(guard, accepted) is False

    # Skipped guarded step produces status="skipped" in CollectPack!
    skipped = {"s_guarded"}
    join_inputs = bind_step_inputs(
        orch_def.procedure.steps[2],
        {"amount": 50},
        accepted,
        skipped_steps=skipped,
    )
    assert join_inputs["items"] == [
        {"tag": "guarded_branch", "status": "skipped", "value": None}
    ]

    accepted["s_join"] = {"out": {"val": 50, "extra": "客观摘要"}}
    exported = export_outputs(orch_def, accepted, skipped_steps=skipped)
    assert exported == {"final": {"val": 50, "extra": "客观摘要"}}

    # V0 gate passes on clean output and fails on numeric mismatch or prohibited term!
    v0_ok = evaluate_v0_checks(orch_def, {"amount": 50}, exported)
    assert len(v0_ok) == 2 and all(r.passed for r in v0_ok)

    bad_export = {"final": {"val": 999, "extra": "强烈推荐买入"}}
    v0_bad = evaluate_v0_checks(orch_def, {"amount": 50}, bad_export)
    assert [r.passed for r in v0_bad] == [False, False]


def test_envelope_authorization_eligibility_and_multi_account_slot_sources():
    root, _, _, deps = shape()
    leaf = next(iter(deps.values()))
    root_doc = root.draft.definition.document()
    req_id = root_doc["access_requirements"][0]["requirement_id"]

    # Upgrade root approval rule to scope="envelope" with non-write trigger.
    req_ops = tuple(root_doc["access_requirements"][0]["operations"])
    root_doc["what_requires_approval"] = [
        ApprovalRule(
            requirement_id=req_id,
            operations=req_ops,
            when="立项包络预授权确认",
            preview=("summary",),
            on_denied="fail",
            scope="envelope",
        ).model_dump(mode="json")
    ]
    # Duplicate leaf step so same MethodRef runs twice under distinct slot_sources!
    step_1 = deepcopy(root_doc["procedure"]["steps"][0])
    step_1["id"] = "first_a"
    step_2 = deepcopy(step_1)
    step_2["id"] = "second_b"
    step_2["depends_on"] = ["first_a"]
    root_doc["procedure"]["steps"] = [step_1, step_2]
    root_doc["procedure"]["max_steps"] = 4
    root_doc["procedure"]["max_expansions"] = 4
    out_key = next(iter(root_doc["procedure"]["exports"]))
    root_doc["procedure"]["exports"][out_key]["step"] = "second_b"
    root_def = FormalMethodDefinition.model_validate(root_doc)
    root_ref = MethodRef(method_id=uuid4(), version=1, digest=root_def.digest())

    src_a = leaf.draft.source
    assert src_a is not None
    src_b = FormalMethodSource.model_validate(
        {**src_a.model_dump(mode="json"), "asset_id": str(uuid4())}
    )
    in_name = next(iter(root_def.interfaces()["inputs"]))
    plan = ConsumerPlan(
        match_task_id=uuid4(),
        preparation_revision=1,
        preview_digest="sha256:" + "e" * 64,
        reference=root_ref,
        draft=FormalMethodDraft(
            definition=root_def, source=None, allowed_callers=("host",)
        ),
        arguments={in_name: "input@example.test"},
        dependencies=(
            ConsumerMethod(
                reference=leaf.reference,
                draft=FormalMethodDraft(
                    definition=leaf.draft.definition,
                    source=src_a,
                    allowed_callers=("host",),
                ),
            ),
        ),
        slot_sources={"second_b": src_b},
        envelope_approved=True,
        policies={"first_a": "p1", "second_b": "p2"},
    )
    assert len(orchestration_plan(plan)) == 2
    assert len(execution_steps(plan)) == 2
    assert plan.execution_source(plan.dependencies[0], "first_a") == src_a
    assert plan.execution_source(plan.dependencies[0], "second_b") == src_b

    read_ctx = HostContext(
        task={},
        attempt={},
        asset={},
        registration=None,
        access={"requirement": {"requirement_id": req_id}},
        operation={"effect": "read", "operation": "fetch"},
        intent={"execution_kind": "formal_mcp"},
    )
    assert FormalHostCalls._can_auto_authorize_envelope(plan, read_ctx) is True

    # Write effect is NEVER auto-authorized by envelope!
    write_ctx = HostContext(
        task={},
        attempt={},
        asset={},
        registration=None,
        access={"requirement": {"requirement_id": req_id}},
        operation={"effect": "write", "operation": "send"},
        intent={"execution_kind": "formal_mcp"},
    )
    assert FormalHostCalls._can_auto_authorize_envelope(plan, write_ctx) is False

    # Unconfirmed envelope falls back to per-step Interaction!
    unconfirmed = plan.model_copy(update={"envelope_approved": False})
    assert FormalHostCalls._can_auto_authorize_envelope(unconfirmed, read_ctx) is False


@pytest.mark.asyncio
async def test_intake_review_batches_definitions_and_degrades_withdrawn_candidate():
    root, _, _, _ = shape()
    defs = {}
    options = []
    for label in ("专家甲", "专家乙"):
        doc = root.draft.definition.document()
        doc["capability_declaration"]["title"] = label
        d = FormalMethodDefinition.model_validate(doc)
        r = MethodRef(method_id=uuid4(), version=1, digest=d.digest())
        defs[r] = d
        options.append(SceneRegistry.public_declaration(r, d))

    # Withdraw second candidate after matching round finished!
    withdrawn_ref = MethodRef.model_validate(options[1]["reference"])
    del defs[withdrawn_ref]

    batch_calls = 0

    class BatchRegistry:
        local_definitions = True
        public_declaration = staticmethod(SceneRegistry.public_declaration)

        async def planning_definitions(self, owner, refs, *, experts_only=False):
            nonlocal batch_calls
            batch_calls += 1
            assert experts_only is True
            return {r: defs[r] for r in refs if r in defs}

        async def planning_declaration(self, owner, ref, *, experts_only=False):
            if ref not in defs:
                raise NotFound("withdrawn")
            return self.public_declaration(ref, defs[ref])

    promise_key = next(iter(options[0]["declaration"]["promises"]))
    promise_text = options[0]["declaration"]["promises"][promise_key]
    directory_payload = {"items": options, "more": False}
    artifact = {
        "version": 1,
        "content": {
            "contract": "knowbench/expert-match/v5",
            "outcome": "candidates",
            "requirements": [
                {
                    "id": "r1",
                    "kind": "result",
                    "statement": promise_text,
                    "question_quote": promise_text,
                    "required": True,
                }
            ],
            "picks": [
                {
                    "reference": opt["reference"],
                    "reason": "匹配",
                    "coverage": [
                        {
                            "requirement": "r1",
                            "promise": promise_key,
                            "quote": promise_text,
                        }
                    ],
                    "limitations": [],
                }
                for opt in options
            ],
            "explanation": "两项候选",
            "clarification": None,
        },
    }
    task = {
        "id": str(uuid4()),
        "session_id": str(uuid4()),
        "profile_id": "FIND_EXPERT",
        "profile_version": "5",
        "state": "SUCCEEDED",
        "original_input": {"input": {"text": f"需要{promise_text}"}},
    }

    class FakeRepo:
        def transaction(self):
            class Tx:
                async def __aenter__(self):
                    return self

                async def __aexit__(self, *args):
                    return False

            return Tx()

        async def get_task(self, tid, **kw):
            return task

        async def get_artifact(self, **kw):
            return artifact

        async def list_task_events(self, tid, *, types=()):
            if "task/formal_expert_directory" in types:
                return [{"payload": directory_payload}]
            return []

        async def latest_task_payload(self, tid, event_type):
            return None

    reg = BatchRegistry()
    repo = FakeRepo()
    res = await review(repo, reg, "owner-1", task, artifact)
    assert batch_calls == 1
    assert res["options"][0].get("unavailable") is None
    assert res["options"][1]["unavailable"] is True

    # Confirming the withdrawn candidate still raises NotFound!
    with pytest.raises(NotFound):
        await confirm(
            repo,
            reg,
            "owner-1",
            task["id"],
            SimpleNamespace(
                reference=withdrawn_ref, review_digest=res["review_digest"]
            ),
        )

    # Exact material_slots extraction from domain_id:
    doc = root.draft.definition.document()
    first_step = doc["procedure"]["steps"][0]
    in_port = next(iter(first_step["inputs"]))
    first_step["inputs"][in_port]["slot"]["meaning"] = {
        "domain_id": "material.pdf_document"
    }
    mat_def = FormalMethodDefinition.model_validate(doc)
    caller_name = first_step["inputs"][in_port]["binding"]["name"]
    assert declared_material_slots(mat_def) == [caller_name]


@pytest.mark.asyncio
async def test_recursive_root_expert_enforces_root_v0_checks_and_exports(monkeypatch):
    import app.application.workbench.formal_recursive_runtime as rec_rt
    from app.domain.workbench.capabilities import digest
    from app.domain.workbench.formal_execution import plan_slot, recursive_plan
    from app.domain.workbench.formal_method_generated import (
        ProhibitedTermsCheck,
        SchemaRangeCheck,
    )
    from app.domain.workbench.formal_recursive import node_key

    sub_req, _, _, deps = shape()
    leaf = next(iter(deps.values()))
    sub_def = sub_req.draft.definition
    sub_ref = MethodRef(method_id=uuid4(), version=1, digest=sub_def.digest())

    # Build a 2-level recursive root expert calling sub_expert (which calls leaf)
    root_doc = sub_def.document()
    root_doc["capability_declaration"]["title"] = "递归组合根专家"
    root_doc["procedure"]["max_depth"] = 4
    root_doc["procedure"]["max_expansions"] = 6
    root_doc["procedure"]["max_steps"] = 4
    root_doc["procedure"]["steps"][0]["id"] = "sub"
    root_doc["procedure"]["steps"][0]["candidates"] = [sub_ref.model_dump(mode="json")]
    root_doc["procedure"]["exports"] = {
        "receipt": {"step": "sub", "output": "receipt", "always_present": True}
    }
    promise_id = root_doc["how_to_validate"][0]["promise_id"]
    root_doc["how_to_validate"][0]["outputs"] = ["receipt"]
    root_doc["how_to_validate"][0]["v0_checks"] = [
        SchemaRangeCheck(
            kind="schema_and_range", pointer="/receipt", non_empty=True
        ).model_dump(mode="json"),
        ProhibitedTermsCheck(kind="prohibited_terms", pointer="/receipt").model_dump(
            mode="json"
        ),
    ]
    root_def = FormalMethodDefinition.model_validate(root_doc)
    root_ref = MethodRef(method_id=uuid4(), version=1, digest=root_def.digest())

    plan = ConsumerPlan(
        match_task_id=uuid4(),
        preparation_revision=1,
        preview_digest="sha256:" + "d" * 64,
        reference=root_ref,
        draft=FormalMethodDraft(
            definition=root_def, source=None, allowed_callers=("host",)
        ),
        arguments={"to": "audit@example.test"},
        dependencies=(
            ConsumerMethod(
                reference=sub_ref,
                draft=FormalMethodDraft(
                    definition=sub_def, source=None, allowed_callers=("host",)
                ),
            ),
            ConsumerMethod(
                reference=leaf.reference,
                draft=FormalMethodDraft(
                    definition=leaf.draft.definition,
                    source=leaf.draft.source,
                    allowed_callers=("host",),
                ),
            ),
        ),
        policies={plan_slot(("sub", "only"), nested=True): "p1"},
        scope_hours=1,
    )
    assert recursive_plan(plan) is True

    task_id = str(uuid4())
    session_id = str(uuid4())
    task = {
        "id": task_id,
        "owner_actor_id": "actor-1",
        "session_id": session_id,
        "current_step": 0,
        "pack_digest": plan.task_columns()["pack_digest"],
    }

    calls = {
        "task:" + task_id: {
            "id": str(uuid4()),
            "status": "running",
            "detail": {"path": []},
        },
        node_key(task_id, ("sub",)): {
            "id": str(uuid4()),
            "status": "succeeded",
            "detail": {"path": ["sub"], "arguments": {"to": "audit@example.test"}},
        },
        node_key(task_id, ("sub", "only")): {
            "id": str(uuid4()),
            "status": "succeeded",
            "detail": {
                "path": ["sub", "only"],
                "arguments": {"to": "audit@example.test"},
            },
        },
    }

    class FakeRecursiveRepo:
        async def call_by_key(self, owner, session, key):
            return calls.get(key)

        async def put_artifact(self, *, task_id, attempt_id, name, kind, content):
            return {
                "id": str(uuid4()),
                "name": name,
                "version": 1,
                "digest": digest(content),
            }

        async def update_call(self, owner, cid, status, detail):
            for k, row in calls.items():
                if row["id"] == cid:
                    calls[k] = {**row, "status": status, "detail": detail}

        async def append_event(self, **kw):
            return None

    async def fake_return_subtree(*args, **kwargs):
        return None

    monkeypatch.setattr(rec_rt, "return_subtree", fake_return_subtree)
    repo = FakeRecursiveRepo()

    # 1. Positive case: leaves + sub_expert + root V0 checks all pass -> root exports returned
    async def ok_children(_repo, _task, _snap, _parent, parent_path, before=None):
        if tuple(parent_path) == ("sub",):
            return {"only": {"receipt": "客观勾稽核验回执"}}, []
        return {"sub": {"receipt": "客观勾稽核验回执"}}, []

    monkeypatch.setattr(rec_rt, "accepted_children", ok_children)
    settled = await rec_rt.settle_ancestors(repo, task, plan, outcome="succeeded")
    assert settled is not None
    assert settled["outputs"] == {"receipt": "客观勾稽核验回执"}

    # 2. Negative V0 case: leaves pass & schema valid, but root ProhibitedTermsCheck fails!
    async def bad_v0_children(_repo, _task, _snap, _parent, parent_path, before=None):
        if tuple(parent_path) == ("sub",):
            return {"only": {"receipt": "强烈推荐买入"}}, []
        return {"sub": {"receipt": "强烈推荐买入"}}, []

    monkeypatch.setattr(rec_rt, "accepted_children", bad_v0_children)
    with pytest.raises(ValueError, match=rf"^v0_validation_failed: {promise_id}:"):
        await rec_rt.settle_ancestors(repo, task, plan, outcome="succeeded")

    # 3. Missing required root export case: child omits required export -> export_outputs fails!
    async def missing_export_children(
        _repo, _task, _snap, _parent, parent_path, before=None
    ):
        if tuple(parent_path) == ("sub",):
            return {"only": {"receipt": "客观回执"}}, []
        return {"sub": {}}, []

    monkeypatch.setattr(rec_rt, "accepted_children", missing_export_children)
    with pytest.raises(ValueError, match="Missing plan value: receipt"):
        await rec_rt.settle_ancestors(repo, task, plan, outcome="succeeded")
