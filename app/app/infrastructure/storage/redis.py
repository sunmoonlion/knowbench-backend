import asyncio
import logging
from functools import lru_cache

from redis.asyncio import Redis

from core.config import get_settings

logger = logging.getLogger(__name__)


class RedisClient:
    def __init__(self):
        self._client: Redis | None = None
        self._settings = get_settings()
        self._lock = asyncio.Lock()

    async def init(self) -> None:
        async with self._lock:
            if self._client is not None:
                return
            kw: dict = dict(
                host=self._settings.redis_host,
                port=self._settings.redis_port,
                db=self._settings.redis_db,
                password=self._settings.redis_password,
                decode_responses=True,
            )
            if self._settings.redis_user:
                kw["username"] = self._settings.redis_user
            client = Redis(**kw)
            try:
                await client.ping()  # type: ignore[misc]
            except BaseException:
                await client.aclose()
                raise
            self._client = client
            logger.info("Redis初始化成功")

    async def shutdown(self) -> None:
        async with self._lock:
            if self._client is not None:
                await self._client.aclose()
                self._client = None
                logger.info("Redis连接已关闭")
        get_redis.cache_clear()

    @property
    def client(self) -> Redis:
        if self._client is None:
            raise RuntimeError("Redis未初始化")
        return self._client


@lru_cache
def get_redis() -> RedisClient:
    return RedisClient()
