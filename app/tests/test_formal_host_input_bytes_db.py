"""Use the same UTF-8 byte count and admitted slot for both host inputs."""

from test_formal_host_calls_db import setup
from test_workbench_ledger_db import db as db

from app.domain.workbench.formal_execution import FormalTaskSnapshot, validate_values
from app.domain.workbench.formal_host import check_host_inputs
from app.domain.workbench.formal_method_execution_generated import FormalCaseInput
from app.infrastructure.workbench.repository import WorkbenchRepository


async def test_host_and_suite_count_identical_utf8_bytes(db):
    _, _, _, _, task, _ = await setup(db)
    async with db() as session:
        document = (await WorkbenchRepository(session).get_task(task))["pack_snapshot"]
    for sample in ("a" * 33000, "甲" * 11000):
        values = FormalCaseInput(values={"to": sample}).values
        snapshot = FormalTaskSnapshot.model_validate({**document, "arguments": values})
        validate_values(snapshot.draft.definition.procedure.inputs, values)
        check_host_inputs(snapshot)
