"""Bounded back-channel OAuth and A2A calls to reviewed, DNS-pinned endpoints."""

import asyncio
import json
from uuid import NAMESPACE_URL, uuid4, uuid5

import httpx

from app.domain.workbench.capabilities import (
    CapabilityError,
    CapabilityOutcomeUnknown,
    CapabilityReauthRequired,
)
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

    async def a2a_task(self, provider, access, name, arguments, submission):
        rpc_id = str(uuid4())
        context = str(uuid5(NAMESPACE_URL, f"knowbench:a2a:context:{submission}"))
        message = str(uuid5(NAMESPACE_URL, f"knowbench:a2a:message:{submission}"))
        expected_id = arguments.get("id")
        if name == "a2a_get_task":
            method, params = "GetTask", {"id": expected_id, "historyLength": 0}
        elif name == "a2a_cancel_task":
            method, params = "CancelTask", {"id": expected_id}
        elif name == "a2a_submit_task":
            method, params = (
                "SendMessage",
                {
                    "message": {
                        "role": "ROLE_USER",
                        "contextId": context,
                        "messageId": message,
                        "parts": [{"text": arguments["text"]}],
                    },
                    "configuration": {
                        "returnImmediately": True,
                        "acceptedOutputModes": ["text/plain"],
                    },
                },
            )
        elif name == "a2a_reconcile_task":
            method, params = (
                "ListTasks",
                {"contextId": context, "pageSize": 2, "historyLength": 100},
            )
        else:
            raise CapabilityError("Unknown A2A operation")
        try:
            response = await self.request(
                "POST",
                provider.service_url,
                headers={"Authorization": "Bearer " + access, "A2A-Version": "1.0"},
                body={
                    "jsonrpc": "2.0",
                    "id": rpc_id,
                    "method": method,
                    "params": params,
                },
            )
            if (
                response.get("id") != rpc_id
                or response.get("jsonrpc") != "2.0"
                or "error" in response
                or not isinstance(response.get("result"), dict)
            ):
                raise CapabilityError("The A2A service returned no matching receipt")
            task = response["result"]
            if method == "SendMessage":
                task = task.get("task")
            elif method == "ListTasks":
                tasks = task.get("tasks")
                if (
                    not isinstance(tasks, list)
                    or len(tasks) != 1
                    or task.get("nextPageToken")
                    or task.get("totalSize", 1) != 1
                ):
                    raise CapabilityError(
                        "An exact A2A submission could not be confirmed"
                    )
                task = tasks[0]
            if (
                not isinstance(task, dict)
                or not isinstance(task.get("id"), str)
                or not 1 <= len(task["id"]) <= 512
                or expected_id is not None
                and expected_id != task["id"]
                or method in ("SendMessage", "ListTasks")
                and task.get("contextId") != context
                or not isinstance(task.get("status"), dict)
                or not isinstance(task["status"].get("state"), str)
                or not 1 <= len(task["status"]["state"]) <= 128
            ):
                raise CapabilityError(
                    "The A2A service returned no matching task status"
                )
            if method == "ListTasks" and not any(
                isinstance(entry, dict)
                and entry.get("messageId") == message
                and entry.get("role") == "ROLE_USER"
                and entry.get("contextId", context) == context
                for entry in (task.get("history") or [])
            ):
                raise CapabilityError(
                    "The A2A task has no matching submission evidence"
                )
        except CapabilityReauthRequired:
            raise
        except CapabilityError as exc:
            if method in ("SendMessage", "CancelTask"):
                raise CapabilityOutcomeUnknown(
                    "The A2A action outcome is unknown. Check the original task."
                ) from exc
            raise
        result = {"id": task["id"], "state": task["status"]["state"]}
        if submission is not None:
            result["submission_call_id"] = submission
        return {
            "content": [{"type": "text", "text": json.dumps(result)}],
            "structuredContent": result,
        }
