"""第五个运行角色：工作台 runner。同一镜像，`python -m app.bootstrap.runner`。

长连接不适合 Celery，所以单独一个 asyncio 进程：保持到各沙箱 app-server 的 WebSocket，投影事件，执行命令。
第一期单实例；多实例时命令由 `for update skip locked` 分摊，但同一沙箱的连接会重复——多实例分片留到 D9。
"""

from __future__ import annotations

import asyncio
import logging
import signal

from app.bootstrap.workbench import (
    build_deliverable_check,
    build_grant_releaser,
    build_machine_sync,
    build_runner,
    build_test_conductor,
    event_publisher,
)
from app.infrastructure.logging.logging import setup_logging
from app.infrastructure.storage.postgres import get_postgres
from app.infrastructure.storage.redis import get_redis
from core.config import get_settings

logger = logging.getLogger(__name__)


async def main() -> None:
    settings = get_settings()
    setup_logging()
    if not settings.workbench_enabled:
        logger.error("WORKBENCH_ENABLED is false; runner refuses to start")
        raise SystemExit(2)
    await get_redis().init()
    await get_postgres().init()
    deliverables = build_deliverable_check(
        get_postgres().session_factory,
        relay_url=settings.workbench_relay_admin_url,
        signing_key=settings.workbench_token_signing_key,
        issuer_name=settings.workbench_token_issuer,
    )
    runner = build_runner(
        get_postgres().session_factory,
        publisher=event_publisher(
            get_redis().client, settings.workbench_redis_key_prefix
        ),
        runner_id=settings.workbench_runner_id,
        environment_key=settings.workbench_environment_key,
        poll_seconds=settings.workbench_poll_seconds,
        records=settings.workbench_records_mcp_enabled,
        prices=settings.workbench_model_prices(),
        deliverables=deliverables,
    )
    # 我的机器：同一个进程里另起一条线，隔一会儿和会合点对一遍谁在线（没配管理通道就没有这条线）
    machines = build_machine_sync(
        get_postgres().session_factory,
        relay_admin_url=settings.workbench_relay_admin_url,
        relay_admin_token=settings.workbench_relay_admin_token,
        interval_seconds=settings.workbench_machine_sync_seconds,
    )
    stopping = asyncio.Event()

    def stop() -> None:
        runner.stop()
        stopping.set()

    loop = asyncio.get_running_loop()
    for sig in (signal.SIGINT, signal.SIGTERM):
        loop.add_signal_handler(sig, stop)
    logger.info("service_starting service=%s role=runner", settings.service_name)
    syncing = asyncio.create_task(machines.run_forever(stopping)) if machines else None
    if syncing is None:
        logger.warning("machine sync is off: relay admin channel not configured")
    # 作为运行方跑分身测试（SDD 0027 §3.4）：同一个进程里再起一条线推进在跑的测试
    tests = build_test_conductor(get_postgres().session_factory, settings)
    testing = (
        asyncio.create_task(
            tests.run_forever(stopping, settings.knowbot_test_poll_seconds)
        )
        if tests
        else None
    )
    # 请专家用自己的数据（SDD 0042）：委托结束后交还它的运行授权
    releasing = asyncio.create_task(
        build_grant_releaser(get_postgres().session_factory).run_forever(stopping)
    )
    try:
        await runner.run_forever()
    finally:
        stopping.set()
        await releasing
        if syncing is not None:
            await syncing
        if testing is not None:
            await testing
        await get_postgres().shutdown()
        await get_redis().shutdown()


if __name__ == "__main__":
    asyncio.run(main())
