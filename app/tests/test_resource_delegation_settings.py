"""Configuration does not invent delegation rights or install real review records."""

import json

import pytest
from pydantic import ValidationError
from test_resource_delegation import OTHER, delegation_record

from core.config import Settings


def settings(**values):
    # BaseSettings accepts this runtime keyword; its generated field signature
    # does not include private environment-source constructor arguments.
    return Settings(**{"_env_file": None, **values})


def test_resource_delegation_configuration_defaults_to_empty(monkeypatch):
    monkeypatch.delenv("WORKBENCH_RESOURCE_DELEGATIONS", raising=False)
    assert settings().workbench_resource_delegations == ()


def test_resource_delegation_environment_round_trip(monkeypatch):
    record = delegation_record()
    monkeypatch.setenv(
        "WORKBENCH_RESOURCE_DELEGATIONS", json.dumps([record.model_dump(mode="json")])
    )
    configured = settings().workbench_resource_delegations
    assert configured == (record,)
    assert configured[0].reference == record.reference


@pytest.mark.parametrize(
    "change",
    [
        {},
        {"revision": 2},
        {"owner_actor_id": str(OTHER)},
        {"status": "revoked"},
        {"issuer": "test:another-reviewer"},
    ],
)
def test_duplicate_identity_fails_configuration_instead_of_selecting_first(change):
    with pytest.raises(ValidationError, match="Duplicate"):
        settings(
            workbench_resource_delegations=(
                delegation_record(),
                delegation_record(**change),
            ),
        )


def test_distinct_review_records_remain_explicit_options_not_one_merged_grant():
    records = (delegation_record(), delegation_record(record_id="second-review"))
    configured = settings(workbench_resource_delegations=records)
    assert configured.workbench_resource_delegations == records
    assert records[0].reference != records[1].reference


def test_environment_rejects_browser_self_certification(monkeypatch):
    record = delegation_record().model_dump(mode="json")
    record["browser_confirmed"] = True
    monkeypatch.setenv("WORKBENCH_RESOURCE_DELEGATIONS", json.dumps([record]))
    with pytest.raises(ValidationError):
        settings()


def test_resource_delegation_catalog_has_a_bounded_record_count():
    with pytest.raises(ValidationError, match="1000"):
        settings(workbench_resource_delegations=(delegation_record(),) * 1001)
