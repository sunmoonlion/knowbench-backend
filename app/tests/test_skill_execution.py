from copy import deepcopy

import pytest

from app.domain.workbench.skill_execution import public_call


@pytest.mark.parametrize("script", [True, False])
def test_receipt_distinguishes_asset_from_account_without_mutating_approved_record(
    script,
):
    raw = {
        "id": "call-id",
        "connection_id": "asset-id",
        "digest": "unchanged-approval-digest",
        "detail": {
            "connection_id": "asset-id",
            **({"execution_kind": "skill_script"} if script else {}),
            "result": {"artifacts": [{"path": "output.txt", "data_base64": "NDI="}]},
        },
    }
    before = deepcopy(raw)
    result = public_call(raw)
    assert (
        result["source_asset_id"] == result["detail"]["source_asset_id"] == "asset-id"
    )
    assert (
        result["connection_id"]
        == result["detail"]["connection_id"]
        == (None if script else "asset-id")
    )
    assert "data_base64" not in result["detail"]["result"]["artifacts"][0]
    assert result["digest"] == raw["digest"]
    assert raw == before
