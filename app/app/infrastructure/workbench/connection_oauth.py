"""Bounded back-channel OAuth and A2A calls to reviewed, DNS-pinned endpoints."""

import asyncio
import json
from uuid import uuid4

import httpx

from app.domain.workbench.capabilities import CapabilityError, CapabilityReauthRequired
from app.infrastructure.workbench.capability_mcp import MAX_BYTES, public_address


class OAuthTransport:
    def __init__(self, *, transport=None):
        self.transport = transport

    async def request(
        self, method, url, *, headers=None, data=None, body=None, empty=False
    ):
        ip, host = await public_address(url)
        target = httpx.URL(url).copy_with(host=ip)
        try:
            async with (
                asyncio.timeout(15),
                httpx.AsyncClient(
                    timeout=10,
                    follow_redirects=False,
                    trust_env=False,
                    transport=self.transport,
                ) as client,
                client.stream(
                    method,
                    target,
                    headers={
                        "Host": host,
                        "Accept": "application/json",
                        **(headers or {}),
                    },
                    data=data,
                    json=body,
                    extensions={"sni_hostname": host},
                ) as response,
            ):
                if response.status_code == 401:
                    raise CapabilityReauthRequired(
                        "The provider rejected this credential"
                    )
                if empty:
                    if response.status_code == 200:
                        return {}
                    raise CapabilityError("Provider revocation was not confirmed")
                payload = bytearray()
                async for chunk in response.aiter_bytes():
                    payload.extend(chunk)
                    if len(payload) > MAX_BYTES:
                        raise CapabilityError(
                            "Provider response exceeds the size limit"
                        )
                if response.status_code == 400:
                    if json.loads(payload).get("error") == "invalid_grant":
                        raise CapabilityReauthRequired(
                            "The provider rejected this grant"
                        )
                if not response.is_success:
                    raise CapabilityError("The provider request was unsuccessful")
                result = json.loads(payload)
                if not isinstance(result, dict):
                    raise CapabilityError("Invalid provider response")
                return result
        except (
            ValueError,
            AttributeError,
            RecursionError,
            httpx.HTTPError,
            TimeoutError,
        ):
            raise CapabilityError("The provider response is unavailable") from None

    @staticmethod
    def client_fields(provider):
        fields = {"client_id": provider.client_id}
        if provider.client_secret:
            fields["client_secret"] = provider.client_secret.get_secret_value()
        return fields

    async def token(self, provider, grant):
        fields = self.client_fields(provider) | grant
        if provider.resource_indicator:
            fields["resource"] = provider.service_url
        return await self.request("POST", provider.token_url, data=fields)

    async def account(self, provider, access):
        return await self.request(
            "GET", provider.account_url, headers={"Authorization": "Bearer " + access}
        )

    async def revoke(self, provider, token, hint):
        if not provider.revocation_url:
            return False
        await self.request(
            "POST",
            provider.revocation_url,
            data={
                **self.client_fields(provider),
                "token": token,
                "token_type_hint": hint,
            },
            empty=True,
        )
        return True

    async def get_task(self, provider, access, arguments):
        rpc_id = str(uuid4())
        response = await self.request(
            "POST",
            provider.service_url,
            headers={"Authorization": "Bearer " + access, "A2A-Version": "1.0"},
            body={
                "jsonrpc": "2.0",
                "id": rpc_id,
                "method": "GetTask",
                "params": {"id": arguments["id"], "historyLength": 0},
            },
        )
        task = response.get("result")
        if (
            response.get("id") != rpc_id
            or response.get("jsonrpc") != "2.0"
            or "error" in response
            or not isinstance(task, dict)
            or task.get("id") != arguments["id"]
            or not isinstance(task.get("status"), dict)
            or not isinstance(task["status"].get("state"), str)
        ):
            raise CapabilityError("The A2A service returned no matching task status")
        result = {"id": task["id"], "state": task["status"]["state"]}
        return {
            "content": [{"type": "text", "text": json.dumps(result)}],
            "structuredContent": result,
        }
