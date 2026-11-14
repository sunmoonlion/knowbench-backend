"""Delegation cannot replace resource authority, action approval or input binding."""

from datetime import UTC, datetime, timedelta
from uuid import uuid4

import pytest
from method_access_support import ACCESS

from app.domain.workbench.capabilities import CapabilityError
from app.domain.workbench.method_authorization import (
    MethodAuthorization,
    PermissionGrant,
    check_delegation,
    delegate,
)


def parent():
    expiry = datetime.now(UTC) + timedelta(hours=1)
    grant = PermissionGrant(
        grant_id="mail",
        confirmation_context="a" * 64,
        connection_id=uuid4(),
        connection_revision=1,
        operation_digest="b" * 64,
        requirement=ACCESS,
        delegable=True,
        recipient_call_id=None,
        expires_at=expiry,
        method_ids=(),
    )
    return MethodAuthorization(
        call_id=uuid4(),
        revision=1,
        parent_call_id=None,
        parent_revision=None,
        method_id="parent",
        revoked=False,
        expires_at=expiry,
        grants=(grant,),
    )


def child(root):
    return delegate(root, call_id=uuid4(), method_id="child", requirements=root.grants)


def test_authority_inheritance_does_not_require_or_fetch_input_values():
    root = parent()
    leaf = child(root)
    check_delegation(root, leaf)
    assert leaf.grants == root.grants
    assert leaf.parent_call_id == root.call_id
    assert (
        not {"inputs", "content", "credentials"}
        & MethodAuthorization.model_fields.keys()
    )


@pytest.mark.parametrize(
    "change",
    [
        "account",
        "revision",
        "operation",
        "purpose",
        "target",
        "fields",
        "resources",
        "role",
        "policy",
        "expiry",
        "method",
        "nondelegable",
        "revoke",
    ],
)
def test_parent_cannot_authorize_incompatible_child(change):
    root = parent()
    raw = root.grants[0].model_dump(mode="json")
    if change == "account":
        raw["connection_id"] = str(uuid4())
    elif change == "revision":
        raw["connection_revision"] += 1
    elif change == "policy":
        raw["operation_digest"] = "c" * 64
    elif change == "operation":
        raw["requirement"]["operations"] = ["read"]
    elif change == "purpose":
        raw["requirement"]["data_transfer"]["purpose"] = "Another purpose"
    elif change == "target":
        raw["requirement"]["data_transfer"]["destination"] = (
            "https://other.example.test/mcp"
        )
    elif change == "fields":
        raw["requirement"]["data_transfer"]["argument_fields"].append("secret")
    elif change == "resources":
        raw["requirement"]["resources"][0]["values"].append("other@example.test")
    elif change == "role":
        raw["requirement"]["resources"][0]["resource_role"] = "other-role"
    elif change == "expiry":
        raw["expires_at"] = (datetime.now(UTC) + timedelta(days=1)).isoformat()
    elif change == "method":
        root = root.model_copy(
            update={
                "grants": (
                    root.grants[0].model_copy(update={"method_ids": ("other",)}),
                )
            }
        )
    elif change == "nondelegable":
        root = root.model_copy(
            update={"grants": (root.grants[0].model_copy(update={"delegable": False}),)}
        )
    else:
        root = root.model_copy(update={"revoked": True})
    with pytest.raises(CapabilityError):
        delegate(
            root,
            call_id=uuid4(),
            method_id="child",
            requirements=(PermissionGrant.model_validate(raw),),
        )


def test_recovery_rechecks_parent_and_never_expands_frozen_child():
    root = parent()
    leaf = child(root)
    raw = root.model_dump(mode="json")
    raw["revision"] += 1
    raw["grants"][0]["requirement"]["resources"][0]["values"].append("new@example.test")
    widened = MethodAuthorization.model_validate(raw)
    check_delegation(widened, leaf)
    assert leaf.grants[0].requirement.resources[0].values == ("test@example.test",)
    raw["grants"][0]["requirement"]["resources"][0]["values"] = ["new@example.test"]
    with pytest.raises(CapabilityError):
        check_delegation(MethodAuthorization.model_validate(raw), leaf)
    with pytest.raises(CapabilityError):
        check_delegation(root, leaf, now=datetime.now(UTC) + timedelta(days=1))
    with pytest.raises(CapabilityError):
        check_delegation(root.model_copy(update={"call_id": uuid4()}), leaf)


def test_empty_scope_still_expires_and_cannot_gain_an_account():
    root = parent().model_copy(update={"grants": ()})
    leaf = child(root)
    with pytest.raises(CapabilityError):
        check_delegation(root, leaf, now=datetime.now(UTC) + timedelta(days=1))
    with pytest.raises(CapabilityError):
        delegate(root, call_id=uuid4(), method_id="child", requirements=parent().grants)


def test_direct_confirmation_is_for_one_call_and_cannot_delegate_to_a_grandchild():
    root = parent()
    recipient = uuid4()
    direct = root.grants[0].model_copy(
        update={
            "delegable": False,
            "recipient_call_id": recipient,
        }
    )
    root = root.model_copy(update={"grants": (direct,)})
    leaf = delegate(root, call_id=recipient, method_id="child", requirements=(direct,))
    check_delegation(root, leaf)
    for requirements in (
        leaf.grants,
        (direct.model_copy(update={"delegable": True, "recipient_call_id": None}),),
        (direct.model_copy(update={"recipient_call_id": uuid4()}),),
    ):
        with pytest.raises(CapabilityError):
            delegate(
                leaf, call_id=uuid4(), method_id="child", requirements=requirements
            )
    with pytest.raises(CapabilityError):
        check_delegation(root.model_copy(update={"revoked": True}), leaf)
    with pytest.raises(CapabilityError):
        check_delegation(root.model_copy(update={"grants": ()}), leaf)


def test_explicit_resource_delegation_may_narrow_to_a_direct_recipient():
    root = parent()  # An already verified resource-owner grant, not user selection.
    recipient = uuid4()
    direct = root.grants[0].model_copy(
        update={
            "delegable": False,
            "recipient_call_id": recipient,
        }
    )
    leaf = delegate(root, call_id=recipient, method_id="child", requirements=(direct,))
    check_delegation(root, leaf)
    with pytest.raises(CapabilityError):
        delegate(leaf, call_id=uuid4(), method_id="child", requirements=(direct,))


def test_legacy_grant_without_recipient_is_not_an_implicit_delegation():
    raw = parent().grants[0].model_dump(mode="json")
    raw.pop("recipient_call_id")
    with pytest.raises(ValueError):
        PermissionGrant.model_validate(raw)
