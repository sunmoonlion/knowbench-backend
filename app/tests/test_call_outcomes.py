"""Only a verified service receipt can be recorded as a successful data function."""

from types import SimpleNamespace
from uuid import uuid4

import pytest
from test_service_function_management_db import revision
from test_workbench_mcp_routes import ISSUER

from app.application.workbench.data_function import DataFunction
from app.domain.workbench.capabilities import CapabilityOutcomeUnknown


@pytest.mark.parametrize("fault", ["identity", "digest", "output", "missing"])
async def test_registered_receipt_is_bound_to_call_digest_and_contract(fault):
    r = revision(output_schema={"type": "object", "required": ["facts"]})

    async def invoke(name, token, body):
        result = {
            "call_id": body["call_id"],
            "digest": body["digest"],
            "output": {"facts": []},
        }
        if fault == "missing":
            return {}
        result[
            {"identity": "call_id", "digest": "digest", "output": "output"}[fault]
        ] = None
        return result

    with pytest.raises(CapabilityOutcomeUnknown):
        await DataFunction(SimpleNamespace(invoke_data_query=invoke), ISSUER).invoke(
            r,
            subject="actor",
            sandbox="session",
            call_id=str(uuid4()),
            arguments={"scope": "run"},
        )


@pytest.mark.parametrize(
    "field", ["sha256", "material_id", "method_call_id", "grant_id"]
)
def test_material_receipt_must_match_the_approved_leaf(field):
    import hashlib

    from app.application.workbench.knowbase_method_read import verify_material

    material, leaf = str(uuid4()), str(uuid4())
    scope = {"material_id": material, "version_no": 1, "path": [{"call_id": leaf}]}
    source = {
        "material_id": material,
        "version_no": 1,
        "method_call_id": leaf,
        "grant_id": str(uuid4()),
        "sha256": hashlib.sha256(b"report").hexdigest(),
    }
    with pytest.raises(CapabilityOutcomeUnknown):
        verify_material(
            {"text": "report", "source": {**source, field: "changed"}}, scope
        )
