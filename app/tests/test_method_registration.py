"""Three categories depend on admitted identity, not protocols or model turns."""

from uuid import uuid4

import pytest
from pydantic import ValidationError

from app.domain.workbench.method_registration import (
    AdmittedDependency,
    MethodAdmission,
    MethodReference,
    classify_method,
    validate_dependency_graph,
)


def reference(*, method_id=None, version=1):
    return MethodReference(
        method_id=method_id or uuid4(), version=version, digest="sha256:" + "a" * 64
    )


def dependency(kind, *, ref=None):
    return AdmittedDependency(reference=ref or reference(), method_kind=kind)


def admission(*, dependencies=(), ref=None):
    ref = ref or reference()
    return MethodAdmission(
        reference=ref,
        implementation="orchestration" if dependencies else "direct",
        dependencies=dependencies,
        evidence=[
            {
                "evidence_id": str(uuid4()),
                "definition_digest": ref.digest,
                "checks": [
                    "definition",
                    "source",
                    "dependencies",
                    "authorization",
                    "approval",
                    "isolation",
                    "outcome",
                    "business",
                ],
            }
        ],
        visibility="private",
        uncovered=[],
    )


def test_distinct_experts_not_calls_versions_roles_or_protocols_determine_category():
    identity = uuid4()
    one = dependency("expert", ref=reference(method_id=identity))
    upgraded = dependency("expert", ref=reference(method_id=identity, version=2))
    tool = dependency("tool")
    assert classify_method(implementation="direct", dependencies=()) == "tool"
    assert (
        classify_method(implementation="orchestration", dependencies=(tool,))
        == "expert"
    )
    assert (
        classify_method(
            implementation="orchestration", dependencies=(one, upgraded, tool)
        )
        == "expert"
    )
    assert (
        classify_method(
            implementation="orchestration", dependencies=(one, dependency("expert"))
        )
        == "expert_team"
    )
    assert (
        classify_method(
            implementation="orchestration", dependencies=(dependency("expert_team"),)
        )
        == "expert_team"
    )
    with pytest.raises(ValueError):
        classify_method(implementation="direct", dependencies=(tool,))
    with pytest.raises(ValueError):
        classify_method(implementation="orchestration", dependencies=())


def test_all_candidates_must_resolve_with_exact_admission_not_remote_type_claims():
    tool = admission()
    expert = admission(dependencies=(dependency("tool", ref=tool.reference),))
    root = admission(dependencies=(dependency("expert", ref=expert.reference),))
    resolved = {(m.reference.method_id, m.reference.version): m for m in (tool, expert)}
    validate_dependency_graph(root, resolved)
    assert root.dispatch == expert.dispatch == "orchestration"
    assert tool.dispatch == "direct"
    with pytest.raises(ValueError):
        validate_dependency_graph(root, {})
    forged = root.model_copy(
        update={"dependencies": (dependency("tool", ref=expert.reference),)}
    )
    with pytest.raises(ValueError):
        validate_dependency_graph(forged, resolved)
    for limits in ({"max_depth": 2}, {"max_expansions": 2}):
        with pytest.raises(ValueError):
            validate_dependency_graph(root, resolved, **limits)


def test_cycle_checks_do_not_assume_three_categories_make_recursion_terminate():
    a, b = reference(), reference()
    first = admission(ref=a, dependencies=(dependency("expert", ref=b),))
    second = admission(ref=b, dependencies=(dependency("expert", ref=a),))
    with pytest.raises(ValueError, match="cycle"):
        validate_dependency_graph(
            first, {(a.method_id, a.version): first, (b.method_id, b.version): second}
        )


def test_private_uncovered_effects_never_excuse_missing_governance_or_public_validation():
    raw = admission().model_dump(mode="json")
    raw["evidence"][0]["checks"].remove("business")
    with pytest.raises(ValidationError):
        MethodAdmission.model_validate(raw)
    raw["uncovered"] = [
        {
            "scenario": "Real mailbox delivery",
            "reason": "No safe recipient account",
            "scope": "Private use with exact-action approval",
        }
    ]
    assert MethodAdmission.model_validate(raw).method_kind == "tool"
    for missing in ("authorization", "approval", "outcome", "isolation"):
        changed = {
            **raw,
            "evidence": [
                {
                    **raw["evidence"][0],
                    "checks": [c for c in raw["evidence"][0]["checks"] if c != missing],
                }
            ],
        }
        with pytest.raises(ValidationError):
            MethodAdmission.model_validate(changed)
    raw["visibility"] = "public"
    with pytest.raises(ValidationError):
        MethodAdmission.model_validate(raw)
