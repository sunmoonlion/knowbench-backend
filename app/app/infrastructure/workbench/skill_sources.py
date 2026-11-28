"""Read-only public GitHub downloads with pinned DNS/TLS and bounded responses."""

import asyncio
import json
import re
from urllib.parse import quote

import httpx

from app.domain.workbench.capabilities import CapabilityError
from app.domain.workbench.skill_installation import repository_parts
from app.infrastructure.workbench.capability_mcp import public_address


class RevisionNotFound(CapabilityError):
    pass


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
                if response.status_code in (404, 422) and host == "api.github.com":
                    raise RevisionNotFound("Repository revision was not found")
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
                    parts = ([ref] if ref else ["HEAD"]) + (
                        prefix.split("/") if prefix else []
                    )
                    if len(parts) > 24:
                        raise CapabilityError(
                            "Repository link exceeds the directory depth limit"
                        )
                    for split in range(len(parts), 0, -1):
                        candidate = quote("/".join(parts[:split]), safe="")
                        try:
                            raw = await self.download(
                                f"https://api.github.com/repos/{owner}/{repo}/commits/{candidate}",
                                limit=1024 * 1024,
                            )
                        except RevisionNotFound:
                            if split == 1:
                                raise
                            continue
                        commit = json.loads(raw).get("sha")
                        prefix = "/".join(parts[split:])
                        break
                else:
                    # Installation selects an exact path and digest in a pinned archive.
                    prefix = ""
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
