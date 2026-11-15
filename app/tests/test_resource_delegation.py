"""Synthetic resource-owner evidence; no fixture grants real resource rights."""

from copy import deepcopy
from datetime import UTC, datetime, timedelta
from uuid import UUID

import pytest
from method_access_support import ACCESS
from pydantic import ValidationError

from app.domain.workbench.method_capabilities import AccessRequirement
from app.domain.workbench.resource_delegation import (
    DelegationBinding,
    ResourceAuthorityRef,
    ResourceDelegation,
    ResourceDelegationCatalog,
)

NOW = datetime(2030, 1, 1, tzinfo=UTC)
OWNER = UUID("11111111-1111-4111-8111-111111111111")
CONNECTION = UUID("22222222-2222-4222-8222-222222222222")
OTHER = UUID("33333333-3333-4333-8333-333333333333")


def delegation_record(**changes):
    return ResourceDelegation.model_validate(
        {
            "record_id": "synthetic-resource-authority",
            "revision": 1,
            "issuer": "test:resource-owner",
            "owner_actor_id": str(OWNER),
            "binding": {
                "kind": "accepted_plan",
                "parent_method_id": "synthetic-parent",
                "parent_snapshot_digest": "sha256:" + "a" * 64,
                "child_method_id": "synthetic-child",
                "child_method_version": "1",
            },
            "connection_id": str(CONNECTION),
            "connection_revision": 1,
            "operation_digest": "b" * 64,
            "requirement": deepcopy(ACCESS),
            "max_delegation_depth": 1,
            "status": "active",
            "valid_from": (NOW - timedelta(days=1)).isoformat(),
            "expires_at": (NOW + timedelta(days=1)).isoformat(),
            "evidence": {
                "reference": "test:synthetic-resource-review",
                "digest": "sha256:" + "c" * 64,
            },
            **changes,
        }
    )


def context(record=None, **changes):
    record = record or delegation_record()
    return {
        "binding": record.binding,
        "connection_id": record.connection_id,
        "connection_revision": record.connection_revision,
        "operation_digest": record.operation_digest,
        "requirement": record.requirement,
        "now": NOW,
        **changes,
    }


def test_empty_catalog_never_accepts_a_well_formed_reference():
    record = delegation_record()
    catalog = ResourceDelegationCatalog()
    assert catalog.options(OWNER, **context()) == []
    with pytest.raises(ValueError):
        catalog.require(record.reference, OWNER, **context())


def test_exact_record_matches_owner_binding_account_policy_and_full_requirement():
    record = delegation_record()
    catalog = ResourceDelegationCatalog((record,))
    assert catalog.options(OWNER, **context()) == [record]
    assert catalog.require(record.reference, str(OWNER), **context()) == record
    assert (
        record.reference
        == ResourceDelegation.model_validate_json(record.model_dump_json()).reference
    )
    assert "evidence" not in record.reference.model_dump()
    assert "issuer" not in record.reference.model_dump()


@pytest.mark.parametrize(
    "change",
    [
        "owner",
        "parent",
        "snapshot",
        "child",
        "version",
        "connection",
        "connection_revision",
        "operation_digest",
        "capability",
        "capability_version",
        "operation",
        "resource",
        "role",
        "destination",
        "purpose",
        "argument_fields",
        "expired",
        "not_started",
    ],
)
def test_any_identity_or_scope_mismatch_denies_options_and_exact_reference(change):
    record = delegation_record()
    owner, args = OWNER, context(record)
    if change == "owner":
        owner = OTHER
    elif change in ("parent", "snapshot", "child", "version"):
        values = record.binding.model_dump()
        key, value = {
            "parent": ("parent_method_id", "other-parent"),
            "snapshot": ("parent_snapshot_digest", "sha256:" + "d" * 64),
            "child": ("child_method_id", "other-child"),
            "version": ("child_method_version", "2"),
        }[change]
        values[key] = value
        args["binding"] = DelegationBinding.model_validate(values)
    elif change in ("connection", "connection_revision", "operation_digest"):
        key, value = {
            "connection": ("connection_id", OTHER),
            "connection_revision": ("connection_revision", 2),
            "operation_digest": ("operation_digest", "d" * 64),
        }[change]
        args[key] = value
    elif change in ("expired", "not_started"):
        args["now"] = (
            record.expires_at
            if change == "expired"
            else record.valid_from - timedelta(microseconds=1)
        )
    else:
        need = record.requirement.model_dump(mode="json")
        if change == "capability":
            need["capability_ref"] = "another-capability"
        elif change == "capability_version":
            need["version"] = "2"
        elif change == "operation":
            need["operations"] = ["read"]
        elif change == "resource":
            need["resources"][0]["values"].append("other@example.test")
        elif change == "role":
            need["resources"][0]["resource_role"] = "other-role"
        elif change == "destination":
            need["data_transfer"]["destination"] = "https://other.example.test/mcp"
        elif change == "purpose":
            need["data_transfer"]["purpose"] = "another purpose"
        else:
            need["data_transfer"]["argument_fields"].append("secret")
        args["requirement"] = AccessRequirement.model_validate(need)
    catalog = ResourceDelegationCatalog((record,))
    assert catalog.options(owner, **args) == []
    with pytest.raises(ValueError):
        catalog.require(record.reference, owner, **args)


@pytest.mark.parametrize(
    "change",
    [
        {},
        {"revision": 2},
        {"issuer": "test:other-issuer"},
        {"owner_actor_id": str(OTHER)},
        {"status": "revoked"},
    ],
)
def test_duplicate_record_identity_is_rejected_even_for_a_different_revision(change):
    with pytest.raises(ValueError, match="Duplicate"):
        ResourceDelegationCatalog((delegation_record(), delegation_record(**change)))


@pytest.mark.parametrize(
    "field,value",
    [
        ("issuer", "test:changed-reviewer"),
        (
            "evidence",
            {"reference": "test:another-proof", "digest": "sha256:" + "d" * 64},
        ),
        ("valid_from", NOW - timedelta(hours=1)),
        ("expires_at", NOW + timedelta(hours=1)),
        ("status", "revoked"),
    ],
)
def test_same_revision_changed_record_never_matches_the_old_whole_document_digest(
    field, value
):
    original = delegation_record()
    changed = delegation_record(**{field: value})
    assert (
        original.record_id == changed.record_id
        and original.revision == changed.revision
    )
    assert original.reference.digest != changed.reference.digest
    with pytest.raises(ValueError):
        ResourceDelegationCatalog((changed,)).require(
            original.reference, OWNER, **context(original)
        )


def test_a_new_revision_or_alternate_record_cannot_silently_replace_chosen_evidence():
    original = delegation_record()
    for alternative in (
        delegation_record(revision=2),
        delegation_record(record_id="alternate"),
    ):
        catalog = ResourceDelegationCatalog((alternative,))
        assert catalog.options(OWNER, **context()) == [alternative]
        with pytest.raises(ValueError):
            catalog.require(original.reference, OWNER, **context())
    catalog = ResourceDelegationCatalog(
        (original, delegation_record(record_id="alternate"))
    )
    assert len(catalog.options(OWNER, **context())) == 2
    assert catalog.require(original.reference, OWNER, **context()) == original


def test_multiple_records_cannot_be_unioned_into_a_larger_scope():
    first = delegation_record()
    second_need = first.requirement.model_dump(mode="json")
    second_need["resources"][0]["values"] = ["second@example.test"]
    second = delegation_record(record_id="second-resource", requirement=second_need)
    combined = first.requirement.model_dump(mode="json")
    combined["resources"][0]["values"].append("second@example.test")
    args = context(requirement=AccessRequirement.model_validate(combined))
    catalog = ResourceDelegationCatalog((first, second))
    assert catalog.options(OWNER, **args) == []
    for record in (first, second):
        with pytest.raises(ValueError):
            catalog.require(record.reference, OWNER, **args)
    assert catalog.require(second.reference, OWNER, **context(second)) == second


def test_only_narrowing_is_permitted_and_times_are_half_open():
    record = delegation_record()
    wider = record.requirement.model_dump(mode="json")
    wider["resources"][0]["values"].append("second@example.test")
    ceiling = delegation_record(requirement=wider)
    catalog = ResourceDelegationCatalog((ceiling,))
    assert catalog.require(ceiling.reference, OWNER, **context(record)) == ceiling
    assert catalog.options(OWNER, **context(now=ceiling.valid_from)) == [ceiling]
    assert catalog.options(OWNER, **context(now=ceiling.expires_at)) == []
    with pytest.raises(ValueError, match="aware"):
        catalog.options(OWNER, **context(now=NOW.replace(tzinfo=None)))


@pytest.mark.parametrize(
    "changes",
    [
        {"revision": True},
        {"revision": 0},
        {"connection_revision": True},
        {"issuer": " "},
        {"owner_actor_id": "author"},
        {"status": "approved"},
        {"max_delegation_depth": 0},
        {"max_delegation_depth": True},
        {"max_delegation_depth": 1.0},
        {"max_delegation_depth": 2},
        {"valid_from": NOW.replace(tzinfo=None)},
        {"expires_at": NOW - timedelta(days=2)},
        {"operation_digest": "sha256:" + "b" * 64},
        {
            "evidence": {
                "reference": "https://user:secret@example.test/proof",
                "digest": "sha256:" + "c" * 64,
            }
        },
        {"evidence": {"reference": "test:proof", "digest": "unknown"}},
        {"browser_confirmed": True},
    ],
)
def test_malformed_or_unsupported_resource_authority_is_rejected(changes):
    with pytest.raises(ValidationError):
        delegation_record(**changes)


def test_binding_cannot_claim_a_formal_method_or_unbounded_descendants():
    raw = delegation_record().binding.model_dump()
    for extra in (
        {"kind": "formal_method"},
        {"child_method_version": "*"},
        {"parent_snapshot_digest": "unverified"},
        {"descendants": "all"},
    ):
        with pytest.raises(ValidationError):
            DelegationBinding.model_validate({**raw, **extra})
    with pytest.raises(ValidationError):
        ResourceAuthorityRef(record_id="authority", revision=1, digest="b" * 64)


def resource_parent():
    from app.domain.workbench.method_authorization import (
        MethodAuthorization,
        PermissionGrant,
    )

    record = delegation_record()
    ResourceDelegationCatalog((record,)).require(record.reference, OWNER, **context())
    grant = PermissionGrant(
        grant_id="reviewed-resource",
        confirmation_context="d" * 64,
        connection_id=record.connection_id,
        connection_revision=record.connection_revision,
        operation_digest=record.operation_digest,
        requirement=record.requirement,
        delegable=True,
        recipient_call_id=None,
        expires_at=record.expires_at,
        method_ids=(record.binding.child_method_id,),
        resource_authority=record.reference,
    )
    return MethodAuthorization(
        call_id=OWNER,
        revision=1,
        parent_call_id=None,
        parent_revision=None,
        method_id=record.binding.parent_method_id,
        revoked=False,
        expires_at=record.expires_at,
        grants=(grant,),
    )


@pytest.mark.parametrize(
    "change", ["missing_proof", "empty_methods", "direct_and_delegable"]
)
def test_delegable_grant_requires_pinned_proof_and_a_method_restriction(change):
    from app.domain.workbench.method_authorization import PermissionGrant

    raw = resource_parent().grants[0].model_dump(mode="json")
    if change == "missing_proof":
        raw.pop("resource_authority")
    elif change == "empty_methods":
        raw["method_ids"] = []
    else:
        raw["recipient_call_id"] = str(OTHER)
    with pytest.raises(ValidationError):
        PermissionGrant.model_validate(raw)


def test_one_hop_consumes_delegability_but_retains_exact_resource_proof():
    from app.domain.workbench.capabilities import CapabilityError
    from app.domain.workbench.method_authorization import check_delegation, delegate

    parent = resource_parent()
    child = delegate(
        parent, call_id=OTHER, method_id="synthetic-child", requirements=parent.grants
    )
    check_delegation(parent, child, now=NOW)
    assert child.grants[0].resource_authority == parent.grants[0].resource_authority
    assert child.grants[0].delegable is False
    assert child.grants[0].recipient_call_id == child.call_id
    assert child.grants[0].method_ids == ("synthetic-child",)
    for grants in (child.grants, parent.grants):
        with pytest.raises(CapabilityError):
            delegate(
                child,
                call_id=CONNECTION,
                method_id="synthetic-child",
                requirements=grants,
            )


@pytest.mark.parametrize(
    "change", ["removed", "another_id", "another_revision", "another_digest"]
)
def test_child_cannot_drop_or_replace_the_parent_resource_evidence(change):
    from app.domain.workbench.capabilities import CapabilityError
    from app.domain.workbench.method_authorization import (
        MethodAuthorization,
        check_delegation,
        delegate,
    )

    parent = resource_parent()
    child = delegate(
        parent, call_id=OTHER, method_id="synthetic-child", requirements=parent.grants
    )
    raw = child.model_dump(mode="json")
    proof = raw["grants"][0]["resource_authority"]
    if change == "removed":
        raw["grants"][0].pop("resource_authority")
    elif change == "another_id":
        proof["record_id"] = "another-record"
    elif change == "another_revision":
        proof["revision"] += 1
    else:
        proof["digest"] = "sha256:" + "e" * 64
    changed = MethodAuthorization.model_validate(raw)
    with pytest.raises(CapabilityError):
        check_delegation(parent, changed, now=NOW)
