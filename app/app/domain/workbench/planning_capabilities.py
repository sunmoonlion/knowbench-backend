"""Deployment evidence and structural requirements for consumer-side sketches."""

import re

from app.domain.workbench.expert_match import digest
from app.domain.workbench.formal_method_generated import Orchestration
from app.domain.workbench.formal_method_plan_generated import review_group

PATTERNS = {
    "sequence": r"串行接力|ordered handoff",
    "join": r"固定分支汇合|合并两份独立结果|merge both branch results",
    "independent_review": r"独立.{0,4}(复核|审查)|交叉复核|independent review|cross.review",
    "object_batch": r"按.{0,4}(对象|清单).{0,5}(展开|逐项)|逐对象|for each object",
    "parallel_dispatch": r"必须.{0,5}(并行|同时)执行|must run (in parallel|simultaneously)",
    "unbounded": r"无限(返工|重试)|unlimited retr(y|ies)",
    "conditional": r"按输入.{0,5}(选路|选法)|条件选路|choose by input",
}


def requirements(question, explicit=()):
    return sorted(
        set(explicit)
        | {k for k, expr in PATTERNS.items() if re.search(expr, question, re.I)}
    )


CAPABILITY_HINTS = {
    "sequence": "Pass a verified named output to the next method.",
    "join": "Run fixed branches and join their named results.",
    "independent_review": "Isolate independent reviews, then compare under the approved rules.",
    "object_batch": "Expand a confirmed bounded object list and aggregate verified outputs.",
    "conditional": "Choose a frozen candidate by an explicit input rule.",
}


# Exact hint text included in capability digests before step labels were retired.
LEGACY_HINTS = {
    name: f"{label} {CAPABILITY_HINTS[name]}"
    for name, label in {
        "sequence": "P1",
        "join": "P2",
        "independent_review": "P3",
        "object_batch": "P4",
        "conditional": "P5",
    }.items()
}


CATALOG_ENTRIES = 48
CATALOG_SNAPSHOT_BYTES = 480_000
CONSUMER_MAX_NODES = 4
CONSUMER_MAX_TURNS = 4


def same_runtime(saved, current):
    if saved == current:
        return True
    if current.get("contract") != "knowbench/consumer-planning/v2":
        return False
    expected = {k: v for k, v in current.items() if k != "digest"}
    if saved.get("contract") == "knowbench/consumer-planning/v1":
        expected["contract"] = saved["contract"]
        expected["limits"] = {
            k: v
            for k, v in expected["limits"].items()
            if k not in ("planning_nodes", "planning_turns", "planning_depth")
        }
        expected["limits"].update(candidate_nodes=8, context_bytes=16000)
    # A stored receipt binds the full capability inventory, including its old copy.
    for hints in (CAPABILITY_HINTS, LEGACY_HINTS):
        receipt = {**expected, "hints": hints}
        if saved == {**receipt, "digest": digest(receipt)}:
            return True
    return False


def capabilities(service):
    return host_capabilities(
        service.instruction_service is not None,
        service.script_enabled,
        service.material_read_enabled,
    )


def host_capabilities(independent_review, script, material_read):
    # Versions correspond to the proved host contracts, not model names or instructions.
    enabled = {
        "sequence": True,
        "join": True,
        "independent_review": bool(independent_review),
        "object_batch": True,
        "conditional": True,
    }
    doc = {
        "contract": "knowbench/consumer-planning/v2",
        "patterns": enabled,
        "hints": CAPABILITY_HINTS,
        "script": bool(script),
        "material_read": bool(material_read),
        "limits": {
            "planning_rounds": 2,
            "catalog_queries": 3,
            "candidate_nodes": CATALOG_ENTRIES,
            "context_bytes": CATALOG_SNAPSHOT_BYTES,
            "planning_nodes": CONSUMER_MAX_NODES,
            "planning_turns": CONSUMER_MAX_TURNS,
            "planning_depth": 2,
            "planning_seconds": 300,
            "turn_budget_cny": "2",
        },
    }
    return {**doc, "digest": digest(doc)}


def supports_graph(definition):
    p = definition.procedure
    if not isinstance(p, Orchestration):
        return set()
    patterns = set()
    if review_group(p):
        patterns.add("independent_review")
    if p.object_batch:
        patterns.add("object_batch")
    else:
        if any(len(step.depends_on) == 1 for step in p.steps):
            patterns.add("sequence")
        if any(len(step.depends_on) > 1 for step in p.steps):
            patterns.add("join")
    if p.has_candidate_choice:
        patterns.add("conditional")
    return patterns


def check_structure(root, definitions, runtime, required):
    implemented = supports_graph(root)
    if not set(required) <= implemented:
        raise ValueError("The sketch does not implement the required runtime structure")
    for definition in definitions.values():
        implemented.update(supports_graph(definition))
        procedure = definition.procedure
        if procedure.kind == "direct":
            kind = procedure.implementation.kind
            if kind in {"skill_script", "registered_script"} and not runtime["script"]:
                raise ValueError("The selected method needs a script runner")
            if kind == "service_function" and not runtime["material_read"]:
                raise ValueError("The selected method needs the material function")
    if any(not runtime["patterns"].get(p) for p in implemented):
        raise ValueError(
            "The selected structure requires unavailable runtime capabilities"
        )
