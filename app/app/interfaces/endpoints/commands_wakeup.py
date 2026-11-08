"""叫醒 runner 去领命令：委托、停下、交回这类改动写完库以后发一个 Redis 消息。

发不出去不要紧：runner 每轮对账照样会领到，只是慢一点。工作台路由和运行方路由共用。
"""

from __future__ import annotations

from app.infrastructure.storage.redis import get_redis
from core.config import get_settings


async def publish_commands_wakeup() -> None:
    try:
        await get_redis().client.publish(
            f"{get_settings().workbench_redis_key_prefix}:commands", "1"
        )
    except Exception:  # noqa: BLE001
        pass
