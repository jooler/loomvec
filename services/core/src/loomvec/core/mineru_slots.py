"""MinerU 解析名额：worker 多进程经 Redis 协调同时在 MinerU 上解析的任务数。

MinerU（尤其 CPU 版）逐个解析；不加约束时 worker 各进程同时提交，任务堆在 MinerU
队列里，排队时间吃掉 `mineru.timeout_seconds`，loomvec 判超时放弃后 MinerU 仍继续白跑。
名额满时调用方抛 `MineruBusyError`，由任务层延后重新入队（不占 worker 进程、不计失败）。

名额 = `capacity` 个 Redis 键（SET NX EX）；租约带 TTL，进程崩溃时到期自动释放。
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass
from typing import Any

# 仅当键值仍是本租约 token 时删除（TTL 到期后被他人取得的名额不误删）
_RELEASE_SCRIPT = (
    "if redis.call('get', KEYS[1]) == ARGV[1] then return redis.call('del', KEYS[1]) "
    "else return 0 end"
)


class MineruBusyError(Exception):
    """MinerU 名额已满：调用方应延后重试（非故障——不记失败、不计 autoretry 次数）。"""


@dataclass(frozen=True)
class MineruLease:
    key: str
    token: str


class MineruSlots:
    def __init__(
        self,
        redis: Any,
        *,
        capacity: int,
        ttl_seconds: int,
        prefix: str = "loomvec:mineru:slot",
    ) -> None:
        self._redis = redis
        self._capacity = capacity
        self._ttl = ttl_seconds
        self._prefix = prefix

    async def acquire(self) -> MineruLease | None:
        """取一个空闲名额；全满返回 None（不阻塞）。"""
        token = uuid.uuid4().hex
        for i in range(self._capacity):
            key = f"{self._prefix}:{i}"
            if await self._redis.set(key, token, nx=True, ex=self._ttl):
                return MineruLease(key=key, token=token)
        return None

    async def release(self, lease: MineruLease) -> None:
        await self._redis.eval(_RELEASE_SCRIPT, 1, lease.key, lease.token)

    async def seems_full(self) -> bool:
        """非原子的"大概率已满"探测：只用于在明知会 BusyError 时跳过昂贵的前置工作

        （下载原文件等），不作为正确性依据——真正的占用判定仍在 acquire() 的
        SET NX 上，这里的 TOCTOU 窗口只影响是否值得一试，不影响名额是否超发。
        """
        for i in range(self._capacity):
            if not await self._redis.exists(f"{self._prefix}:{i}"):
                return False
        return True
