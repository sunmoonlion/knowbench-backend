"""Dispatch the registered KnowBase fact query with host-bound data scope."""

from app.domain.workbench.capabilities import (
    CapabilityError,
    CapabilityOutcomeUnknown,
    digest,
)
from app.domain.workbench.tool_schema import schema_validator


class FactFunction:
    def __init__(self, client, issuer):
        self.client, self.issuer = client, issuer

    async def invoke(self, revision, *, subject, sandbox, call_id, arguments):
        if (
            revision.provider,
            revision.name,
            revision.version,
            revision.contract.operation,
        ) != (
            "knowbase",
            "query_facts",
            1,
            "query_facts",
        ) or revision.contract.required_permissions != ["facts.read"]:
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
        receipt = await self.client.invoke_query_facts(ticket.token, body)
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
