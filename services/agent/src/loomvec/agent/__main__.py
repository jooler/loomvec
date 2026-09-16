"""agent 服务启动入口：端口单源 LOOMVEC_AGENT_PORT（默认 8090）。

用法：`uv run python -m loomvec.agent [--reload]`（dev.sh 使用）。
"""

from __future__ import annotations

import argparse

import uvicorn

from loomvec.core.config import get_settings


def main() -> None:
    parser = argparse.ArgumentParser(description="LoomVec Agent gateway")
    parser.add_argument("--reload", action="store_true", help="启用热重载（开发）")
    args = parser.parse_args()

    settings = get_settings()
    uvicorn.run(
        "loomvec.agent.app:app",
        host="127.0.0.1",  # 内网 only（api facade 转发；不对公网暴露）
        port=settings.agent_port,
        reload=args.reload,
    )


if __name__ == "__main__":
    main()
