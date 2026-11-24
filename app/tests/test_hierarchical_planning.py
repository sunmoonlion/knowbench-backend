from copy import deepcopy
from types import SimpleNamespace
from uuid import uuid4

import pytest
from test_temporary_plan_graph import QUESTION, directory, proposal

from app.application.workbench.temporary_planning import compile_choice
from app.domain.workbench.expert_match import digest
from app.domain.workbench.formal_method_plan_generated import (
    UnmatchedCandidate,
    select_plan,
)
from app.domain.workbench.hierarchical_planning import (
    HierarchicalPlanner,
    compact_schemas,
)
from app.domain.workbench.planning_capabilities import host_capabilities, same_runtime
from app.domain.workbench.temporary_plan import read_choice


def setup(*, author=False):
    task, definitions = directory()
    runtime = host_capabilities(True, True, True)
    entries = task["original_input"]["input"]["catalog"]["entries"]
    for entry, definition in zip(entries, definitions.values(), strict=True):
        entry.update(
            kind="direct",
            interfaces=definition.interfaces(),
            declaration=definition.capability_declaration.model_dump(mode="json"),
        )
    src = task["original_input"]["input"]
    src.update(text=QUESTION, runtime=runtime)
    src["catalog"]["partial"] = False
    task["profile_id"] = "FIND_EXPERT"
    if author:
        methods = {
            e["alias"]: {
                **e,
                **{
                    side: {k: v["slot"] for k, v in ports.items()}
                    for side, ports in e["interfaces"].items()
                },
            }
            for e in entries
        }
        task = {
            "profile_id": "INCUBATE",
            "profile_version": "2",
            "original_input": {
                "input": {
                    "incubation": {
                        "seed": {"text": QUESTION},
                        "selection_context": {
                            "methods": methods,
                            "patterns": runtime["patterns"],
                            "partial": False,
                        },
                    }
                }
            },
        }
    return task, definitions


def decision(step, operator="leaf", **extra):
    return dict(
        action="hit" if operator == "leaf" else "split",
        operator=operator,
        reason=step["task"],
        step=step,
        **extra,
    )


def inspect(planner, *aliases):
    if not set(aliases) <= planner.prompt()["contracts"].keys():
        assert planner.advance({"action": "inspect", "methods": aliases}) is None


def route(planner):
    first, branch = proposal()["steps"]
    inspect(planner, "a", "b", "first")
    assert (
        planner.advance(
            decision(branch, "branch", children={"prepare": "摘取原表明细"})
        )
        is None
    )
    return decision(first)


@pytest.mark.parametrize("author", [False, True])
async def test_both_entries_resume_then_compile_only_the_selected_route(author):
    task, definitions = setup(author=author)
    p = HierarchicalPlanner(task)
    leaf = route(p)
    resumed = HierarchicalPlanner(task, p.snapshot())
    assert resumed.current.parent == "n1"
    assert resumed.prompt()["required_outputs"]["value"]["output"] == "receipt"
    out = resumed.advance(leaf)
    artifact = out["selection"] if author else out
    assert artifact["status"] == "ready"
    assert [s["id"] for s in artifact["steps"]] == ["n1_1", "n1"]
    assert set(artifact["tree"]["pins"]) == {"a", "b", "first"}
    if author:
        return
    choice = read_choice(task, artifact, check_graph=True)

    async def lookup(owner, pins):
        return {r: definitions[r] for r in pins}

    service = SimpleNamespace(
        formal_registry=SimpleNamespace(planning_definitions=lookup)
    )
    _, root = await compile_choice(
        service,
        "owner",
        str(uuid4()),
        QUESTION,
        choice,
        host_capabilities(True, True, True),
        ["sequence", "conditional"],
    )
    for label, alias in (("A", "a"), ("B", "b")):
        assert select_plan(root, {"label": label}).procedure.steps[-1].candidates == (
            choice.aliases[alias],
        )
    with pytest.raises(UnmatchedCandidate):
        select_plan(root, {"label": "C"})


def test_focused_revision_keeps_parent_and_invalidates_changed_pins():
    task, _ = setup(author=True)
    p = HierarchicalPlanner(task)
    artifact = p.advance(route(p))["selection"]
    ctx = task["original_input"]["input"]["incubation"]
    ctx["selection_context"]["previous"] = artifact
    ctx["instruction"] = "[n1_1] 更正提取范围"
    revised = HierarchicalPlanner(task)
    assert revised.current.id == "n1_1" and revised.nodes["n1"].status == "split"
    ctx["selection_context"]["methods"]["first"]["declaration"]["title"] = "改版"
    assert HierarchicalPlanner(task).current.id == "n1"


def test_contract_conflict_backtracks_once_then_exposes_no_executable_steps():
    task, _ = setup(author=True)
    p = HierarchicalPlanner(task)
    p.methods["first"]["outputs"]["receipt"]["meaning"] = {"unit": "cents"}
    leaf = route(p)
    assert p.advance(leaf) is None and p.current.id == "n1"
    assert "incompatible" in p.current.reason
    leaf = route(p)
    result = p.advance(leaf)["selection"]
    assert result["status"] == "gap" and not result["steps"]


def test_large_index_exposes_late_match_without_its_full_schema_until_inspected():
    task, _ = setup()
    catalog = task["original_input"]["input"]["catalog"]
    base = catalog["entries"][0]
    catalog["entries"] = [{**deepcopy(base), "alias": f"a{i}"} for i in range(40)]
    p = HierarchicalPlanner(task)
    assert len(p.prompt()["authorized_index"]) == 40 and not p.prompt()["contracts"]
    leaf = proposal()["steps"][0]
    leaf["method"] = "a39"
    assert p.advance(decision(leaf)) is None
    assert p.current.status == "pending"
    inspect(p, "a39")
    assert set(p.prompt()["contracts"]) == {"a39"}
    assert p.advance(decision(leaf))["steps"][0]["method"] == "a39"


def test_total_and_depth_limits_are_fail_closed_and_repeated_inspect_is_rejected():
    task, _ = setup()
    p = HierarchicalPlanner(task)
    decision_in = {"action": "inspect", "methods": ["a"]}
    assert p.advance(decision_in) is None
    with pytest.raises(ValueError, match="new authorized"):
        p.advance(decision_in)
    p.turns = p.max_calls - 1
    out = p.advance({"action": "inspect", "methods": ["b"]})
    assert out["status"] == "gap" and not out["steps"]
    task["original_input"]["input"]["catalog"]["partial"] = True
    partial = HierarchicalPlanner(task).advance(
        {"action": "gap", "reason": "缺少汇总方法"}
    )
    assert partial["status"] == "clarify" and partial["search_goal"] == QUESTION


@pytest.mark.parametrize(
    "policy",
    [
        {"iteration": "fold"},
        {"iteration": "while"},
        {"review_policy": "distinct_model_families"},
    ],
)
def test_unsupported_governance_and_stateful_loops_cannot_create_a_plan(policy):
    task, _ = setup()
    p = HierarchicalPlanner(task)
    inspect(p, "first")
    result = p.advance(decision(proposal()["steps"][0], **policy))
    assert result["status"] == "gap" and not result["steps"]


def test_schema_dictionary_round_trips_and_legacy_receipts_still_bind_every_capability():
    schema = {"type": "object", "description": "例" * 200}
    docs = {"one": {"items": schema}, "two": {"properties": {"same": schema}}}
    compact, parts = compact_schemas(docs)

    def expand(v):
        if isinstance(v, dict) and set(v) == {"$planning_part"}:
            return expand(parts[v["$planning_part"]])
        return {k: expand(item) for k, item in v.items()} if isinstance(v, dict) else v

    assert expand(compact) == docs and set(compact) == set(docs)
    current = host_capabilities(True, False, True)
    legacy = {
        **current,
        "contract": "knowbench/consumer-planning/v1",
        "limits": dict(
            planning_rounds=2,
            catalog_queries=3,
            candidate_nodes=8,
            context_bytes=16000,
            planning_seconds=300,
            turn_budget_cny="2",
        ),
    }
    del legacy["digest"]
    legacy["digest"] = digest(legacy)
    assert same_runtime(legacy, current)
    assert not same_runtime(legacy, host_capabilities(False, False, True))


def test_gap_with_search_suggestion_does_not_consume_a_repair():
    task, _ = setup()
    out = HierarchicalPlanner(task).advance(
        dict(action="gap", reason="缺汇总", search_goal="五项汇总")
    )
    assert out["status"] == "gap" and not out.get("search_goal") and not out["steps"]


def test_identical_sibling_goals_reuse_the_decision_but_keep_separate_invocations():
    task, _ = setup(author=True)
    p = HierarchicalPlanner(task)
    methods = p.methods
    sample = methods["first"]["inputs"]["value"]
    methods["a"]["inputs"] = {"left": sample, "right": sample}
    parent = {
        "id": "join",
        "method": "a",
        "task": "核对两份回执",
        "inputs": {
            key: {"source": "step", "step": key, "output": "receipt"}
            for key in ("left", "right")
        },
    }
    inspect(p, "a", "first")
    assert (
        p.advance(
            decision(
                parent,
                "parallel_join",
                children={"left": "生成回执", "right": "生成回执"},
            )
        )
        is None
    )
    result = p.advance(decision(proposal()["steps"][0]))["selection"]
    assert [s["method"] for s in result["steps"]] == ["first", "first", "a"]
    assert result["steps"][0]["id"] != result["steps"][1]["id"]


@pytest.mark.parametrize("agreed", [False, True])
def test_map_requires_human_bound_and_verified_worker_output(agreed):
    task, _ = setup(author=True)
    material = task["original_input"]["input"]["incubation"]
    if agreed:
        material["instruction"] = "每轮最多三项，缺项先停。"
    methods = material["selection_context"]["methods"]
    slot = methods["first"]["outputs"]["receipt"]
    methods["a"]["inputs"] = {
        "rows": {
            **slot,
            "schema": {
                "type": "array",
                "items": {"type": "object", "properties": {"value": slot["schema"]}},
            },
        }
    }
    p = HierarchicalPlanner(task)
    inspect(p, "a", "first")
    parent = {
        "id": "collect",
        "method": "a",
        "task": "归集",
        "inputs": {
            "rows": {"source": "object_rows", "step": "worker", "output": "receipt"}
        },
    }
    result = p.advance(
        decision(
            parent,
            "bounded_loop",
            iteration="map",
            children={"worker": "处理清单中的一项"},
            object_batch={"max_objects": 3, "on_missing": "stop"},
            limit_evidence="最多三项",
        )
    )
    if not agreed:
        assert result["selection"]["status"] == "clarify"
        return
    worker = proposal()["steps"][0]
    worker["inputs"] = {"value": {"source": "object_item", "name": "objects"}}
    result = p.advance(decision(worker))["selection"]
    assert result["patterns"] == ["P4"] and result["object_batch"]["max_objects"] == 3


def test_independent_checks_share_source_but_receive_different_target_parameters():
    task, _ = setup(author=True)
    p = HierarchicalPlanner(task)
    sample = p.methods["first"]["inputs"]["value"]
    p.methods["first"]["inputs"] = {"target": sample, "rows": sample}
    p.methods["a"]["inputs"] = {"opening": sample, "closing": sample}
    parent = {
        "method": "a",
        "id": "join",
        "task": "汇总",
        "inputs": {
            key: dict(source="step", step=key, output="receipt")
            for key in ("opening", "closing")
        },
    }
    inspect(p, "a", "first")
    assert (
        p.advance(
            decision(
                parent,
                "parallel_join",
                children={"opening": "核期初", "closing": "核期末"},
            )
        )
        is None
    )
    child = {
        "method": "first",
        "id": "check",
        "task": "核对",
        "inputs": {key: dict(source="caller", name=key) for key in ("rows", "target")},
    }
    assert p.advance(decision(child, shared_inputs=["rows"])) is None
    with pytest.raises(ValueError, match="identical inputs"):
        p.advance(decision(child, shared_inputs=["rows", "target"]))
    out = p.advance(decision(child, shared_inputs=["rows"]))["selection"]
    first, second, _ = out["steps"]
    assert first["inputs"]["rows"] == second["inputs"]["rows"]
    assert first["inputs"]["target"] != second["inputs"]["target"]


def test_author_pin_survives_checkpoint_and_cannot_be_substituted():
    task, _ = setup(author=True)
    p = HierarchicalPlanner(task)
    selection = p.advance(route(p))["selection"]
    context = task["original_input"]["input"]["incubation"]["selection_context"]
    context.update(
        previous=selection,
        edit={
            "node": "n1_1",
            "mode": "method",
            "method": p.methods["first"]["reference"],
        },
    )
    task["original_input"]["input"]["incubation"]["instruction"] = "使用选定的登记版本"
    revised = HierarchicalPlanner(task)
    assert (
        revised.current.id == "n1_1" and revised.prompt()["requested_method"] == "first"
    )
    inspect(revised, "a", "first")
    restored = HierarchicalPlanner(task, revised.snapshot())
    wrong = {**proposal()["steps"][0], "method": "a"}
    with pytest.raises(ValueError, match="chosen by the author"):
        restored.advance(decision(wrong))
    assert (
        restored.advance(decision(proposal()["steps"][0]))["selection"]["status"]
        == "ready"
    )


def test_author_request_to_split_cannot_return_another_direct_hit():
    task, _ = setup(author=True)
    p = HierarchicalPlanner(task)
    selection = p.advance(route(p))["selection"]
    material = task["original_input"]["input"]["incubation"]
    material["selection_context"].update(
        previous=selection, edit={"node": "n1_1", "mode": "split"}
    )
    material["instruction"] = "拆解这一步"
    revised = HierarchicalPlanner(task)
    inspect(revised, "first")
    with pytest.raises(ValueError, match="decomposition"):
        revised.advance(decision(proposal()["steps"][0]))


def test_malformed_free_text_can_only_return_a_stop_without_search_or_steps():
    from app.domain.workbench.hierarchical_planning import read_stop_message

    bad = '{"action":"gap","reason":"缺少"正式报告"方法。","search_goal":null}'
    assert read_stop_message(bad) == {"action": "gap", "reason": '缺少"正式报告"方法。'}
    assert read_stop_message(bad.replace('"gap"', '"hit"')) is None
    assert read_stop_message(bad.replace("null", '"find"')) is None
    assert read_stop_message(bad.replace("方法。", '方法。","step":"run')) is None


def test_no_second_search_for_a_capability_once_authorized_directory_is_complete():
    task, _ = setup()
    stop = {
        "action": "clarify",
        "reason": "要查找尚未获权的汇总方法",
        "search_goal": "汇总",
    }
    assert HierarchicalPlanner(task).advance(stop)["status"] == "gap"
    task["original_input"]["input"]["catalog"]["partial"] = True
    out = HierarchicalPlanner(task).advance(stop)
    assert out["status"] == "clarify" and out["search_goal"] == "汇总"


def test_installed_tool_does_not_discard_other_nodes_when_catalog_aliases_shift():
    task, _ = setup(author=True)
    p = HierarchicalPlanner(task)
    previous = p.advance(route(p))["selection"]
    material = task["original_input"]["input"]["incubation"]
    ctx = material["selection_context"]
    ctx["previous"] = previous
    material["instruction"] = "[n1_1] 补工具后重试"
    ctx["methods"] = {f"new_{k}": v for k, v in ctx["methods"].items()}
    q = HierarchicalPlanner(task)
    assert q.current.id == "n1_1" and q.nodes["n1"].step.method == "new_a"
    assert q.nodes["n1"].step.cases == {"A": "new_a", "B": "new_b"}
    assert not q.may_replan(q.nodes["n1"])


def test_provider_line_wrap_requires_a_complete_json_document_and_normal_admission():
    from app.domain.workbench.hierarchical_planning import recover_decision

    task, _ = setup()
    p = HierarchicalPlanner(task)
    text = '{"\naction":"hit","reason":"x","operator":"leaf","step":{"id":"x","method":"absent","task":"x"}}'
    restored = recover_decision(text)
    with pytest.raises(ValueError, match="authorized"):
        p.advance(restored)
    assert recover_decision(text + " trailing") is None
