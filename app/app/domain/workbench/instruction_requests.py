"""Pinned instruction inputs, Responses wire contract, and route-specific cost ceiling."""

import json
from dataclasses import asdict, dataclass
from decimal import Decimal

from pydantic import ConfigDict, StrictInt

from app.domain.workbench.capabilities import CapabilityError, digest
from app.domain.workbench.pricing import Price, PriceList, Usage, cost_of
from app.domain.workbench.provider_url import validate_provider_base_url


@dataclass(frozen=True)
class InstructionRoute:
    """Trusted deployment route. context_tokens is the provider-enforced maximum."""

    __pydantic_config__ = ConfigDict(extra="forbid")

    provider: str
    model: str
    base_url: str
    price_key: str
    context_tokens: StrictInt
    output_tokens: StrictInt

    def quote(self, prices: PriceList):
        price = prices.of(self.price_key)
        if (
            not self.provider
            or not self.model
            or not price
            or type(self.context_tokens) is not int
            or type(self.output_tokens) is not int
            or not 0 < self.output_tokens <= self.context_tokens
        ):
            raise CapabilityError("Instruction route lacks a bounded model or price")
        # The full model context also covers the instructions and tools added by
        # Codex. Reserve the most expensive input category until actual usage arrives.
        upper = max(price.input, price.cached_input, price.cache_write)
        maximum = cost_of(
            Usage(input=self.context_tokens, output=self.output_tokens),
            Price(upper, upper, upper, price.output),
        )
        return {
            **asdict(self),
            "base_url": validate_provider_base_url(self.base_url),
            "currency": prices.currency,
            "as_of": prices.as_of,
            "rates": {k: str(v) for k, v in asdict(price).items()},
            "maximum": str(maximum),
        }


def instruction_usage_cost(quote, claim, usage: Usage):
    if (
        any(type(n) is not int or n < 0 for n in usage.as_json().values())
        or usage.output > claim["output_tokens"]
        or usage.total - usage.output > quote["context_tokens"]
    ):
        raise CapabilityError("Provider usage differs from the instruction limits")
    rates = quote["rates"]
    price = Price(
        input=Decimal(rates["input"]),
        cached_input=Decimal(rates["cached_input"]),
        cache_write=Decimal(rates["cache_write"]),
        output=Decimal(rates["output"]),
    )
    return cost_of(usage, price)


def instruction_job(request, entry, arguments):
    step = request.draft.definition.procedure
    return {
        "input": {
            "skill": entry,
            "instructions": step.instructions,
            "arguments": arguments,
        },
        "output_schema": {
            "type": "object",
            "properties": {k: v.schema_ for k, v in step.outputs.items()},
            "required": [k for k, v in step.outputs.items() if v.required],
            "additionalProperties": False,
        },
    }


def verify_instruction_body(body, job):
    # The pinned Codex host adds developer messages and may add one environment
    # message. Dispatch only a fresh, text-only job with its exact business input.
    try:
        messages = body["input"]
        users = []
        for message in messages:
            if message["type"] != "message" or message["role"] not in (
                "developer",
                "user",
            ):
                raise ValueError
            parts = message["content"]
            if not parts or any(
                p["type"] != "input_text" or not isinstance(p["text"], str)
                for p in parts
            ):
                raise ValueError
            if message["role"] == "user":
                if len(parts) != 1:
                    raise ValueError
                users.append(parts[0]["text"])
        output = body["text"]["format"]
        if (
            messages[-1]["role"] != "user"
            or len(users) not in (1, 2)
            or len(users) == 2
            and not users[0].startswith("<environment_context>")
            or digest(json.loads(users[-1])) != digest(job["input"])
            or output["type"] != "json_schema"
            or digest(output["schema"]) != digest(job["output_schema"])
            or body.get("service_tier") not in (None, "default")
            or body.get("stream") is not True
            or body.get("store") is not False
            or set(body)
            - {
                "model",
                "stream",
                "store",
                "input",
                "tools",
                "tool_choice",
                "parallel_tool_calls",
                "reasoning",
                "include",
                "prompt_cache_key",
                "text",
                "client_metadata",
                "max_output_tokens",
                "service_tier",
            }
            or any(
                tool.get("type") != "function"
                or tool.get("name") != "request_user_input"
                for tool in body.get("tools", ())
            )
        ):
            raise ValueError
    except (ValueError, TypeError, KeyError, IndexError, AttributeError) as exc:
        raise CapabilityError(
            "Outbound instruction differs from the bounded trial input or output contract"
        ) from exc


def leaf_responses_body(job, model, limit):
    """Build the entire external payload from authorized inputs, with no session metadata."""
    return {
        "model": model,
        "store": False,
        "stream": True,
        "max_output_tokens": limit,
        "input": [
            {
                "role": "user",
                "type": "message",
                "content": [
                    {
                        "type": "input_text",
                        "text": json.dumps(
                            job["input"],
                            ensure_ascii=False,
                            sort_keys=True,
                            separators=(",", ":"),
                        ),
                    }
                ],
            }
        ],
        "text": {
            "format": {
                "type": "json_schema",
                "name": "method_output",
                "schema": job["output_schema"],
            }
        },
    }
