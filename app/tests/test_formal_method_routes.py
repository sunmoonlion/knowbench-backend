"""Formal-method HTTP boundaries with real auth/application code and an in-memory repo.

No shared database, external connector or paid model is used. Validation intake
currently persists pending evidence requirements; these tests do not manufacture
a successful executor or claim that incubation/runtime validation is complete.
"""

from __future__ import annotations

from contextlib import asynccontextmanager
from copy import deepcopy
from datetime import UTC, datetime
from typing import Any

import pytest
from httpx import ASGITransport, AsyncClient

from app.application.errors.exceptions import UnauthorizedError
from app.application.workbench.formal_methods import FormalMethods
from app.bootstrap.api import create_app
from app.domain.workbench.errors import NotFound
from app.domain.workbench.formal_method_admission_generated import (
    CONTRACT,
    FormalMethodValidationRequest,
)
from app.infrastructure.security import relation_auth
from app.infrastructure.security.relation_auth import RelationServiceVerifier
from app.interfaces.endpoints import knowbot_runtime_routes
from core.config import Settings

BASE = "/api/internal/v1/knowbot-runtime"
OWNER = "ed88a860-adba-44bc-8dcd-500f919d08e0"
OTHER = "fe939feb-ddc5-4ed8-9771-ce102027f18b"
ASSET = "2541c3d4-71aa-47b9-8138-c7ef9385cf61"
METHOD = "8c4040dc-004d-43e7-9826-26b76d09a9f5"
RUN = "d2ca1514-1b66-4c9b-86d0-ae4fa6fa9c7e"
REQUEST = "f583e1ec-23e1-4ad8-9bc0-03f0cc3e0cba"
WORKDRAFT = "6a103785-900d-46f6-a31c-ec8b73514d8e"
HEADERS = {"Authorization": "Bearer author-service"}


class MemoryRepository:
    """Repository substitute, not a substitute for auth or the application service."""

    def __init__(self):
        self.accesses = 0
        self.assets = {
            ASSET: {
                "id": ASSET,
                "owner_actor_id": OWNER,
                "kind": "skill",
                "revision": 1,
                "status": "active",
                "credentials_ciphertext": "must-not-leak",
                "detail": {
                    "digest": "a" * 64,
                    "version": "1",
                    "files": [],
                    "content": "Synthetic instruction, not runtime evidence.",
                },
            }
        }
        self.validations: dict[str, dict[str, Any]] = {}
        self.methods: dict[tuple[str, int], dict[str, Any]] = {}

    @asynccontextmanager
    async def transaction(self):
        self.accesses += 1
        yield self

    async def actor_for_subject(self, subject):
        return {"author": OWNER, "other": OTHER}.get(subject)

    async def capability_assets(self, owner):
        return [a for a in self.assets.values() if a["owner_actor_id"] == owner]

    async def capability_asset(self, owner, asset_id, *, lock=False):
        found = self.assets.get(asset_id)
        if found is None or found["owner_actor_id"] != owner:
            raise NotFound("No such asset")
        return found

    async def formal_validation(self, owner, run_id, *, lock=False):
        found = self.validations.get(run_id)
        return found if found and found["owner_actor_id"] == owner else None

    async def insert_formal_validation(self, record):
        self.validations[record["run_id"]] = deepcopy(record)
        return self.validations[record["run_id"]]

    async def formal_method(self, owner, method_id, version):
        found = self.methods.get((method_id, version))
        return found if found and found["owner_actor_id"] == owner else None

    async def insert_formal_method(self, record):
        self.methods[(record["method_id"], record["version"])] = deepcopy(record)
        return record


def validation_body():
    source = {
        "asset_id": ASSET,
        "asset_revision": 1,
        "kind": "skill_instruction",
        "source": "skill:" + ASSET,
        "version": "1",
        "digest": "sha256:" + "a" * 64,
        "entry": "SKILL.md",
        "license_basis": "pending",
    }
    return {
        "contract": CONTRACT,
        "run_id": RUN,
        "owner_subject": "author",
        "request_id": REQUEST,
        "workdraft_id": WORKDRAFT,
        "revision": 1,
        "method_id": METHOD,
        "dependencies": [],
        "draft": {
            "source": source,
            "allowed_callers": ["host"],
            "definition": {
                "contract": "knowlab/formal-method/v1",
                "capability_declaration": {
                    "title": "Synthetic summary",
                    "purpose": "Summarize supplied text",
                    "applies_when": ["Text is supplied"],
                    "excludes": [],
                    "promises": {"summary": "A summary"},
                },
                "access_requirements": [],
                "procedure": {
                    "kind": "direct",
                    "implementation": {
                        key: source[key]
                        for key in ("kind", "source", "version", "digest", "entry")
                    }
                    | {"requirement_ids": []},
                    "inputs": {},
                    "outputs": {
                        "summary": {
                            "description": "Summary",
                            "schema": {"type": "string"},
                            "required": True,
                            "meaning": {},
                            "on_missing": "fail",
                        }
                    },
                    "instructions": "Produce the declared summary only",
                    "stop_conditions": ["Missing source text"],
                    "max_model_turns": 1,
                },
                "how_to_validate": [
                    {
                        "promise_id": "summary",
                        "outputs": ["summary"],
                        "check": "Nonempty summary",
                        "evidence": "Actual output",
                        "on_failure": "fail",
                    }
                ],
                "what_requires_approval": [],
            },
        },
    }


def claimed_frozen_body():
    """An adversarial well-shaped claim: it has no trusted persisted pass receipt."""
    request = FormalMethodValidationRequest.model_validate(validation_body())
    assert request.draft.source is not None
    fingerprint = request.draft.definition.digest()
    return {
        "contract": "knowlab/frozen-formal-method/v1",
        "reference": {"method_id": METHOD, "version": 1, "digest": fingerprint},
        "owner_subject": "author",
        "draft": request.draft.model_dump(mode="json", by_alias=True),
        "visibility": "private",
        "validation": {
            "contract": CONTRACT,
            "run_id": RUN,
            "owner_subject": "author",
            "request_id": REQUEST,
            "workdraft_id": WORKDRAFT,
            "revision": 1,
            "method_id": METHOD,
            "definition_digest": fingerprint,
            "source_digest": request.draft.source.fingerprint(),
            "dependencies": [],
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
            "uncovered": [],
            "completed_at": datetime.now(UTC).isoformat(),
        },
    }


class FakeOidc:
    """Only cryptographic token verification is stubbed; binding and scopes are real."""

    async def verify_access_token(self, encoded, *, audience):
        common = {"iss": "https://casdoor.test", "iat": 1, "exp": 4102444800}
        claims = {
            "author-service": {
                "aud": "lab-aud",
                "sub": "admin/lab",
                "scope": "knowbench:testrun",
            },
            "other-service": {
                "aud": "mart-aud",
                "sub": "admin/mart",
                "scope": "knowbench:testrun",
            },
            "wrong-scope": {
                "aud": "lab-aud",
                "sub": "admin/lab",
                "scope": "knowbench:incubate",
            },
            "browser-subject": {
                "aud": "lab-aud",
                "sub": "author",
                "scope": "knowbench:testrun",
            },
        }.get(encoded)
        if claims is None or claims["aud"] != audience:
            raise UnauthorizedError("Wrong audience", code="audience_mismatch")
        return common | claims


@pytest.fixture
def app_and_repo(monkeypatch):
    repo = MemoryRepository()
    svc = FormalMethods(repo)
    app = create_app(Settings())
    app.dependency_overrides[knowbot_runtime_routes.formal_methods] = lambda: svc
    env = {
        "SERVICE_AUTH_TESTRUN_CALLERS": "knowlab,knowmart",
        "SERVICE_AUTH_TESTRUN_KNOWLAB_AUDIENCE": "lab-aud",
        "SERVICE_AUTH_TESTRUN_KNOWLAB_SUBJECT_ALLOWLIST": "admin/lab",
        "SERVICE_AUTH_TESTRUN_KNOWLAB_REQUIRED_SCOPE": "knowbench:testrun",
        "SERVICE_AUTH_TESTRUN_KNOWMART_AUDIENCE": "mart-aud",
        "SERVICE_AUTH_TESTRUN_KNOWMART_SUBJECT_ALLOWLIST": "admin/mart",
        "SERVICE_AUTH_TESTRUN_KNOWMART_REQUIRED_SCOPE": "knowbench:testrun",
    }
    verifier = RelationServiceVerifier(
        "testrun", settings=Settings(), oidc_client=FakeOidc(), environ=env
    )
    monkeypatch.setattr(relation_auth, "relation_verifier", lambda _: verifier)
    return app, repo


@pytest.mark.parametrize(
    ("token", "status", "code"),
    [
        (None, 401, "auth_required"),
        ("other-service", 403, "formal_method_caller_denied"),
        ("wrong-scope", 403, "service_scope_denied"),
        ("browser-subject", 403, "service_subject_unbound"),
        ("unknown", 401, "audience_mismatch"),
    ],
)
async def test_formal_endpoints_authenticate_before_repository(
    app_and_repo, token, status, code
):
    app, repo = app_and_repo
    headers = {"Authorization": "Bearer " + token} if token else {}
    async with AsyncClient(
        transport=ASGITransport(app=app), base_url="http://127.0.0.1"
    ) as http:
        calls = [
            await http.get(
                f"{BASE}/users/author/formal-method-sources", headers=headers
            ),
            await http.post(
                f"{BASE}/formal-method-validations",
                json=validation_body(),
                headers=headers,
            ),
            await http.get(
                f"{BASE}/formal-method-validations/{RUN}?owner_subject=author",
                headers=headers,
            ),
            await http.post(
                f"{BASE}/formal-method-registrations",
                json=claimed_frozen_body(),
                headers=headers,
            ),
            await http.get(
                f"{BASE}/formal-method-registrations/{METHOD}/versions/1?owner_subject=author",
                headers=headers,
            ),
        ]
    for response in calls:
        assert response.status_code == status, response.text
        assert response.json()["code"] == code
    assert repo.accesses == 0


async def test_public_host_hides_formal_routes_even_from_valid_service(app_and_repo):
    app, repo = app_and_repo
    async with AsyncClient(
        transport=ASGITransport(app=app), base_url="http://testserver", headers=HEADERS
    ) as http:
        response = await http.post(
            f"{BASE}/formal-method-validations", json=validation_body()
        )
    assert response.status_code == 404
    assert repo.accesses == 0


@pytest.mark.parametrize("field", ["passed", "receipt", "method_kind", "visibility"])
async def test_validation_rejects_client_admission_claims(app_and_repo, field):
    app, repo = app_and_repo
    async with AsyncClient(
        transport=ASGITransport(app=app), base_url="http://127.0.0.1", headers=HEADERS
    ) as http:
        response = await http.post(
            f"{BASE}/formal-method-validations", json=validation_body() | {field: True}
        )
    assert response.status_code == 422
    assert repo.accesses == 0


@pytest.mark.parametrize("field", ["run_id", "request_id", "workdraft_id", "method_id"])
async def test_validation_rejects_invalid_uuid_fields(app_and_repo, field):
    app, repo = app_and_repo
    async with AsyncClient(
        transport=ASGITransport(app=app), base_url="http://127.0.0.1", headers=HEADERS
    ) as http:
        response = await http.post(
            f"{BASE}/formal-method-validations",
            json=validation_body() | {field: "not-a-uuid"},
        )
    assert response.status_code == 422
    assert not repo.validations


async def test_sources_only_list_current_author_assets_and_never_private_fields(
    app_and_repo,
):
    app, _ = app_and_repo
    async with AsyncClient(
        transport=ASGITransport(app=app), base_url="http://127.0.0.1", headers=HEADERS
    ) as http:
        own = await http.get(f"{BASE}/users/author/formal-method-sources")
        other = await http.get(f"{BASE}/users/other/formal-method-sources")
        absent = await http.get(f"{BASE}/users/unknown/formal-method-sources")
    assert own.status_code == 200
    assert own.headers["cache-control"] == "no-store"
    assert own.json()["items"][0]["asset_id"] == ASSET
    assert own.json()["items"][0]["license_basis"] == "pending"
    assert "must-not-leak" not in own.text
    assert "content" not in own.json()["items"][0]
    assert other.json() == {"items": []}
    assert absent.status_code == 404


async def test_validation_is_persisted_pending_idempotently_and_scoped_to_owner(
    app_and_repo,
):
    app, repo = app_and_repo
    async with AsyncClient(
        transport=ASGITransport(app=app), base_url="http://127.0.0.1", headers=HEADERS
    ) as http:
        first = await http.post(
            f"{BASE}/formal-method-validations", json=validation_body()
        )
        second = await http.post(
            f"{BASE}/formal-method-validations", json=validation_body()
        )
        current = await http.get(
            f"{BASE}/formal-method-validations/{RUN}?owner_subject=author"
        )
        other = await http.get(
            f"{BASE}/formal-method-validations/{RUN}?owner_subject=other"
        )
        changed = await http.post(
            f"{BASE}/formal-method-validations",
            json=validation_body() | {"revision": 2},
        )
    assert first.status_code == second.status_code == 202
    assert current.status_code == 200
    assert first.json() == second.json() == current.json()
    assert first.json()["status"] == "pending"
    assert first.json()["receipt"] is None
    assert {"authorization", "approval", "isolation", "outcome", "source"} <= set(
        first.json()["missing_checks"]
    )
    assert (
        first.headers["cache-control"] == current.headers["cache-control"] == "no-store"
    )
    assert other.status_code == 404
    assert changed.status_code == 409
    assert len(repo.validations) == 1
    assert FormalMethodValidationRequest.model_validate(
        repo.validations[RUN]["request"]
    ) == FormalMethodValidationRequest.model_validate(validation_body())


async def test_source_cannot_be_borrowed_by_another_owner(app_and_repo):
    app, repo = app_and_repo
    async with AsyncClient(
        transport=ASGITransport(app=app), base_url="http://127.0.0.1", headers=HEADERS
    ) as http:
        response = await http.post(
            f"{BASE}/formal-method-validations",
            json=validation_body() | {"owner_subject": "other"},
        )
    assert response.status_code == 404
    assert not repo.validations


async def test_well_shaped_frozen_claim_cannot_promote_a_pending_run(app_and_repo):
    app, repo = app_and_repo
    async with AsyncClient(
        transport=ASGITransport(app=app), base_url="http://127.0.0.1", headers=HEADERS
    ) as http:
        assert (
            await http.post(f"{BASE}/formal-method-validations", json=validation_body())
        ).status_code == 202
        response = await http.post(
            f"{BASE}/formal-method-registrations", json=claimed_frozen_body()
        )
    assert response.status_code == 403
    assert not repo.methods
    assert repo.validations[RUN]["result"]["status"] == "pending"


@pytest.mark.parametrize(
    "suffix",
    [
        "/formal-method-validations/not-a-uuid?owner_subject=author",
        f"/formal-method-validations/{RUN}",
        "/formal-method-registrations/not-a-uuid/versions/1?owner_subject=author",
        f"/formal-method-registrations/{METHOD}/versions/0?owner_subject=author",
        f"/formal-method-registrations/{METHOD}/versions/1",
    ],
)
async def test_formal_lookup_rejects_invalid_identity_or_missing_owner(
    app_and_repo, suffix
):
    app, repo = app_and_repo
    async with AsyncClient(
        transport=ASGITransport(app=app), base_url="http://127.0.0.1", headers=HEADERS
    ) as http:
        response = await http.get(BASE + suffix)
    assert response.status_code == 422
    assert repo.accesses == 0


async def test_missing_registration_is_404_and_projection_is_owner_and_caller_scoped(
    app_and_repo,
):
    app, repo = app_and_repo
    url = f"{BASE}/formal-method-registrations/{METHOD}/versions/1"
    async with AsyncClient(
        transport=ASGITransport(app=app), base_url="http://127.0.0.1", headers=HEADERS
    ) as http:
        assert (
            await http.get(url, params={"owner_subject": "author"})
        ).status_code == 404
        # Projection-only fixture: this does not assert that validate() can pass.
        repo.methods[(METHOD, 1)] = {
            "owner_actor_id": OWNER,
            "caller": "knowlab",
            "document": claimed_frozen_body(),
        }
        own = await http.get(url, params={"owner_subject": "author"})
        assert own.status_code == 200
        assert own.headers["cache-control"] == "no-store"
        assert own.json()["owner_subject"] == "author"
        assert own.json()["reference"]["method_id"] == METHOD
        assert (
            await http.get(url, params={"owner_subject": "other"})
        ).status_code == 404
        repo.methods[(METHOD, 1)]["caller"] = "knowmart"
        assert (
            await http.get(url, params={"owner_subject": "author"})
        ).status_code == 404
