"""Dispatch registered KnowBase data queries with host-bound data scope."""

from uuid import uuid4

from app.application.workbench.method_calls import root_call
from app.domain.workbench.capabilities import (
    CapabilityError,
    CapabilityOutcomeUnknown,
    digest,
)
from app.domain.workbench.data_function import (
    DATA_QUERIES,
    QUERY_PERMISSIONS,
    descriptor,
    destination,
)
from app.domain.workbench.errors import (
    ComputeRefused,
    RecordsRefused,
    UserDataUnavailable,
)
from app.domain.workbench.states import TASK_TERMINAL
from app.domain.workbench.tool_registry import ToolRegistration
from app.domain.workbench.tool_schema import schema_validator


class DataFunction:
    def __init__(self, client, issuer):
        self.client, self.issuer = client, issuer

    async def invoke(
        self, revision, *, subject, sandbox, call_id, arguments, method_read=None
    ):
        if (
            (revision.provider, revision.version) != ("knowbase", 1)
            or revision.name not in DATA_QUERIES
            or revision.contract.operation != revision.name
            or revision.contract.required_permissions
            != [QUERY_PERMISSIONS[revision.name]]
        ):
            raise CapabilityError("Unsupported data query binding")
        if not schema_validator(revision.contract.input_schema).is_valid(arguments):
            raise CapabilityError("Invalid registered function input")
        if (revision.name == "read_method_input") != (method_read is not None):
            raise CapabilityError(
                "Formal material reads require a verified method invocation"
            )
        ticket = self.issuer.function_ticket(
            subject,
            sandbox,
            call_id,
            revision.digest,
            digest(arguments),
            **({"method_read": method_read} if method_read is not None else {}),
        )
        body = {
            "call_id": str(call_id),
            "digest": revision.digest,
            "arguments": arguments,
        }
        receipt = await self.client.invoke_data_query(revision.name, ticket.token, body)
        if (
            not isinstance(receipt, dict)
            or receipt.get("call_id") != str(call_id)
            or receipt.get("digest") != revision.digest
            or not schema_validator(revision.contract.output_schema).is_valid(
                receipt.get("output")
            )
        ):
            raise CapabilityOutcomeUnknown("Unverified KnowBase function receipt")
        if revision.name == "read_input":
            result = receipt["output"]
            source = result["source"]
            mode = arguments["what"]
            kind_field = {
                "pages": "pages",
                "text": "offset",
                "format": "media_type",
                "original": "text",
            }[mode]
            if (
                source["material_id"] != arguments["material_id"]
                or (
                    "version_no" in arguments
                    and source["version_no"] != arguments["version_no"]
                )
                or kind_field not in result
                or (
                    mode in ("pages", "text")
                    and not str(source["reading"]).startswith(mode + "/")
                )
                or (mode in ("format", "original") and source["reading"] is not None)
                or (
                    mode == "text"
                    and result["offset"] != max(0, arguments.get("offset", 0))
                )
            ):
                raise CapabilityOutcomeUnknown(
                    "KnowBase returned a different material, rendition or offset"
                )
        if method_read is not None:
            from app.application.workbench.knowbase_method_read import verify_material

            verify_material(receipt["output"], method_read)
        return receipt

    async def host_read(
        self, repo, name, args, *, owner, session_id, task, parent_call_id
    ):
        """Record a host-bound read under the consuming task or approved script."""
        task_id = str(task["id"]) if task else None
        subject = await repo.subject_of_actor(owner)
        if not subject:
            raise RecordsRefused("this account is not known to the data service")
        call_id = str(uuid4())
        try:
            async with repo.transaction():
                revisions = await repo.scene_function_revisions(
                    {name: ("knowbase", 1)}, scene="step"
                )
                if not revisions:
                    raise UserDataUnavailable(f"{name} v1 is unavailable for this step")
                revision = revisions[0]
                tool = ToolRegistration.discover(
                    "service_function", destination(revision.name), descriptor(revision)
                )
                if not schema_validator(revision.contract.input_schema).is_valid(args):
                    raise ComputeRefused(
                        "Arguments do not match the registered read contract"
                    )
                session = await repo.get_session(session_id, owner_actor_id=owner)
                parent = parent_call_id
                if task:
                    active = await repo.get_task(
                        task_id, owner_actor_id=owner, for_update=True
                    )
                    if (
                        str(session.get("active_task_id")) != task_id
                        or active["state"] in TASK_TERMINAL
                        or active.get("active_attempt_id")
                        != task.get("active_attempt_id")
                    ):
                        raise RecordsRefused("The task changed before its data read")
                    parent = parent or (await root_call(repo, active))["id"]
                elif (
                    not parent
                    or session.get("active_task_id")
                    or session.get("wheel") != "user"
                    or str(session.get("knowbase_session_id")) != args.get("session")
                ):
                    raise RecordsRefused(
                        "The conversation changed before its material read"
                    )
                detail = {
                    "tool": tool.descriptor(),
                    "operation": {
                        "operation": revision.name,
                        "effect": "read",
                        "registered_source": revision.model_dump(exclude={"contract"}),
                        "arguments_digest": digest(args),
                    },
                    "identity_basis": "host_task_data_read"
                    if task
                    else "host_session_data_read",
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
                        "attempt_id": task.get("active_attempt_id") if task else None,
                        "registration_id": tool.tool_id,
                        "registration_digest": tool.descriptor_digest,
                        "digest": digest(detail),
                        "status": "dispatching",
                        "detail": detail,
                    }
                )
        except CapabilityError as exc:
            raise UserDataUnavailable("Invalid registered data reader") from exc
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
                if isinstance(exc, (ComputeRefused, CapabilityError))
                and not isinstance(exc, CapabilityOutcomeUnknown)
                else "unknown"
            )
            async with repo.transaction():
                await repo.update_call(owner, call_id, status, detail)
            if isinstance(exc, ComputeRefused):
                raise
            raise UserDataUnavailable(
                "The registered data reader could not confirm the result"
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
