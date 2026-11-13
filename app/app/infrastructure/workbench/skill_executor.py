"""Short requests to the isolated executor. Never launch scripts in this process."""

from __future__ import annotations

from uuid import UUID

import httpx

from app.domain.workbench.capabilities import CapabilityError
from app.domain.workbench.script_contract_generated import (
    ScriptExecution,
    ScriptStatus,
    ScriptStopped,
)


class SkillExecutor:
    def __init__(self, url: str, token: str, *, transport=None):
        self.url, self.token, self.transport = url, token, transport

    async def request(self, method: str, owner: str, run: str, body=None):
        path = f"/v1/script-executions/{UUID(owner)}/{UUID(run)}"
        async with httpx.AsyncClient(
            base_url=self.url,
            headers={"Authorization": "Bearer " + self.token},
            timeout=httpx.Timeout(15, connect=3),
            transport=self.transport,
            trust_env=False,
            follow_redirects=False,
        ) as client:
            response = await client.request(method, path, json=body)
            if method == "PUT" and response.status_code in (401, 403, 409, 422):
                raise CapabilityError(
                    "Script was not started: execution slot is busy or its configuration is unavailable"
                )
            response.raise_for_status()
            if method == "DELETE":
                return ScriptStopped.model_validate_json(response.content).model_dump()
            return ScriptStatus.model_validate_json(response.content).model_dump()

    async def start(self, owner: str, run: str, body: dict):
        return await self.request(
            "PUT", owner, run, ScriptExecution.model_validate(body).model_dump()
        )

    async def status(self, owner: str, run: str):
        return await self.request("GET", owner, run)

    async def cancel(self, owner: str, run: str):
        try:
            await self.request("DELETE", owner, run)
        except (httpx.HTTPError, ValueError) as exc:
            raise CapabilityError(
                "Execution termination is not confirmed; retry or check its status. The execution deadline remains in force."
            ) from exc
