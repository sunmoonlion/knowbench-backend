"""PR-2 KnowBench tests: multi-hop FormalTreeDelegationBinding, safe capability vector & M05 delivery."""

from __future__ import annotations

from copy import deepcopy
from datetime import UTC, datetime, timedelta
from pathlib import Path
from uuid import UUID, uuid4

import pytest
from method_access_support import ACCESS

from app.application.workbench.formal_methods import FormalMethods
from app.domain.workbench.capabilities import CapabilityError
from app.domain.workbench.errors import Forbidden
from app.domain.workbench.formal_method_execution_generated import fingerprint
from app.domain.workbench.method_authorization import (
    MethodAuthorization,
    PermissionGrant,
    check_delegation,
    delegate,
)
from app.domain.workbench.resource_delegation import (
    FormalTreeDelegationBinding,
    ResourceDelegation,
    ResourceDelegationCatalog,
)

NOW = datetime(2030, 1, 1, tzinfo=UTC)
OWNER = UUID("11111111-1111-4111-8111-111111111111")
CONN = UUID("22222222-2222-4222-8222-222222222222")


def test_safe_capability_vector_migration_guards_every_jsonb_expander():
    mig = (
        Path(__file__).resolve().parents[1]
        / "alembic/versions/20261126_0063_safe_capability_vector.py"
    ).read_text()
    upgrade_sql = mig.split("def downgrade():")[0]
    for fn in (
        "jsonb_each(",
        "jsonb_each_text(",
        "jsonb_array_elements(",
        "jsonb_array_elements_text(",
    ):
        for chunk in upgrade_sql.split(fn)[1:]:
            arg = chunk.split(")")[0]
            assert "CASE WHEN jsonb_typeof(" in arg, f"Unguarded {fn} in 0063: {arg}"
    assert "{meaning,domain_id}" in upgrade_sql


def test_formal_call_tree_multi_hop_delegation_decrements_depth_and_stops_at_leaf():
    binding = FormalTreeDelegationBinding(
        root_method_id="expert-root",
        root_method_version="1",
        root_definition_digest="sha256:" + "a" * 64,
        allowed_method_ids=("expert-root", "sub-expert", "leaf-tool"),
    )
    record = ResourceDelegation.model_validate(
        {
            "record_id": "tree-authority-3hop",
            "revision": 1,
            "issuer": "test:resource-owner",
            "owner_actor_id": str(OWNER),
            "binding": binding.model_dump(mode="json"),
            "connection_id": str(CONN),
            "connection_revision": 1,
            "operation_digest": "b" * 64,
            "requirement": deepcopy(ACCESS),
            "max_delegation_depth": 2,
            "status": "active",
            "valid_from": (NOW - timedelta(days=1)).isoformat(),
            "expires_at": (NOW + timedelta(days=1)).isoformat(),
            "evidence": {
                "reference": "test:tree-review",
                "digest": "sha256:" + "c" * 64,
            },
        }
    )
    catalog = ResourceDelegationCatalog((record,))
    assert (
        catalog.require(
            record.reference,
            OWNER,
            binding=binding,
            connection_id=CONN,
            connection_revision=1,
            operation_digest="b" * 64,
            requirement=record.requirement,
            now=NOW,
        )
        == record
    )

    root_grant = PermissionGrant(
        grant_id="tree-grant",
        confirmation_context="d" * 64,
        connection_id=CONN,
        connection_revision=1,
        operation_digest="b" * 64,
        requirement=record.requirement,
        delegable=True,
        recipient_call_id=None,
        expires_at=record.expires_at,
        method_ids=binding.allowed_method_ids,
        resource_authority=record.reference,
        remaining_delegation_depth=record.max_delegation_depth,
    )
    root_auth = MethodAuthorization(
        call_id=uuid4(),
        revision=1,
        parent_call_id=None,
        parent_revision=None,
        method_id="expert-root",
        revoked=False,
        expires_at=record.expires_at,
        grants=(root_grant,),
    )
    # Hop 1 -> intermediate sub-expert: depth 2 -> 1, still delegable.
    sub_call = uuid4()
    sub_req = root_grant.model_copy(update={"method_ids": ("sub-expert", "leaf-tool")})
    sub_auth = delegate(
        root_auth,
        call_id=sub_call,
        method_id="sub-expert",
        requirements=(sub_req,),
        intermediate=True,
    )
    check_delegation(root_auth, sub_auth, now=NOW)
    assert sub_auth.grants[0].delegable is True
    assert sub_auth.grants[0].remaining_delegation_depth == 1

    # Hop 2 -> leaf-tool: depth 1 -> direct recipient, no longer delegable.
    leaf_call = uuid4()
    leaf_req = sub_auth.grants[0].model_copy(update={"method_ids": ("leaf-tool",)})
    leaf_auth = delegate(
        sub_auth,
        call_id=leaf_call,
        method_id="leaf-tool",
        requirements=(leaf_req,),
        intermediate=False,
    )
    check_delegation(sub_auth, leaf_auth, now=NOW)
    assert leaf_auth.grants[0].delegable is False
    assert leaf_auth.grants[0].recipient_call_id == leaf_call

    # Hop 3 -> blocked!
    with pytest.raises(CapabilityError):
        delegate(
            leaf_auth,
            call_id=uuid4(),
            method_id="leaf-tool",
            requirements=leaf_auth.grants,
        )


@pytest.mark.asyncio
async def test_check_delivery_source_blocks_standalone_script_but_allows_verified_internal_leaf():
    import hashlib

    from test_formal_method_admission_db import validation_request

    from app.domain.workbench.formal_method_admission_generated import (
        CONTRACT,
        GOVERNANCE_CHECKS,
        FormalMethodValidationReceipt,
        FrozenFormalMethod,
    )

    source_id = str(uuid4())
    content = "print(1)\n"
    real_digest = "sha256:" + hashlib.sha256(content.encode()).hexdigest()
    req = validation_request(
        {
            "asset_id": None,
            "asset_revision": None,
            "kind": "registered_script",
            "source": source_id,
            "version": "1",
            "digest": real_digest,
            "entry": "calc.py",
            "license_basis": "owner",
        }
    )
    evidence = {
        "adapter": "knowbench/formal-script-host/v1",
        "cases": [],
    }
    assert req.draft.source is not None
    receipt = FormalMethodValidationReceipt.model_validate(
        {
            "contract": CONTRACT,
            "run_id": req.run_id,
            "owner_subject": req.owner_subject,
            "request_id": req.request_id,
            "workdraft_id": req.workdraft_id,
            "revision": req.revision,
            "method_id": req.method_id,
            "definition_digest": req.draft.definition.digest(),
            "source_digest": req.draft.source.fingerprint(),
            "dependencies": [],
            "checks": sorted(GOVERNANCE_CHECKS | {"business"}),
            "uncovered": [],
            "completed_at": datetime.now(UTC),
            "execution": {
                "contract": "knowlab/formal-method-execution/v1",
                "suite_id": str(uuid4()),
                "suite_version": 1,
                "suite_digest": "sha256:" + "1" * 64,
                "request_digest": "sha256:" + "2" * 64,
                "evidence_digest": fingerprint(evidence),
                "validated_callers": ["host"],
            },
        }
    )
    leaf2 = FrozenFormalMethod.model_validate(
        {
            "contract": "knowlab/frozen-formal-method/v1",
            "owner_subject": req.owner_subject,
            "reference": {
                "method_id": req.method_id,
                "version": 1,
                "digest": req.draft.definition.digest(),
            },
            "draft": req.draft,
            "validation": receipt,
            "visibility": "private",
        }
    )

    class RepoWithContent:
        async def formal_validation(self, owner, run_id):
            return {"execution_evidence": evidence}

        async def direct_revision(self, owner, sid, version, dig):
            return {
                "source_id": UUID(sid),
                "version": version,
                "kind": "script",
                "name": "calc.py",
                "digest": real_digest,
                "content": content,
            }

    svc = FormalMethods(RepoWithContent())
    with pytest.raises(Forbidden):
        await svc.check_delivery_source(
            str(OWNER), leaf2, market=True, internal_dependency=False
        )
    await svc.check_delivery_source(
        str(OWNER), leaf2, market=True, internal_dependency=True
    )
