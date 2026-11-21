"""Publish the local project conversation functions under KnowBench's service identity."""

import asyncio

from app.application.errors.exceptions import ServiceUnavailableError
from app.domain.workbench.native_functions import PROJECT_REVISIONS
from app.infrastructure.external.downstream_service import DownstreamServiceClient
from core.config import get_settings


async def publish_revision(client, revision):
    path = f"/api/internal/v1/method-skill-center/functions/{revision.name}/{revision.version}"
    for delay in (0.25, 0.5, None):
        try:
            receipt = await client.request_json(
                "PUT",
                path,
                operation_id=revision.digest,
                json_body=revision.contract.model_dump(),
            )
        except ServiceUnavailableError:
            if delay is None:
                raise
            await asyncio.sleep(delay)
            continue
        if receipt != revision.model_dump():
            raise ValueError("KnowBench returned a different function revision")
        return revision.digest


async def publish_project_functions(client):
    published = {}
    for name, build in PROJECT_REVISIONS.items():
        published[name] = await publish_revision(client, build())
    return published


async def main():
    settings = get_settings()
    if settings.downstream_scope.strip() != "knowbench:function-publish":
        raise ValueError("Expected a knowbench:function-publish service identity")
    print(await publish_project_functions(DownstreamServiceClient(settings)))


if __name__ == "__main__":
    asyncio.run(main())
