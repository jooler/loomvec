"""测试会话的线上库硬防护：测试将使用的 Postgres 若是线上库，pytest 在建立任何库连接之前直接退出。

背景：Settings 的内置默认值与仓库 .env 都指向本机 compose 的线上 PG（35433），
`env -u LOOMVEC_POSTGRES__URL` 无效（.env 会被重新加载）。凡是没显式隔离、
走真实 settings 的用例都会读写线上库（曾因此污染 users / spaces / system_config）。

判定（只看即将生效的 `Settings().postgres.url`，环境变量 > .env > 内置默认）：
- 主机不是回环地址：一律拒绝（远端库不是测试库）；
- 回环地址 + 线上端口（35433）：本机该端口有进程在监听才拒绝（TCP 探测，不是库连接）；
  无人监听时无库可污染（如 CI 无基础设施栈），放行，需要库的用例自行失败/跳过；
- 其他端口（testcontainers 随机端口、临时库、`127.0.0.1:1` 这类刻意不可达地址）：放行。

跑本包测试：`LOOMVEC_POSTGRES__URL=<临时库 DSN> pytest services/api/tests`；
只跑不碰库的用例可指向不可达地址（如 `postgresql+asyncpg://x:x@127.0.0.1:1/x`）。
"""

from __future__ import annotations

import socket

from sqlalchemy.engine import make_url

# 线上 compose 的宿主机 PG 端口（deploy/compose/.env 的 POSTGRES_PORT，亦即 Settings 默认值端口）
LIVE_PG_PORTS = frozenset({35433})
_LOOPBACK_HOSTS = frozenset({"localhost", "127.0.0.1", "::1"})


def _listening(host: str, port: int) -> bool:
    try:
        with socket.create_connection((host, port), timeout=0.5):
            return True
    except OSError:
        return False


def live_db_violation(url: str, *, listening=_listening) -> str | None:
    """返回拒绝原因；URL 可安全用于测试返回 None。"""
    parsed = make_url(url)
    host = parsed.host or "localhost"
    port = parsed.port or 5432
    where = f"{host}:{port}/{parsed.database}"
    if host not in _LOOPBACK_HOSTS:
        return f"{where} 不是本机地址，疑似线上/远端库"
    if port in LIVE_PG_PORTS and listening(host, port):
        return f"{where} 是线上 compose 库（端口 {port} 正在监听）"
    return None


def effective_postgres_url() -> str:
    from loomvec.core.config import Settings

    return Settings().postgres.url  # 含 .env；只读配置，不建立任何连接


def exit_message(reason: str) -> str:
    return (
        f"拒绝运行测试：{reason}。\n"
        "测试会写库，不得指向线上库。请启动临时 PG 并用环境变量覆盖（环境变量优先于 .env）：\n"
        "  docker run -d --rm --name lv-test-pg -p 127.0.0.1:0:5432 \\\n"
        "    -e POSTGRES_USER=loomvec -e POSTGRES_PASSWORD=loomvec -e POSTGRES_DB=loomvec \\\n"
        "    loomvec/postgres-age:18   # 再用 docker port lv-test-pg 查端口\n"
        "  LOOMVEC_POSTGRES__URL=postgresql+asyncpg://loomvec:loomvec@127.0.0.1:<端口>/loomvec \\\n"
        "    pytest ...\n"
        "只跑不碰库的用例，可指向不可达地址：\n"
        "  LOOMVEC_POSTGRES__URL=postgresql+asyncpg://x:x@127.0.0.1:1/x pytest ..."
    )
