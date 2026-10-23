"""D10：工作台签发的令牌（ES256 JWT），会合点与知识服务用公钥就地验，不回源。

六种：代理令牌（aud=relay, role=agent）、沙箱令牌（aud=relay, role=sandbox）、工作台服务流令牌（aud=relay, role=workbench，0022）、
本机访问票（aud=local-agent，网页敲代理回环口用，0022 乙案）、知识 MCP 令牌（aud=knowbase）、读项目记录的令牌（aud=workbench-records）。
都带 sub（用户的会合点名）、jti、exp。吊销靠会合点管理通道推 jti，或到期自然失效（F-RELAY-06）。
签名私钥在工作台配置（Secret）；公钥经 /api/workbench/token-keys（JWKS）给边缘与知识服务。
"""

from __future__ import annotations

import base64
import json
import secrets
import time
from dataclasses import dataclass
from typing import Any

from joserfc import jwt
from joserfc.errors import JoseError
from joserfc.jwk import ECKey

ALG = "ES256"
RECORDS_AUDIENCE = "workbench-records"
AGENT_TTL_SECONDS = 30 * 24 * 3600  # 所有者 2026-10-09 定：仅代理 30 天
WORKBENCH_SERVICE_TTL_SECONDS = (
    10 * 60
)  # 0022：工作台连会合点服务流用，一问一签，10 分钟
LOCAL_AGENT_AUDIENCE = "local-agent"
LOCAL_TICKET_TTL_SECONDS = 10 * 60  # 0022 乙案：浏览器敲本机代理回环口用的票，10 分钟
DEFAULT_TTL_SECONDS = 90 * 24 * 3600  # 长期：这些令牌随沙箱/代理重签，不是浏览器会话


@dataclass(frozen=True)
class Issued:
    token: str
    jti: str
    expires_at: int


class TokenIssuer:
    def __init__(
        self, private_key_pem: str, *, issuer: str = "sunmoon-workbench"
    ) -> None:
        self.key = ECKey.import_key(private_key_pem)
        self.key.ensure_kid()  # kid = 公钥指纹，换钥时边缘能按 kid 选钥
        self.issuer = issuer

    @property
    def kid(self) -> str:
        return str(self.key.kid)

    def public_jwks(self) -> dict[str, Any]:
        return {"keys": [{**self.key.as_dict(private=False), "use": "sig", "alg": ALG}]}

    def public_pem(self) -> str:
        return self.key.as_pem(private=False).decode()

    def _issue(self, claims: dict[str, Any], ttl_seconds: int) -> Issued:
        now = int(time.time())
        jti = secrets.token_urlsafe(16)
        payload = {
            "iss": self.issuer,
            "iat": now,
            "exp": now + ttl_seconds,
            "jti": jti,
            **claims,
        }
        token = jwt.encode(
            {"alg": ALG, "kid": self.kid, "typ": "JWT"}, payload, self.key
        )
        return Issued(token=token, jti=jti, expires_at=payload["exp"])

    def agent_token(
        self, relay_user: str, *, ttl_seconds: int = AGENT_TTL_SECONDS
    ) -> Issued:
        return self._issue(
            {"aud": "relay", "sub": relay_user, "role": "agent"}, ttl_seconds
        )

    def workbench_token(
        self, relay_user: str, *, ttl_seconds: int = WORKBENCH_SERVICE_TTL_SECONDS
    ) -> Issued:
        """0022：工作台自己作第三种客户连会合点 `/service`（role=workbench）；短期，一问一签。"""
        return self._issue(
            {"aud": "relay", "sub": relay_user, "role": "workbench"}, ttl_seconds
        )

    def local_agent_ticket(
        self, relay_user: str, *, ttl_seconds: int = LOCAL_TICKET_TTL_SECONDS
    ) -> Issued:
        """0022 乙案：网页拿它去敲用户电脑上代理的回环口；代理用 JWKS 就地验，sub 必须等于它自己的 userId。"""
        return self._issue(
            {"aud": LOCAL_AGENT_AUDIENCE, "sub": relay_user}, ttl_seconds
        )

    def sandbox_token(
        self, relay_user: str, *, sandbox: str, ttl_seconds: int = DEFAULT_TTL_SECONDS
    ) -> Issued:
        return self._issue(
            {"aud": "relay", "sub": relay_user, "role": "sandbox", "sandbox": sandbox},
            ttl_seconds,
        )

    def knowbase_token(
        self,
        relay_user: str,
        *,
        sandbox: str,
        tools: list[str] | None = None,
        ttl_seconds: int = DEFAULT_TTL_SECONDS,
    ) -> Issued:
        claims: dict[str, Any] = {
            "aud": "knowbase",
            "sub": relay_user,
            "sandbox": sandbox,
        }
        if tools:
            claims["tools"] = list(tools)
        return self._issue(claims, ttl_seconds)

    def records_token(
        self,
        relay_user: str,
        *,
        owner_actor_id: str,
        sandbox: str,
        ttl_seconds: int = DEFAULT_TTL_SECONDS,
    ) -> Issued:
        """沙箱里的 Codex 读项目记录用。绑用户；能读哪个项目，每次调用按委托核对。"""
        return self._issue(
            {
                "aud": RECORDS_AUDIENCE,
                "sub": relay_user,
                "owner": owner_actor_id,
                "sandbox": sandbox,
            },
            ttl_seconds,
        )

    def owner_of_records_token(self, token: str) -> str | None:
        """验这张令牌是不是我们签的、给读记录用的、没过期。是就返回用户编号。"""
        try:
            claims = verify(
                token,
                self.public_pem(),
                audience=RECORDS_AUDIENCE,
                issuer=self.issuer,
            )
        except (JoseError, ValueError):
            return None
        owner = claims.get("owner")
        return str(owner) if owner else None


def exp_of(token: str) -> int | None:
    """不验签地取 exp（给设置页显示令牌到期日；令牌是我们自己签的）。非 JWT 或没有返回 None。"""
    parts = token.split(".")
    if len(parts) != 3:
        return None
    try:
        payload = json.loads(
            base64.urlsafe_b64decode(parts[1] + "=" * (-len(parts[1]) % 4))
        )
    except (ValueError, UnicodeDecodeError):
        return None
    exp = payload.get("exp") if isinstance(payload, dict) else None
    return exp if isinstance(exp, int) and not isinstance(exp, bool) else None


def jti_of(token: str) -> str | None:
    """不验签地取 jti（吊销时用；令牌是我们自己签的）。非 JWT 返回 None。"""
    parts = token.split(".")
    if len(parts) != 3:
        return None
    try:
        payload = json.loads(
            base64.urlsafe_b64decode(parts[1] + "=" * (-len(parts[1]) % 4))
        )
    except (ValueError, UnicodeDecodeError):
        return None
    jti = payload.get("jti") if isinstance(payload, dict) else None
    return str(jti) if jti else None


def generate_private_key_pem() -> str:
    """运维一次性生成签名私钥（放进 Secret）。"""
    return ECKey.generate_key("P-256").as_pem(private=True).decode()


def verify(
    token: str, public_key_pem: str, *, audience: str, issuer: str | None = None
) -> dict[str, Any]:
    """工作台自己也能验（测试与自检用）；边缘与知识服务各有自己的验证器。"""
    key = ECKey.import_key(public_key_pem)
    decoded = jwt.decode(token, key, algorithms=[ALG])
    claims = decoded.claims
    if claims.get("aud") != audience:
        raise ValueError("audience mismatch")
    if issuer and claims.get("iss") != issuer:
        raise ValueError("issuer mismatch")
    if int(claims.get("exp", 0)) <= int(time.time()):
        raise ValueError("token expired")
    return claims
