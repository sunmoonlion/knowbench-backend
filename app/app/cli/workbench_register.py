"""登记一台本地机器（环境）与一个沙箱给某个已登录过的用户——第一期手工版（0001-workbench）。

正式版由本地代理自己登记环境、由沙箱池登记沙箱（D10 令牌签发之后）。现在由运维在 api 容器里跑：

    python -m app.cli.workbench_register --email demo@example.com --environment-name this-pc \\
        --root /home/me/research --sandbox-url ws://sandbox-demo.sandbox-pool.svc:47800 \\
        --token-ref env:SANDBOX_DEMO_APP_SERVER_TOKEN

幂等：同名环境 / 同地址沙箱已存在则复用。只打印 id，不打印令牌。
"""

from __future__ import annotations

import argparse
import asyncio
import json
import sys

from sqlalchemy import text

from app.bootstrap.workbench import workbench_store
from app.domain.workbench.projects import CODEX_VERSION
from app.infrastructure.storage.postgres import get_postgres


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--email", required=True, help="auth_user.email of a user who has logged in"
    )
    parser.add_argument("--environment-name", default="this-pc")
    parser.add_argument(
        "--root",
        action="append",
        required=True,
        help="whitelisted root on the user's machine (repeatable)",
    )
    parser.add_argument("--codex-version", default=CODEX_VERSION)
    parser.add_argument("--agent-version", default="0.1.0")
    parser.add_argument(
        "--sandbox-url", required=True, help="ws://…:47800 of the user's app-server"
    )
    parser.add_argument(
        "--token-ref",
        required=True,
        help="env:NAME | file:/path | inline:… (how the runner finds the capability token)",
    )
    return parser.parse_args(argv)


async def register(session_factory, args: argparse.Namespace) -> dict:
    async with session_factory() as s:
        row = (
            await s.execute(
                text(
                    "select id from auth_user where email = :e order by created_at limit 1"
                ),
                {"e": args.email},
            )
        ).first()
        if row is None:
            raise SystemExit(
                f"no auth_user with email {args.email}: the user must log in once first"
            )
        owner = str(row[0])
        repo = workbench_store(s)
        envs = await repo.list_environments(owner_actor_id=owner)
        sbs = await repo.list_sandboxes(owner_actor_id=owner)
        async with repo.transaction():
            env = next((e for e in envs if e["name"] == args.environment_name), None)
            env_id = (
                str(env["id"])
                if env
                else await repo.register_environment(
                    owner_actor_id=owner,
                    name=args.environment_name,
                    agent_version=args.agent_version,
                    codex_version=args.codex_version,
                    roots=list(args.root),
                    ceiling={"sandbox": "workspace-write", "network": False},
                )
            )
            sb = next((x for x in sbs if x["app_server_url"] == args.sandbox_url), None)
            sb_id = (
                str(sb["id"])
                if sb
                else await repo.register_sandbox(
                    owner_actor_id=owner,
                    app_server_url=args.sandbox_url,
                    token_ref=args.token_ref,
                    codex_version=args.codex_version,
                )
            )
        return {
            "owner_actor_id": owner,
            "environment_id": env_id,
            "environment_reused": env is not None,
            "sandbox_id": sb_id,
            "sandbox_reused": sb is not None,
        }


async def run(argv: list[str] | None = None) -> int:
    args = parse_args(argv)
    pg = get_postgres()
    await pg.init()
    try:
        result = await register(pg.session_factory, args)
    finally:
        await pg.shutdown()
    print(json.dumps(result, ensure_ascii=False))
    return 0


def main() -> int:
    return asyncio.run(run(sys.argv[1:]))


if __name__ == "__main__":
    raise SystemExit(main())
