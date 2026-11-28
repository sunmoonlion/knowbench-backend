"""Two real approved trial executions with a synthetic local MCP response."""

import pytest
from test_formal_execution_db import StructuredRemote, approve, setup, status
from test_workbench_ledger_db import db as db

from app.domain.workbench.formal_method_admission_generated import FrozenFormalMethod
from app.infrastructure.workbench.repository import WorkbenchRepository


class AlternatingRemote(StructuredRemote):
    async def call(self, url, token, name, arguments):
        response = await super().call(url, token, name, arguments)
        response["structuredContent"]["receipt"] = "A" if len(self.calls) == 1 else "B"
        return response


@pytest.mark.asyncio
async def test_partially_passing_trial_keeps_both_verified_outputs(db):
    case = dict(
        case_id="notice",
        input={"values": {"to": "test@example.test"}},
        repetitions=2,
        minimum_passes=1,
        expected={
            "outcome": "succeeded",
            "assertions": [{"kind": "equals", "pointer": "/receipt", "value": "A"}],
        },
    )
    request, _, remote, factory, conductor = await setup(
        db, cases=[case], remote=AlternatingRemote()
    )
    for _ in range(16):
        await conductor.advance(str(request.run_id))
        result = await status(db, request, factory)
        if result["actions"]:
            await approve(db, request, factory, result)
        if result["status"] in {"passed", "failed", "cancelled"}:
            break
    assert len(remote.calls) == 2
    assert result["status"] == "passed"
    assert sorted(c["status"] for c in result["cases"]) == ["failed", "passed"]
    frozen = FrozenFormalMethod.model_validate(
        dict(
            contract="knowlab/frozen-formal-method/v1",
            owner_subject=request.owner_subject,
            reference=dict(
                method_id=request.method_id,
                version=1,
                digest=request.draft.definition.digest(),
            ),
            draft=request.draft,
            validation=result["admission"]["receipt"],
            visibility="private",
        )
    )
    async with db() as session:
        svc = factory(WorkbenchRepository(session)).formal_sources
        await svc.register(frozen, caller="knowlab")
        review = await svc.self_review_outputs(request.owner_subject, frozen.reference)
        assert [
            (s["status"], s["steps"][0]["artifact"]["receipt"])
            for s in review["samples"]
        ] == [("passed", "A"), ("failed", "B")]
