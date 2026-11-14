"""Synthetic operator evidence only; these tests establish no real source rights."""

import json
from datetime import UTC, datetime, timedelta

import pytest
from pydantic import ValidationError

from app.domain.workbench.source_licenses import (
    SourceIdentity,
    SourceLicense,
    SourceLicenseCatalog,
)
from core.config import Settings

NOW = datetime(2030, 1, 1, tzinfo=UTC)
SOURCE = {
    "kind": "skill_instruction",
    "source": "skill:2541c3d4-71aa-47b9-8138-c7ef9385cf61",
    "version": "1",
    "digest": "sha256:" + "a" * 64,
    "entry": "SKILL.md",
}


def license_record(source=None, **changes):
    """A made-up review record, never a deployed license fixture."""
    source = SOURCE if source is None else source
    return SourceLicense.model_validate(
        {
            "record_id": "synthetic-test-only",
            "revision": 1,
            "subject": "author",
            "source": {k: source[k] for k in SOURCE},
            "rights": {"execute": True, "deliver": False},
            "status": "active",
            "valid_from": "2020-01-01T00:00:00Z",
            "expires_at": "2100-01-01T00:00:00Z",
            "evidence": {
                "reference": "test:synthetic-source-review",
                "digest": "sha256:" + "b" * 64,
            },
            **changes,
        }
    )


def basis(record=None, *, subject="author", source=None, now=NOW):
    return SourceLicenseCatalog((record or license_record(),)).execution_basis(
        subject, SourceIdentity.model_validate(source or SOURCE), now=now
    )


def test_empty_catalog_is_not_authority():
    assert Settings(_env_file=None).workbench_source_licenses == ()
    assert (
        SourceLicenseCatalog().execution_basis(
            "author", SourceIdentity.model_validate(SOURCE), now=NOW
        )
        == "pending"
    )


@pytest.mark.parametrize(
    "changes",
    [
        {"kind": "skill_script"},
        {"source": SOURCE["source"] + "-other"},
        {"version": "2"},
        {"digest": "sha256:" + "c" * 64},
        {"entry": "scripts/run.py"},
    ],
)
def test_all_five_source_fields_must_match(changes):
    assert basis(source={**SOURCE, **changes}) == "pending"


@pytest.mark.parametrize("subject", ["other", "Author", "author/child"])
def test_subject_is_exact(subject):
    assert basis(subject=subject) == "pending"


@pytest.mark.parametrize(
    "changes",
    [
        {"status": "revoked"},
        {"rights": {"execute": False, "deliver": True}},
        {"rights": {"execute": False, "deliver": False}},
        {"valid_from": NOW + timedelta(seconds=1)},
        {"expires_at": NOW},
        {"expires_at": NOW - timedelta(seconds=1)},
    ],
)
def test_inactive_or_delivery_only_record_cannot_authorize_execution(changes):
    assert basis(license_record(**changes)) == "pending"


def test_execution_right_starts_inclusively_and_does_not_require_delivery():
    record = license_record(valid_from=NOW)
    assert basis(record) == record.basis
    assert record.rights.execute and not record.rights.deliver


@pytest.mark.parametrize(
    "changes",
    [
        {"revision": 2},
        {"rights": {"execute": True, "deliver": True}},
        {"expires_at": "2099-01-01T00:00:00Z"},
        {"evidence": {"reference": "test:changed", "digest": "sha256:" + "b" * 64}},
        {
            "evidence": {
                "reference": "test:synthetic-source-review",
                "digest": "sha256:" + "c" * 64,
            }
        },
    ],
)
def test_basis_changes_even_if_operator_forgets_to_increment_revision(changes):
    assert license_record(**changes).basis != license_record().basis


@pytest.mark.parametrize(
    "changes",
    [
        {"subject": "*"},
        {"subject": "author "},
        {"revision": True},
        {"revision": 0},
        {"status": "approved"},
        {"rights": {"execute": True}},
        {"rights": {"execute": "true", "deliver": False}},
        {"rights": {"execute": True, "deliver": False, "delegate": True}},
        {"valid_from": "2020-01-01T00:00:00"},
        {"expires_at": "2020-01-01T00:00:00Z"},
        {
            "evidence": {
                "reference": "I have permission",
                "digest": "sha256:" + "b" * 64,
            }
        },
        {
            "evidence": {
                "reference": "https://user:secret@example.test/proof",
                "digest": "sha256:" + "b" * 64,
            }
        },
        {"evidence": {"reference": "test:proof", "digest": "unknown"}},
        {"source": {**SOURCE, "digest": "a" * 64}},
        {"source": {**SOURCE, "kind": "platform"}},
        {"verified_by_browser": True},
    ],
)
def test_malformed_or_unsupported_evidence_is_rejected(changes):
    with pytest.raises(ValidationError):
        license_record(**changes)


@pytest.mark.parametrize(
    "other",
    [
        {},
        {"revision": 2},
        {"record_id": "another-review", "status": "revoked"},
        {"subject": "other"},
    ],
)
def test_conflicts_fail_settings_validation_instead_of_first_match_wins(other):
    with pytest.raises(ValidationError, match="conflicting"):
        Settings(
            _env_file=None,
            workbench_source_licenses=(license_record(), license_record(**other)),
        )


def test_environment_catalog_round_trip(monkeypatch):
    record = license_record()
    monkeypatch.setenv(
        "WORKBENCH_SOURCE_LICENSES", json.dumps([record.model_dump(mode="json")])
    )
    assert Settings(_env_file=None).workbench_source_licenses == (record,)
    assert "test:synthetic-source-review" not in record.basis
