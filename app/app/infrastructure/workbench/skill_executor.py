"""Short requests to the isolated executor. Never launch scripts in this process."""

from __future__ import annotations

from uuid import UUID

import httpx
from pydantic import TypeAdapter

from app.domain.workbench.capabilities import (
    CapabilityError,
    ScriptInputNotSent,
    ScriptSlotOccupied,
)
from app.domain.workbench.script_contract_generated import (
    ScriptContainerMeter,
    ScriptExecution,
    ScriptNotStarted,
    ScriptStatus,
    ScriptStopped,
    execution_frame,
)


class SkillExecutor:
    def __init__(self, url: str, token: str, *, transport=None):
        self.url, self.token, self.transport = url, token, transport

    async def request(self, method: str, owner: str, run: str, body=None, *, suffix=""):
        path = f"/v1/script-executions/{UUID(owner)}/{UUID(run)}{suffix}"
        async with httpx.AsyncClient(
            base_url=self.url,
            headers={"Authorization": "Bearer " + self.token},
            timeout=httpx.Timeout(60 if suffix == "/prepare" else 20, connect=3),
            transport=self.transport,
            trust_env=False,
            follow_redirects=False,
        ) as client:
            response = await client.request(method, path, json=body)
            if (
                suffix == "/prepare"
                and response.status_code == 409
                and response.json().get("detail")
                == {"code": "slot_occupied_before_creation", "run_id": str(run)}
            ):
                raise ScriptSlotOccupied("Another script occupies the execution slot")
            if method in ("PUT", "POST") and response.status_code in (
                401,
                403,
                409,
                422,
            ):
                raise CapabilityError(
                    "Script was not started: execution slot is busy or its configuration is unavailable"
                )
            response.raise_for_status()
            if suffix == "/stop-metered":
                return response.json()
            if suffix == "/container-meter":
                meter = TypeAdapter(
                    ScriptContainerMeter | ScriptNotStarted
                ).validate_json(response.content)
                if meter.run_id != UUID(run):
                    raise CapabilityError(
                        "Container meter belongs to a different execution"
                    )
                return meter
            if method == "DELETE":
                return ScriptStopped.model_validate_json(response.content).model_dump()
            return ScriptStatus.model_validate_json(response.content).model_dump()

    async def prepare(self, owner: str, run: str, body: dict):
        frame, _ = execution_frame(ScriptExecution.model_validate(body))
        return await self.request(
            "POST", owner, run, frame.model_dump(), suffix="/prepare"
        )

    async def start(self, owner: str, run: str, body: dict):
        payload = ScriptExecution.model_validate(body).model_dump()
        try:
            return await self.request("PUT", owner, run, payload, suffix="/input")
        except (httpx.ConnectError, httpx.ConnectTimeout) as exc:
            raise ScriptInputNotSent(
                "Could not connect to deliver the approved script input"
            ) from exc
        except (httpx.HTTPError, ValueError):
            # The durable call remains dispatching while the runner queries this exact Job.
            return {"state": "running"}

    async def status(self, owner: str, run: str):
        return await self.request("GET", owner, run)

    async def container_meter(self, owner: str, run: str):
        return await self.request("GET", owner, run, suffix="/container-meter")

    async def stop_metered(self, owner, run):
        return await self.request("POST", owner, run, suffix="/stop-metered")

    async def cancel(self, owner: str, run: str):
        try:
            await self.request("DELETE", owner, run)
        except (httpx.HTTPError, ValueError) as exc:
            raise CapabilityError(
                "Execution termination is not confirmed; retry or check its status. The execution deadline remains in force."
            ) from exc
