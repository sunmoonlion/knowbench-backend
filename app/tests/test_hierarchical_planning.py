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
def test_explicit_delivery_survives_planner_resume(author):
    task, _ = setup(author=author)
    planner = HierarchicalPlanner(task)
    first, branch = proposal()["steps"]
    branch["deliver_as"] = {"receipt": "difference"}
    inspect(planner, "a", "b", "first")
    assert (
        planner.advance(
            decision(branch, "branch", children={"prepare": "摘取原表明细"})
        )
        is None
    )
    resumed = HierarchicalPlanner(task, planner.snapshot())
    assert resumed.prompt()["public_deliveries"] == {
        "difference": {"step": "n1", "output": "receipt"}
    }
    first["deliver_as"] = {"receipt": "evidence"}
    result = resumed.advance(decision(first))
    artifact = result["selection"] if author else result
    assert [s["deliver_as"] for s in artifact["steps"]] == [
        {"receipt": "evidence"},
        {"receipt": "difference"},
    ]


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
    assert "patterns" not in artifact
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


@pytest.mark.parametrize("author", [False, True])
def test_replanning_a_legacy_sketch_supplies_business_structure_without_labels(author):
    task, _ = setup(author=author)
    src = task["original_input"]["input"]
    previous = proposal()
    if author:
        src["incubation"]["selection_context"]["previous"] = previous
    else:
        src["previous_selection"] = previous
    brief = HierarchicalPlanner(task).prompt()["request"]["previous_selection"]
    assert "patterns" not in brief
    assert brief["steps"] == previous["steps"]
    assert brief["routing_required"] is True


def test_focused_revision_keeps_parent_and_invalidates_changed_pins():
    task, _ = setup(author=True)
    p = HierarchicalPlanner(task)
    artifact = p.advance(route(p))["selection"]
    ctx = task["original_input"]["input"]["incubation"]
    ctx["selection_context"]["previous"] = artifact
    ctx["instruction"] = "[n1_1] 更正提取范围"
    revised = HierarchicalPlanner(task)
    assert revised.current.id == "n1_1" and revised.nodes["n1"].status == "split"
    ctx["selection_context"]["methods"]["b"]["declaration"]["title"] = "保留的分支改版"
    stale = HierarchicalPlanner(task)
    assert stale.stale_edit and stale.current.id == "n1"


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
    for old in (legacy, current):
        old = {k: v for k, v in old.items() if k != "digest"}
        old["hints"] = {
            k: f"P{i} {v}" for i, (k, v) in enumerate(old["hints"].items(), 1)
        }
        old["digest"] = digest(old)
        assert same_runtime(old, current)
        assert not same_runtime(old, host_capabilities(False, False, True))
        assert not same_runtime({**old, "digest": "invalid"}, current)


def test_gap_with_search_suggestion_does_not_consume_a_repair():
    task, _ = setup()
    out = HierarchicalPlanner(task).advance(
        dict(action="gap", reason="缺汇总", search_goal="五项汇总")
    )
    assert out["status"] == "gap" and not out.get("search_goal") and not out["steps"]


@pytest.mark.parametrize("author", [True, False])
def test_each_identical_sibling_goal_needs_its_own_model_decision_after_resume(author):
    task, _ = setup(author=author)
    src = task["original_input"]["input"]
    if author:
        methods = src["incubation"]["selection_context"]["methods"]
    else:
        methods = {
            entry["alias"]: entry["interfaces"] for entry in src["catalog"]["entries"]
        }
    sample = methods["first"]["inputs"]["value"]
    methods["a"]["inputs"] = {"left": sample, "right": sample}
    p = HierarchicalPlanner(task)
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
    first = proposal()["steps"][0]
    assert set(p.prompt()["required_outputs"]) == {"left"}
    assert p.advance(decision(first)) is None
    saved = p.snapshot()
    p = HierarchicalPlanner(task, saved)
    clarification = p.advance({"action": "clarify", "reason": "第二份用哪组资料？"})
    assert (clarification["selection"] if author else clarification)[
        "status"
    ] == "clarify"
    p = HierarchicalPlanner(task, saved)
    assert p.current.goal == "生成回执" and p.current.step is None
    assert set(p.prompt()["required_outputs"]) == {"right"}
    second = {**first, "inputs": {"value": {"source": "caller", "name": "other_rows"}}}
    handback = p.advance(decision(second))
    result = handback["selection"] if author else handback
    assert result["steps"][0]["inputs"]["value"]["name"] == "rows"
    assert result["steps"][1]["inputs"]["value"]["name"] == "other_rows"
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
    assert "patterns" not in result
    assert result["object_batch"]["max_objects"] == 3


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


def test_directory_first_turn_shows_input_responsibilities_and_only_inspects_or_stops():
    task, _ = setup()
    catalog = task["original_input"]["input"]["catalog"]
    catalog["entries"] += [{**deepcopy(catalog["entries"][0]), "alias": "fourth"}]
    planner = HierarchicalPlanner(task)
    context = planner.prompt()
    _, schema = planner.model_contract(context)
    assert context["authorized_index"]["first"]["inputs"].keys() == {"value"}
    assert set(schema["discriminator"]["mapping"]) == {"inspect", "gap", "clarify"}
    planner.advance({"action": "inspect", "methods": ["first"]})
    resumed = HierarchicalPlanner(task, planner.snapshot())
    _, schema = resumed.model_contract(resumed.prompt())
    assert (
        "hit" in schema["discriminator"]["mapping"]
        and "split" in schema["discriminator"]["mapping"]
    )


def test_multiple_planning_replies_cannot_choose_a_winner():
    from app.domain.workbench.hierarchical_planning import turn_answer

    messages = [
        {"type": "agentMessage", "text": '{"action":"gap","reason":"x"}'},
        {"type": "agentMessage", "text": '{"action":"inspect","methods":["a"]}'},
    ]
    assert turn_answer(messages) is None


@pytest.mark.parametrize("author", [False, True])
def test_live_planning_checks_for_a_reserved_choice_before_inspection(author):
    task, _ = setup(author=author)
    request = "两套材料都齐全，我稍后指定用哪套。"
    if author:
        task["original_input"]["input"]["incubation"]["seed"]["text"] = request
    else:
        task["original_input"]["input"]["text"] = request
    planner = HierarchicalPlanner(task)
    inspect = {"action": "inspect", "methods": ["first"]}
    with pytest.raises(ValueError, match="pending_choice"):
        planner.advance(inspect, require_choice_check=True)
    with pytest.raises(ValueError, match="must quote"):
        planner.advance(
            {**inspect, "pending_choice": "a catalog suggestion"},
            require_choice_check=True,
        )
    result = planner.advance(
        {**inspect, "pending_choice": "我稍后指定用哪套"}, require_choice_check=True
    )
    selection = result["selection"] if author else result
    assert selection["status"] == "clarify" and selection["steps"] == []
    assert "我稍后指定用哪套" in selection["reason"]


def test_deferred_choice_wins_over_binding_and_a_later_answer_allows_binding():
    task, _ = setup()
    src = task["original_input"]["input"]
    src["text"] = "稍后由我选择口径；两份数据都已备好。"
    planner = HierarchicalPlanner(task)
    raw = {**decision(proposal()["steps"][0]), "pending_choice": "稍后由我选择口径"}
    stopped = planner.advance(raw, require_choice_check=True)
    assert stopped["status"] == "clarify" and not stopped["steps"]

    src["clarification"] = "已经确定采用合并口径。"
    src["previous_selection"] = stopped
    revised = HierarchicalPlanner(task)
    inspect(revised, "first")
    ready = revised.advance({**raw, "pending_choice": None}, require_choice_check=True)
    assert ready["status"] == "ready"


def test_compact_planner_prompt_preserves_all_catalog_and_schema_values():
    import json

    from app.application.workbench.task_runtime import TaskRuntime
    from app.domain.workbench.packs import find_pack

    task, _ = setup()
    planner = HierarchicalPlanner(task)
    context = planner.prompt()
    rules, schema = planner.model_contract(context)
    step = (
        find_pack("FIND_EXPERT", "7")
        .workflow[0]
        .model_copy(update={"method_text": rules, "output_schema": schema})
    )
    context["goal"] = "术语中的 空格、冒号: 与逗号, 都要保留"
    original = TaskRuntime.compose(step, context, {}, 0)
    compact = TaskRuntime.compose(step, context, {}, 0, sealed_planning=True)
    assert "request_user_input" in original and "request_user_input" not in compact
    assert "Return clarify in your JSON" in compact
    assert len(compact) < len(original)
    assert [
        json.loads(line) for line in compact.splitlines() if line.startswith(("{", "["))
    ] == [
        json.loads(line)
        for line in original.splitlines()
        if line.startswith(("{", "["))
    ]


@pytest.mark.parametrize("author", [False, True])
def test_separate_inspections_can_finish_a_branch_in_four_turns(author):
    task, _ = setup(author=author)
    src = task["original_input"]["input"]
    if author:
        methods = src["incubation"]["selection_context"]["methods"]
        methods["extra"] = deepcopy(methods["first"])
    else:
        entries = src["catalog"]["entries"]
        entries.append({**entries[0], "alias": "extra"})
    seed = HierarchicalPlanner(task)
    assert not seed.prompt()["contracts"]
    inspect(seed, "first", "a")
    inspect(seed, "b")
    resumed = HierarchicalPlanner(task, seed.snapshot())
    assert list(resumed.prompt()["contracts"]) == ["first", "a", "b"]
    first, branch = proposal()["steps"]
    assert (
        resumed.advance(
            decision(branch, "branch", children={"prepare": "摘取原表明细"})
        )
        is None
    )
    result = resumed.advance(decision(first))
    chosen = result["selection"] if author else result
    assert chosen["status"] == "ready" and resumed.turns == 4
    assert chosen["steps"][-1]["cases"] == {"A": "a", "B": "b"}


def test_disclosure_window_evicts_oldest_and_keeps_requested_contracts_atomic():
    task, _ = setup(author=True)
    p = HierarchicalPlanner(task)
    for entry in p.methods.values():
        entry["declaration"]["purpose"] = "字" * 15000
    inspect(p, "a", "b")
    inspect(p, "a", "first")
    assert p.shown == ["a", "first"]
    inspect(p, "b")
    assert list(p.prompt()["contracts"]) == ["first", "b"]
    with pytest.raises(ValueError, match="fewer"):
        p.advance({"action": "inspect", "methods": ["a", "first", "b"]})
    assert p.shown == ["first", "b"]
    p.methods["a"]["declaration"]["purpose"] = "宽" * 44000
    result = p.advance({"action": "inspect", "methods": ["a", "a"]})
    assert result["selection"]["status"] == "gap"


@pytest.mark.parametrize("author", [False, True])
@pytest.mark.parametrize("target", ["n1", "n1_1"])
def test_edit_can_replace_withdrawn_leaf_without_reusing_its_pin(author, target):
    task, _ = setup(author=author)
    planner = HierarchicalPlanner(task)
    result = planner.advance(route(planner))
    previous = result["selection"] if author else result
    src = task["original_input"]["input"]
    amendment = f"[{target}] 使用新的提取方法"
    if author:
        material = src["incubation"]
        ctx = material["selection_context"]
        ctx["previous"] = previous
        replacement = deepcopy(ctx["methods"].pop("first"))
        replacement["reference"]["method_id"] = str(uuid4())
        ctx["methods"]["new"] = replacement
        material["instruction"] = amendment
    else:
        src.update(previous_selection=previous, clarification=amendment)
        entry = src["catalog"]["entries"][0]
        entry["alias"] = "new"
        entry["reference"]["method_id"] = str(uuid4())
    revised = HierarchicalPlanner(task)
    assert not revised.stale_edit and revised.current.id == target
    assert "first" not in revised.prompt()["authorized_index"]
    assert "first" not in revised.used_pins()
    inspect(revised, "new")
    child = {**proposal()["steps"][0], "method": "new"}
    updated = revised.advance(decision(child))
    selection = updated["selection"] if author else updated
    assert selection["status"] == "ready"
    if target == "n1_1":
        assert selection["steps"][-1] == previous["steps"][-1]
    assert set(selection["tree"]["pins"]) == (
        {"new", "a", "b"} if target == "n1_1" else {"new"}
    )


@pytest.mark.parametrize("fenced", [False, True])
def test_unphased_explanation_can_precede_only_one_complete_decision(fenced):
    import json

    from app.domain.workbench.hierarchical_planning import turn_answer

    answer = '{"action":"gap","reason":"缺少能生成汇总报告的方法"}'
    final = f"```json\n{answer}\n```" if fenced else answer

    def messages(*parts):
        return [{"type": "agentMessage", "text": part} for part in parts]

    replies = messages("Checking supplied `m1`.", "各项核验都有，缺少汇总。\n" + final)
    assert json.loads(turn_answer(replies)) == json.loads(answer)
    assert turn_answer(messages(answer, final)) is None
    assert turn_answer(messages(answer + answer)) is None
    assert turn_answer(messages("example: [", answer + "]")) is None
    assert turn_answer(messages("Explanation.", final + " trailing")) is None


def test_only_a_single_verbatim_human_excerpt_can_be_unwrapped_for_clarification():
    task, _ = setup()
    src = task["original_input"]["input"]
    src["text"] = "给了两组原行，先问我采用哪一个报告年度再动手。"

    def inspect_with(note):
        return HierarchicalPlanner(task).advance(
            {"action": "inspect", "methods": ["first"], "pending_choice": note},
            require_choice_check=True,
        )

    result = inspect_with('模型解释，用户要求"先问我采用哪一个报告年度再动手"')
    assert result["status"] == "clarify" and result["reason"].endswith(
        "：先问我采用哪一个报告年度再动手"
    )
    assert "模型解释" not in result["reason"] and not result["steps"]
    for note in (
        "模型猜测“确认费用”",
        "解释「给了两组原行」或「哪一个报告年度」",
        '说明 " "',
    ):
        with pytest.raises(ValueError, match="must quote"):
            inspect_with(note)


@pytest.mark.parametrize("author", [False, True])
@pytest.mark.parametrize("source", ["literal", "resource"])
def test_unsupported_binding_can_be_corrected_without_resetting_parent(author, source):
    from copy import deepcopy

    planner = HierarchicalPlanner(setup(author=author)[0])
    child = route(planner)
    before = planner.snapshot()
    _, schema = planner.model_contract(planner.prompt())
    inputs = schema["$defs"]["ProposedStep"]["properties"]["inputs"]
    [binding] = inputs["patternProperties"].values()
    sources = binding["discriminator"]["mapping"]
    assert set(sources) == {"caller", "step", "object_item", "object_rows"}
    invalid = deepcopy(child)
    invalid["step"]["inputs"]["value"] = {"source": source, "value": "undeclared"}
    with pytest.raises(ValueError, match="union_tag_invalid"):
        planner.advance(invalid)
    assert planner.snapshot() == before
    result = planner.advance(child)
    assert (result["selection"] if author else result)["status"] == "ready"


def test_direct_binding_accepts_copy_of_frozen_port_but_rejects_contract_changes():
    task, _ = setup(author=True)
    planner = HierarchicalPlanner(task)
    first = proposal()["steps"][0]
    method = first["method"]
    inspect(planner, method)
    port, binding = next(iter(first["inputs"].items()))
    slot = planner.methods[method]["inputs"][port]
    first["inputs"][port] = {
        **binding,
        "caller_slot": {**slot, "schema": {"type": "null"}},
    }
    with pytest.raises(ValueError, match="caller_slot"):
        planner.advance(decision(first))
    first["inputs"][port]["caller_slot"] = {**slot, "description": "作者对此输入的说明"}
    artifact = planner.advance(decision(first))["selection"]
    assert artifact["status"] == "ready"
    assert artifact["steps"][0]["inputs"][port] == binding


def test_separate_recovers_only_user_named_outputs_from_model_explanations():
    task, _ = setup()
    src = task["original_input"]["input"]
    src["text"] = (
        "独立交付 opening_evidence（期初平衡核验回执）和 cash_difference（现金滚动差额回执）。"
    )
    labels = {
        "opening_evidence": "opening_evidence（期初平衡核验回执）",
        "cash_difference": "cash_difference（现金滚动差额回执）",
    }
    goals = {
        name: f"按核验目标复算并交付 {label}，再执行额外动作。"
        for name, label in labels.items()
    }
    for bad in (
        goals | {"cash_difference": goals["opening_evidence"]},
        goals | {"cash_difference": labels["opening_evidence"]},
        goals | {"cash_difference": "交付 cash_difference（新增结果）。"},
        goals | {"cash_difference": "交付 extra_cash_difference（现金滚动差额回执）。"},
    ):
        planner = HierarchicalPlanner(task)
        with pytest.raises(ValueError, match="for cash_difference"):
            planner.advance({"action": "separate", "goals": bad})
        assert len(planner.nodes) == 1
    planner = HierarchicalPlanner(task)
    assert planner.advance({"action": "separate", "goals": goals}) is None
    resumed = HierarchicalPlanner(task, planner.snapshot())
    root = resumed.nodes["n1"]
    assert {
        name: resumed.nodes[node].goal for node, name in root.delivery_goals.items()
    } == labels


@pytest.mark.parametrize("author", [False, True])
def test_model_proposes_delivery_goals_from_an_open_question(author):
    task, _ = setup(author=author)
    src = task["original_input"]["input"]
    seed = src["incubation"]["seed"] if author else src
    question = "这次出差花了多少钱？哪些单据还没交？"
    seed["text"] = question
    planner = HierarchicalPlanner(task)
    assert "requested_outputs" not in planner.prompt()
    assert not planner.nodes["n1"].delivery_goals
    goals = {
        "trip_spend": {"goal": "按币种汇总差旅支出"},
        "missing_docs": {
            "goal": "列出未提交凭据的支出记录",
            "request_quote": "哪些单据还没交",
        },
    }
    raw = {"action": "separate", "goals": goals, "pending_choice": None}
    bad = deepcopy(raw)
    bad["goals"]["missing_docs"]["request_quote"] = "预测下月开销"
    with pytest.raises(ValueError, match="source quote for missing_docs"):
        planner.advance(bad, require_choice_check=True)
    assert len(planner.nodes) == 1
    assert planner.advance(raw, require_choice_check=True) is None
    resumed = HierarchicalPlanner(task, planner.snapshot())
    assert {
        name: resumed.nodes[node].goal
        for node, name in resumed.nodes["n1"].delivery_goals.items()
    } == {name: proposal["goal"] for name, proposal in goals.items()}


@pytest.mark.parametrize("author", [False, True])
@pytest.mark.parametrize("reverse", [False, True])
async def test_independent_deliveries_keep_both_sources_through_review_and_compile(
    author, reverse
):
    from app.domain.workbench.formal_method_plan_generated import export_outputs

    task, definitions = setup(author=author)
    question = "分别交回原表明细和费用回执。"
    src = task["original_input"]["input"]
    if author:
        src["incubation"]["seed"]["text"] = question
    else:
        src["text"] = question
    goals = [("detail", "原表明细"), ("fees", "费用回执")]
    if reverse:
        goals.reverse()
        ctx = src["incubation"]["selection_context"] if author else src
        ctx["target_deliverables"] = [
            {"promise_id": name, "outputs": [name], "summary": label}
            for name, label in goals
        ]
    planner = HierarchicalPlanner(task)
    inspect(planner, "first", "a")
    selection = {
        name: {"goal": f"生成并交回{label}", "request_quote": label}
        for name, label in goals
    }
    assert planner.advance({"action": "separate", "goals": selection}) is None
    sources = {}
    for name, label in goals:
        planner = HierarchicalPlanner(task, planner.snapshot())
        assert planner.prompt()["assigned_delivery"] == name
        inputs = {"value": {"source": "caller", "name": name + "_rows"}}
        if name == "fees":
            inputs["kind"] = {"source": "caller", "name": "basis"}
        step = dict(
            id=name,
            task=label,
            method="first" if name == "detail" else "a",
            inputs=inputs,
            deliver_as={"receipt": name},
        )
        node = planner.current.id
        sources[node] = {"receipt": name}
        out = planner.advance(decision(step))
    artifact = out["selection"] if author else out
    assert artifact["status"] == "ready"
    assert len(artifact["steps"]) == 2
    # The consumer reads the same public handback contract produced by both planners.
    consumer, _ = setup()
    consumer["original_input"]["input"]["catalog"]["entries"] = (
        src["catalog"]["entries"]
        if not author
        else list(src["incubation"]["selection_context"]["methods"].values())
    )
    choice = read_choice(consumer, artifact, check_graph=True)

    async def lookup(owner, pins):
        return {ref: definitions[ref] for ref in pins}

    _, root = await compile_choice(
        SimpleNamespace(formal_registry=SimpleNamespace(planning_definitions=lookup)),
        "fixture",
        str(uuid4()),
        question,
        choice,
        host_capabilities(True, True, True),
        ["join"],
    )
    assert export_outputs(root, sources) == {"detail": "detail", "fees": "fees"}


@pytest.mark.parametrize("author", [False, True])
def test_named_requirement_and_unbound_parallel_decision_cannot_be_reviewed_as_ready(
    author,
):
    task, _ = setup(author=author)
    src = task["original_input"]["input"]
    ctx = src["incubation"]["selection_context"] if author else src
    ctx["target_deliverables"] = [
        dict(
            promise_id="rows_promise",
            outputs=["detail"],
            summary="原表明细",
            expected_domain_id="core.check.receipt",
        ),
        dict(
            promise_id="fees_promise",
            outputs=["fees"],
            summary="费用回执",
            expected_domain_id="core.check.receipt",
        ),
    ]
    step = dict(
        id="read",
        method="first",
        task="提取明细",
        inputs={"value": {"source": "caller", "name": "rows"}},
        deliver_as={"receipt": "detail"},
    )
    p = HierarchicalPlanner(task)
    inspect(p, "first")
    out = p.advance(decision(step))
    result = out["selection"] if author else out
    assert result["status"] == "gap" and not result["steps"]
    assert "费用回执" in result["reason"] and "原表明细" not in result["reason"]

    p = HierarchicalPlanner(task)
    inspect(p, "first")
    goals = {"detail": "原表明细", "fees": "费用回执"}
    assert p.advance({"action": "separate", "goals": goals}) is None
    assert p.advance(decision(step)) is None
    resumed = HierarchicalPlanner(task, p.snapshot())
    out = resumed.advance(decision({**step, "deliver_as": {}}))
    result = out["selection"] if author else out
    assert result["status"] == "gap" and not result["steps"]
    assert "fees（费用回执）" in result["reason"]

    del ctx["target_deliverables"]
    p = HierarchicalPlanner(task)
    inspect(p, "first")
    out = p.advance(
        decision(
            step, "parallel_join", children={"detail": "原表明细", "fees": "费用回执"}
        )
    )
    result = out["selection"] if author else out
    assert result["status"] == "clarify" and not result["steps"]
    assert "费用回执" in result["reason"]
