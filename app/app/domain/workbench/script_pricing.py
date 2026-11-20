"""Operator-owned container-second prices for a single isolated execution environment."""

from decimal import ROUND_HALF_UP, Decimal
from typing import Annotated, Literal
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field, model_validator

from app.domain.workbench.capabilities import CapabilityError, digest
from app.domain.workbench.method_capabilities import McpRequirement
from app.domain.workbench.script_contract_generated import (
    ScriptContainerMeter,
    ScriptNotStarted,
)

Money = Annotated[
    Decimal, Field(ge=0, max_digits=12, decimal_places=6, allow_inf_nan=False)
]


class ScriptTariff(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")
    environment: str = Field(min_length=1, max_length=128)
    revision: int = Field(ge=1, strict=True)
    destination: str
    provisioner_url: str = Field(min_length=1)
    currency: Literal["CNY"]
    per_container_second: Money
    evidence_reference: str = Field(min_length=1, max_length=512)
    evidence_digest: str = Field(pattern=r"^sha256:[a-f0-9]{64}$")

    @model_validator(mode="after")
    def validate_destination(self):
        McpRequirement(url=self.destination, name="script")
        return self

    def quote(self):
        # 90 s Job deadline plus 1 s termination grace. In excess, settlement needs reconciliation.
        return {
            "environment": self.environment,
            "revision": self.revision,
            "currency": self.currency,
            "per_container_second": str(self.per_container_second),
            "maximum": str(self.per_container_second * 91),
            "maximum_seconds": 91,
            "tariff_digest": digest(self.model_dump(mode="json")),
            "evidence_reference": self.evidence_reference,
            "evidence_digest": self.evidence_digest,
        }


def rated_usage(quote, run_id, meter: ScriptContainerMeter | ScriptNotStarted):
    if isinstance(meter, ScriptNotStarted) and meter.run_id == UUID(str(run_id)):
        return Decimal("0"), {
            "meter": meter.model_dump(mode="json"),
            "seconds": "0",
            "tariff_digest": quote["tariff_digest"],
        }
    if (
        not isinstance(meter, ScriptContainerMeter)
        or meter.run_id != UUID(str(run_id))
        or meter.finished_at is None
    ):
        raise CapabilityError(
            "A final container meter for the original call is required"
        )
    delta = meter.finished_at - meter.started_at
    seconds = (
        Decimal(delta.days * 86400 + delta.seconds)
        + Decimal(delta.microseconds) / 1000000
    )
    amount = (seconds * Decimal(quote["per_container_second"])).quantize(
        Decimal("0.000001"), rounding=ROUND_HALF_UP
    )
    if seconds > Decimal(quote["maximum_seconds"]) or amount > Decimal(
        quote["maximum"]
    ):
        raise CapabilityError("Container usage exceeds its approved price ceiling")
    return amount, {
        "meter": meter.model_dump(mode="json"),
        "seconds": str(seconds),
        "tariff_digest": quote["tariff_digest"],
    }


class ScriptOperation:
    capability_ref = "isolated-script"
    version = "1"
    operation = "execute"
    effect = "execute"
    resources = ()

    def __init__(self, tariff, registration):
        self.tariff = tariff
        self.registration = registration
        self.service_url = tariff.destination
        self.policy_digest = digest(
            [registration.tool_id, registration.descriptor_digest, tariff.quote()]
        )

    def resolve(self, arguments):
        self.registration.validate_arguments(arguments)
        return {
            "policy_digest": self.policy_digest,
            "capability_ref": self.capability_ref,
            "version": self.version,
            "operation": self.operation,
            "effect": self.effect,
            "resources": [],
            "data_transfer": {
                "destination": self.service_url,
                "arguments_digest": digest(arguments),
            },
            "script_quote": self.tariff.quote(),
        }
