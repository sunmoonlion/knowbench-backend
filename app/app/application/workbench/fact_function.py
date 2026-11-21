"""Dispatch the registered KnowBase fact query with host-bound data scope."""

from uuid import uuid4

from app.application.workbench.method_calls import root_call
from app.domain.workbench.capabilities import (
    CapabilityError,
    CapabilityOutcomeUnknown,
    digest,
)
from app.domain.workbench.errors import (
    ComputeRefused,
    RecordsRefused,
    UserDataUnavailable,
)
from app.domain.workbench.fact_function import FACT_QUERIES, descriptor, destination
from app.domain.workbench.states import TASK_TERMINAL
from app.domain.workbench.tool_registry import ToolRegistration
from app.domain.workbench.tool_schema import schema_validator


class FactFunction:
    def __init__(self, client, issuer):
        self.client, self.issuer = client, issuer

    async def invoke(self, revision, *, subject, sandbox, call_id, arguments):
        if (
            (revision.provider, revision.version) != ("knowbase", 1)
            or revision.name not in FACT_QUERIES
            or revision.contract.operation != revision.name
            or revision.contract.required_permissions != ["facts.read"]
        ):
            raise CapabilityError("Unsupported fact query binding")
        if not schema_validator(revision.contract.input_schema).is_valid(arguments):
            raise CapabilityError("Invalid registered function input")
        ticket = self.issuer.function_ticket(
            subject, sandbox, call_id, revision.digest, digest(arguments)
        )
        body = {
            "call_id": str(call_id),
            "digest": revision.digest,
            "arguments": arguments,
        }
        receipt = await self.client.invoke_fact_query(revision.name, ticket.token, body)
        if (
            not isinstance(receipt, dict)
            or receipt.get("call_id") != str(call_id)
            or receipt.get("digest") != revision.digest
            or not schema_validator(revision.contract.output_schema).is_valid(
                receipt.get("output")
            )
        ):
            raise CapabilityOutcomeUnknown("Unverified KnowBase function receipt")
        return receipt

    async def read_task_refs(self, repo, task, run_ref, refs, parent_call_id):
        """Fixed input to compute and sourced acceptance. Only the host supplies scope."""
        owner = str(task["owner_actor_id"])
        task_id, session_id = str(task["id"]), str(task["session_id"])
        args = {"scope": "run", "run": run_ref, "refs": refs}
        subject = await repo.subject_of_actor(owner)
        if not subject:
            raise RecordsRefused("this account is not known to the data service")
        call_id = str(uuid4())
        try:
            async with repo.transaction():
                revisions = await repo.scene_function_revisions(
                    {"read_facts": ("knowbase", 1)}, scene="step"
                )
                if not revisions:
                    raise UserDataUnavailable(
                        "read_facts v1 is unavailable for this step"
                    )
                revision = revisions[0]
                tool = ToolRegistration.discover(
                    "service_function", destination(revision.name), descriptor(revision)
                )
                if not schema_validator(revision.contract.input_schema).is_valid(args):
                    raise ComputeRefused(
                        "Fact references do not match the registered read contract"
                    )
                session = await repo.get_session(session_id, owner_actor_id=owner)
                active = await repo.get_task(
                    task_id, owner_actor_id=owner, for_update=True
                )
                if (
                    str(session.get("active_task_id")) != task_id
                    or active["state"] in TASK_TERMINAL
                    or active.get("active_attempt_id") != task.get("active_attempt_id")
                ):
                    raise RecordsRefused("The task changed before its fact read")
                parent = parent_call_id or (await root_call(repo, active))["id"]
                detail = {
                    "tool": tool.descriptor(),
                    "operation": {
                        "operation": revision.name,
                        "effect": "read",
                        "registered_source": revision.model_dump(exclude={"contract"}),
                        "arguments_digest": digest(args),
                    },
                    "identity_basis": "host_task_data_read",
                    "call_id": call_id,
                }
                await repo.add_call(
                    {
                        "id": call_id,
                        "call_key": "host-read:" + call_id,
                        "kind": "tool",
                        "origin": "platform",
                        "owner_actor_id": owner,
                        "session_id": session_id,
                        "task_id": task_id,
                        "parent_call_id": parent,
                        "attempt_id": task.get("active_attempt_id"),
                        "registration_id": tool.tool_id,
                        "registration_digest": tool.descriptor_digest,
                        "digest": digest(detail),
                        "status": "dispatching",
                        "detail": detail,
                    }
                )
        except CapabilityError as exc:
            raise UserDataUnavailable("Invalid registered fact reader") from exc
        try:
            receipt = await self.invoke(
                revision,
                subject=subject,
                sandbox=str(session["sandbox_id"]),
                call_id=call_id,
                arguments=args,
            )
        except Exception as exc:
            status = (
                "failed"
                if isinstance(exc, CapabilityError)
                and not isinstance(exc, CapabilityOutcomeUnknown)
                else "unknown"
            )
            async with repo.transaction():
                await repo.update_call(owner, call_id, status, detail)
            raise UserDataUnavailable(
                "The registered fact reader could not confirm the result"
            ) from exc
        async with repo.transaction():
            await repo.update_call(
                owner,
                call_id,
                "succeeded",
                detail
                | {
                    "function_receipt": {
                        "call_id": call_id,
                        "digest": revision.digest,
                        "output_digest": digest(receipt["output"]),
                    }
                },
            )
        return receipt["output"]
