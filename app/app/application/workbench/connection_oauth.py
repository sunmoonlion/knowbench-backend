"""External account authorizations scoped to one reviewed service and one asset."""

import asyncio
import base64
import hashlib
import json
import secrets
from datetime import UTC, datetime, timedelta
from urllib.parse import urlencode, urlsplit
from uuid import uuid4

from cryptography.fernet import InvalidToken

from app.application.workbench.call_policy import send_mcp
from app.application.workbench.tool_registry import mcp_catalog
from app.domain.workbench.capabilities import (
    CapabilityAuthOutcomeUnknown,
    CapabilityError,
    CapabilityReauthRequired,
    digest,
    public_asset,
)
from app.domain.workbench.connection_oauth import A2A_TASK as A2A_TASK
from app.domain.workbench.connection_oauth import A2A_TOOLS


class ConnectionOAuth:
    def __init__(
        self, repo, providers, transport, remote, cipher, web_origin, *, now=None
    ):
        self.repo, self.transport, self.remote, self.cipher = (
            repo,
            transport,
            remote,
            cipher,
        )
        self.providers = {p.provider_id: p for p in providers}
        self.urls = {p.service_url for p in providers}
        self.protocols = {p.service_url: p.protocol for p in providers}
        self.origin = web_origin.rstrip("/")
        self.now = now or (lambda: datetime.now(UTC))

    def provider(self, provider_id):
        provider = self.providers.get(provider_id)
        if provider is None or self.cipher is None:
            raise CapabilityError("This connection provider is unavailable")
        return provider

    def callback(self, provider):
        return f"{self.origin}/api/workbench/capabilities/oauth/{provider.provider_id}/callback"

    def encrypt(self, value):
        return self.cipher.encrypt(json.dumps(value).encode()).decode()

    def decrypt(self, value):
        try:
            return json.loads(self.cipher.decrypt(value.encode()))
        except InvalidToken:
            raise CapabilityError(
                "The connection credential key is unavailable"
            ) from None

    def same_credential(self, current, expected):
        # A maintenance re-encryption preserves the credential and any refresh claim.
        return current == expected or bool(
            current and expected and self.decrypt(current) == self.decrypt(expected)
        )

    async def start(self, owner, body):
        p = self.provider(body.provider_id)
        verifier, state = secrets.token_urlsafe(48), secrets.token_urlsafe(32)
        origin = urlsplit(self.origin)
        if origin.scheme != "https" and not (
            origin.scheme == "http"
            and origin.hostname in ("localhost", "127.0.0.1", "::1")
        ):
            raise CapabilityError("The registered web callback requires HTTPS")
        redirect = self.callback(p)
        context = {
            **body.model_dump(mode="json"),
            "binding": p.binding(),
            "redirect": redirect,
        }
        async with self.repo.transaction():
            if body.connection_id:
                asset = await self.repo.capability_asset(
                    owner, str(body.connection_id), lock=True
                )
                info = asset["detail"].get("oauth") or {}
                if (
                    info.get("provider_id") != p.provider_id
                    or info.get("issuer") != p.issuer
                    or asset["detail"]["url"] != p.service_url
                    or asset["status"] == "disconnected"
                    or asset["revision"] != body.revision
                ):
                    raise CapabilityError("The connection has changed")
            await self.repo.start_connection_oauth(
                owner,
                p.provider_id,
                digest(state),
                self.encrypt(verifier),
                context,
                self.now() + timedelta(minutes=10),
            )
        challenge = (
            base64.urlsafe_b64encode(hashlib.sha256(verifier.encode()).digest())
            .rstrip(b"=")
            .decode()
        )
        params = {
            "response_type": "code",
            "client_id": p.client_id,
            "redirect_uri": redirect,
            "scope": " ".join(p.scopes),
            "state": state,
            "code_challenge": challenge,
            "code_challenge_method": "S256",
        }
        if p.resource_indicator:
            params["resource"] = p.service_url
        return {"authorization_url": p.authorization_url + "?" + urlencode(params)}

    def tokens(self, response, scopes, old_refresh=None):
        access = response.get("access_token")
        refresh = response.get("refresh_token", old_refresh)
        ttl = response.get("expires_in")
        raw_scope = response.get("scope", " ".join(scopes))
        if (
            not isinstance(response.get("token_type"), str)
            or response["token_type"].lower() != "bearer"
            or not isinstance(access, str)
            or not access.isascii()
            or not access.isprintable()
            or any(c.isspace() for c in access)
            or not 1 <= len(access) <= 16384
            or refresh is not None
            and (not isinstance(refresh, str) or not 1 <= len(refresh) <= 16384)
            or type(ttl) is not int
            or not 0 < ttl <= 315360000
            or not isinstance(raw_scope, str)
        ):
            raise CapabilityReauthRequired(
                "Provider returned unsupported token metadata"
            )
        granted = sorted(set(raw_scope.split()))
        if not set(granted) <= set(scopes):
            raise CapabilityReauthRequired("The provider returned an unexpected scope")
        return {
            "access": access,
            "refresh": refresh,
            "expires": (self.now() + timedelta(seconds=ttl)).timestamp(),
        }, granted

    async def identity(self, p, token):
        account = await self.transport.account(p, token)
        subject = account.get(p.account_field)
        if (
            not isinstance(subject, str)
            or not subject.strip()
            or len(subject) > 512
            or not subject.isprintable()
        ):
            raise CapabilityReauthRequired(
                "The provider account could not be identified"
            )
        label = account.get(p.label_field)
        display = label.strip()[:100] if isinstance(label, str) else ""
        return (
            subject,
            f"{display} ({subject})" if display and display != subject else subject,
        )

    async def tools(self, p, access, scopes):
        tools = (
            list(A2A_TOOLS.values())
            if p.protocol == "a2a"
            else await self.remote.discover(p.service_url, access)
        )
        return mcp_catalog(
            p.service_url,
            [
                t
                for t in tools
                if t["name"] in p.tool_scopes
                and set(p.tool_scopes[t["name"]]) <= set(scopes)
            ],
            protocol=p.protocol,
        ).descriptors()

    async def finish(self, owner, provider_id, state, issuer, code, denied):
        p = self.provider(provider_id)
        async with self.repo.transaction():
            tx = await self.repo.consume_connection_oauth(
                owner, provider_id, digest(state)
            )
        if not tx:
            raise CapabilityError("The authorization has expired or was already used")
        ctx = tx["context"]
        if (
            issuer is not None
            and issuer != p.issuer
            or p.binding() != ctx["binding"]
            or self.callback(p) != ctx["redirect"]
        ):
            raise CapabilityError("The authorization did not complete")
        location = f"{self.origin}/{ctx['locale']}/workbench/settings/connections"
        query = {"returnTo": ctx["return_to"]} if ctx.get("return_to") else {}
        if denied:
            return location + "?" + urlencode({**query, "authorization": "incomplete"})
        if not code:
            raise CapabilityError("The authorization did not complete")
        if ctx["connection_id"]:
            async with self.repo.transaction():
                target = await self.repo.capability_asset(owner, ctx["connection_id"])
                if (
                    target["revision"] != ctx["revision"]
                    or target["status"] == "disconnected"
                ):
                    raise CapabilityError("The connection changed before authorization")
        response = await self.transport.token(
            p,
            {
                "grant_type": "authorization_code",
                "code": code,
                "redirect_uri": ctx["redirect"],
                "code_verifier": self.decrypt(tx["verifier"]),
            },
        )
        creds, scopes = self.tokens(response, p.scopes)
        try:
            subject, label = await self.identity(p, creds["access"])
            if (
                ctx["connection_id"]
                and target["detail"]["oauth"]["account_id"] != subject
            ):
                await self.revoke(p, creds)
                raise CapabilityError("Use a new connection for a different account")
            descriptors = await self.tools(p, creds["access"], scopes)
        except Exception:
            if not ctx["connection_id"]:
                await self.revoke(p, creds)
            raise
        detail = {
            "url": p.service_url,
            "account_label": label,
            "auth": "oauth",
            "tools": descriptors,
            "checked_at": self.now().isoformat(),
            "oauth": {
                "provider_id": p.provider_id,
                "protocol": p.protocol,
                "issuer": p.issuer,
                "account_id": subject,
                "scopes": scopes,
            },
        }
        creds["binding"] = p.binding()
        asset_id = ctx["connection_id"] or str(uuid4())
        try:
            async with self.repo.transaction():
                if ctx["connection_id"]:
                    asset = await self.repo.capability_asset(
                        owner, ctx["connection_id"], lock=True
                    )
                    changed = (
                        asset["revision"] != ctx["revision"]
                        or asset["status"] == "disconnected"
                    )
                    if not changed:
                        await self.repo.capability_update(
                            owner,
                            asset["id"],
                            asset["revision"],
                            status="active",
                            detail=detail,
                            secret=self.encrypt(creds),
                        )
                else:
                    await self.repo.capability_insert(
                        {
                            "id": asset_id,
                            "owner_actor_id": owner,
                            "kind": "connection",
                            "name": ctx["name"],
                            "status": "active",
                            "detail": detail,
                            "secret": self.encrypt(creds),
                            "request_id": "oauth:" + digest(state),
                            "request_digest": digest(ctx),
                        }
                    )
        except Exception:
            if not ctx["connection_id"]:
                from app.domain.workbench.errors import NotFound

                # A lost commit acknowledgement may leave a usable connection.
                try:
                    async with self.repo.transaction():
                        await self.repo.capability_asset(owner, asset_id)
                except NotFound:
                    await self.revoke(p, creds)
            raise
        if ctx["connection_id"] and changed:
            if asset["status"] == "disconnected":
                await self.revoke(p, creds)
            raise CapabilityError("The connection changed during authorization")
        return location + ("?" + urlencode(query) if query else "")

    async def invalidate(self, owner, asset, reason):
        detail = {**asset["detail"], "reauth_reason": reason}
        await self.repo.capability_update(
            owner,
            asset["id"],
            asset["revision"],
            status="reauth_required",
            detail=detail,
            secret=None,
        )

    async def credential(self, owner, asset_id, revision, tool=None):
        # Claim a single rotation durably before network I/O. A later disconnect can win
        # immediately; a restarted worker cannot replay an uncertain rotating token.
        while True:
            expired = False
            async with self.repo.transaction():
                asset = await self.repo.capability_asset(owner, asset_id, lock=True)
                if asset["status"] != "active" or asset["revision"] != revision:
                    raise CapabilityReauthRequired(
                        "The connection or account binding changed"
                    )
                info = asset["detail"]["oauth"]
                p = self.provider(info["provider_id"])
                creds = self.decrypt(asset["secret"])
                if creds["binding"] != p.binding():
                    raise CapabilityReauthRequired("The provider configuration changed")
                if tool is not None and (
                    tool not in p.tool_scopes
                    or not set(p.tool_scopes[tool]) <= set(info["scopes"])
                ):
                    raise CapabilityError(
                        "Connection scope does not permit this operation"
                    )
                now = self.now().timestamp()
                if creds["expires"] > now and "refresh_started" not in creds:
                    return p, creds["access"], asset["secret"]
                if not creds["refresh"] or now - creds.get("refresh_started", now) > 60:
                    await self.invalidate(owner, asset, "refresh_unavailable")
                    expired = True
                elif "refresh_started" not in creds:
                    claim = self.encrypt({**creds, "refresh_started": now})
                    await self.repo.rotate_connection_secret(
                        owner, asset_id, revision, claim
                    )
                    break
            if expired:
                raise CapabilityReauthRequired(
                    "The external account needs authorization"
                )
            await asyncio.sleep(0.1)
        try:
            response = await self.transport.token(
                p, {"grant_type": "refresh_token", "refresh_token": creds["refresh"]}
            )
            renewed, scopes = self.tokens(response, info["scopes"], creds["refresh"])
            subject, _ = await self.identity(p, renewed["access"])
            if subject != info["account_id"] or scopes != info["scopes"]:
                await self.revoke(p, renewed)
                raise CapabilityReauthRequired("The external account or scopes changed")
            renewed["binding"] = p.binding()
            sealed = self.encrypt(renewed)
        except CapabilityError as exc:
            reason = (
                "grant_invalid"
                if isinstance(exc, CapabilityReauthRequired)
                else "refresh_outcome_unknown"
            )
            await self.reject(owner, asset_id, revision, claim, reason)
            raise
        async with self.repo.transaction():
            current = await self.repo.capability_asset(owner, asset_id, lock=True)
            won = (
                current["status"] == "active"
                and current["revision"] == revision
                and self.same_credential(current["secret"], claim)
            )
            if won:
                await self.repo.rotate_connection_secret(
                    owner, asset_id, revision, sealed
                )
        if not won:
            if current["status"] in ("disconnected", "disabled", "reauth_required"):
                await self.revoke(p, renewed)
            raise CapabilityReauthRequired("Connection changed during token renewal")
        return p, renewed["access"], sealed

    async def reject(self, owner, asset_id, revision, expected, reason):
        async with self.repo.transaction():
            current = await self.repo.capability_asset(owner, asset_id, lock=True)
            if (
                current["status"] == "active"
                and current["revision"] == revision
                and self.same_credential(current["secret"], expected)
            ):
                await self.invalidate(owner, current, reason)

    async def revoke(self, p, creds):
        tokens = []
        if creds.get("refresh"):
            tokens.append(("refresh_token", creds["refresh"]))
        if creds["access"] != creds.get("refresh"):
            tokens.append(("access_token", creds["access"]))
        confirmed = True
        for hint, token in tokens:
            try:
                accepted = await self.transport.revoke(p, token, hint)
            except CapabilityError:
                accepted = False
            confirmed = confirmed and accepted
        return confirmed

    async def disconnect(self, owner, asset_id, revision):
        async with self.repo.transaction():
            asset = await self.repo.capability_asset(owner, asset_id, lock=True)
            if asset["status"] == "disconnected":
                return public_asset(asset)
            detail = {
                **asset["detail"],
                "revocation": "local_access_removed; external_revocation_unconfirmed",
            }
            saved = await self.repo.capability_update(
                owner,
                asset_id,
                revision,
                status="disconnected",
                detail=detail,
                secret=None,
            )
        # No further use is allowed even when the provider is unreachable.
        p = self.providers.get(detail["oauth"]["provider_id"])
        if p and asset["secret"] and self.cipher:
            try:
                creds = self.decrypt(asset["secret"])
            except CapabilityError:
                return public_asset(saved)
            if (
                p.binding() == creds["binding"]
                and await self.revoke(p, creds)
                and "refresh_started" not in creds
            ):
                async with self.repo.transaction():
                    detail["revocation"] = "provider_revoked"
                    saved = await self.repo.capability_update(
                        owner,
                        asset_id,
                        saved["revision"],
                        status="disconnected",
                        detail=detail,
                        secret=None,
                    )
        return public_asset(saved)

    async def action(self, owner, asset, body):
        asset_id = asset["id"]
        if body.action == "disconnect":
            return await self.disconnect(owner, asset_id, body.revision)
        if body.action == "refresh":
            p, token, sealed = await self.credential(owner, asset_id, body.revision)
            try:
                tools = await self.tools(p, token, asset["detail"]["oauth"]["scopes"])
            except CapabilityReauthRequired:
                await self.reject(
                    owner, asset_id, body.revision, sealed, "provider_rejected_token"
                )
                raise
        async with self.repo.transaction():
            current = await self.repo.capability_asset(owner, asset_id, lock=True)
            detail = dict(current["detail"])
            secret = current["secret"]
            if body.action == "refresh" and current["status"] == "active":
                detail.update(tools=tools, checked_at=self.now().isoformat())
                status = "active"
            elif body.action == "disable" and current["status"] == "active":
                if secret and "refresh_started" in self.decrypt(secret):
                    secret = None
                    detail["reauth_reason"] = "refresh_interrupted"
                    status = "reauth_required"
                else:
                    status = "disabled"
            elif body.action == "enable" and current["status"] == "disabled" and secret:
                status = "active"
            else:
                raise CapabilityError("The connection needs a new authorization")
            return public_asset(
                await self.repo.capability_update(
                    owner,
                    asset_id,
                    body.revision,
                    status=status,
                    detail=detail,
                    secret=secret,
                )
            )

    async def call(self, owner, connection, intent, *, call=None, before_send=None):
        name = intent["tool"]["name"]
        p, token, sealed = await self.credential(
            owner, connection["id"], intent["revision"], name
        )
        try:
            if p.protocol == "a2a":
                arguments = intent["arguments"]
                submission = None
                if name in ("a2a_submit_task", "a2a_reconcile_task"):
                    if call is None:
                        raise CapabilityError("A2A submission requires a recorded call")
                    submission = call["id"]
                    if name == "a2a_reconcile_task":
                        original = await self.repo.capability_call(
                            owner, arguments["call_id"]
                        )
                        if (
                            original["session_id"] != call["session_id"]
                            or original["connection_id"] != connection["id"]
                            or original["detail"].get("revision") != intent["revision"]
                            or original["detail"].get("tool", {}).get("name")
                            != "a2a_submit_task"
                            or original["status"] not in ("dispatching", "unknown")
                        ):
                            raise CapabilityError(
                                "No matching uncertain A2A submission"
                            )
                        submission = original["id"]
                if before_send is not None:
                    await before_send()
                return await self.transport.a2a_task(
                    p, token, name, arguments, submission
                )
            return await send_mcp(
                self.remote,
                p.service_url,
                token,
                name,
                intent["arguments"],
                before_send=before_send,
            )
        except (CapabilityReauthRequired, CapabilityAuthOutcomeUnknown):
            await self.reject(
                owner,
                connection["id"],
                intent["revision"],
                sealed,
                "provider_rejected_token",
            )
            raise
