"""Method output limits apply even when its declared JSON schema is permissive."""

from types import SimpleNamespace
from unittest.mock import AsyncMock
from uuid import uuid4

import pytest
from test_formal_method_admission_db import validation_request

from app.application.workbench.formal_method_calls import FormalMethodCalls
from app.domain.workbench.formal_method_admission_generated import FormalMethodDraft
from app.domain.workbench.formal_method_generated import MethodRef


@pytest.mark.parametrize("overflow", ["depth", "size"])
async def test_projection_bounds_schema_valid_output_before_public_dto(overflow):
    asset = str(uuid4())
    draft = validation_request(
        {
            "kind": "mcp",
            "source": "https://example.test/mcp",
            "version": "1",
            "digest": "sha256:" + "a" * 64,
            "entry": "send",
            "asset_id": asset,
            "asset_revision": 1,
            "license_basis": "pending",
        }
    ).draft.model_dump(mode="json", by_alias=True)
    draft["definition"]["procedure"]["outputs"]["receipt"]["schema"] = {}
    frozen_draft = FormalMethodDraft.model_validate(draft)
    value = "x" * 64000
    if overflow == "depth":
        value = "receipt"
        for _ in range(33):
            value = {"nested": value}
    root_id, child_id, request_id = str(uuid4()), str(uuid4()), str(uuid4())
    child = {
        "id": child_id,
        "status": "succeeded",
        "source_asset_id": asset,
        "connection_id": asset,
        "detail": {"result": {"structuredContent": {"receipt": value}}},
    }
    repo = SimpleNamespace(
        capability_call=AsyncMock(return_value=child), update_call=AsyncMock()
    )
    service = object.__new__(FormalMethodCalls)
    service.repo = repo
    frozen = SimpleNamespace(
        draft=frozen_draft,
        reference=MethodRef(
            method_id=uuid4(), version=1, digest=frozen_draft.definition.digest()
        ),
    )
    service._check_pair = lambda *_: frozen
    root = {
        "id": root_id,
        "status": "dispatching",
        "detail": {"child_call_id": child_id, "request": {"request_id": request_id}},
    }
    result = await service._project("owner", "session", root)
    assert result["status"] == "failed" and result["outputs"] is None
    assert result["call"]["status"] == "succeeded"
    assert result["call"]["detail"]["result"]["structuredContent"]["receipt"] == value
    assert repo.update_call.await_args.args[2] == "failed"
    assert repo.update_call.await_args.args[3]["projection"]["outputs"] is None
