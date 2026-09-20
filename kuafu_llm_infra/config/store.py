"""Redis 配置存储：只存一个 JSON 字符串。"""

from __future__ import annotations

from typing import Optional

import redis.asyncio as aioredis


class RedisConfigStore:

    def __init__(self, url: str, key_prefix: str = "kuafu_llm_infra:") -> None:
        self._redis = aioredis.from_url(url, decode_responses=True, socket_timeout=5.0, socket_connect_timeout=5.0)
        self._key = key_prefix + "config:v2"

    async def save(self, config_json: str) -> None:
        await self._redis.set(self._key, config_json)

    async def load(self) -> Optional[str]:
        return await self._redis.get(self._key)
