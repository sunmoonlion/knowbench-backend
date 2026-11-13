"""Read-only public GitHub downloads with pinned DNS/TLS and bounded responses."""

import asyncio
import json
import re

import httpx

from app.domain.workbench.capabilities import CapabilityError
from app.domain.workbench.skill_installation import repository_parts
from app.infrastructure.workbench.capability_mcp import public_address


class GitHubSkills:
    async def download(self, url: str, *, limit: int) -> bytes:
        ip, host = await public_address(url)
        async with httpx.AsyncClient(
            timeout=httpx.Timeout(25, connect=5),
            trust_env=False,
            follow_redirects=False,
        ) as client:
            async with client.stream(
                "GET",
                httpx.URL(url).copy_with(host=ip),
                headers={
                    "Host": host,
                    "User-Agent": "SunMoonAI-Skill-Installer",
                    "Accept": "application/vnd.github+json",
                },
                extensions={"sni_hostname": host},
            ) as response:
                if response.status_code != 200:
                    raise CapabilityError(
                        "Public repository is unavailable or rate-limited; retry later or upload a Skill ZIP"
                    )
                data = bytearray()
                async for chunk in response.aiter_bytes():
                    data.extend(chunk)
                    if len(data) > limit:
                        raise CapabilityError(
                            "Repository exceeds download limit; upload a Skill ZIP instead"
                        )
                return bytes(data)

    async def fetch(self, url: str, commit: str | None = None) -> dict:
        owner, repo, ref, prefix = repository_parts(url)
        try:
            async with asyncio.timeout(60):
                if commit is None:
                    metadata = json.loads(
                        await self.download(
                            f"https://api.github.com/repos/{owner}/{repo}/commits/{ref or 'HEAD'}",
                            limit=1024 * 1024,
                        )
                    )
                    commit = metadata.get("sha")
                if not isinstance(commit, str) or not re.fullmatch(
                    r"[a-f0-9]{40}", commit
                ):
                    raise CapabilityError(
                        "Repository did not return an immutable commit"
                    )
                raw = await self.download(
                    f"https://codeload.github.com/{owner}/{repo}/zip/{commit}",
                    limit=20 * 1024 * 1024,
                )
                return {
                    "repository": f"https://github.com/{owner}/{repo}",
                    "commit": commit,
                    "prefix": prefix,
                    "archive": raw,
                }
        except (httpx.HTTPError, TimeoutError, ValueError) as exc:
            raise CapabilityError(
                "Repository download failed; retry later or upload a Skill ZIP"
            ) from exc
