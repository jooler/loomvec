"""API 启动入口：端口单源为环境配置（LOOMVEC_API_PORT，默认 38080）。

用法：`uv run python -m loomvec.api [--reload]`（Makefile dev 目标使用）；
`--reload` 透传 uvicorn（开发热重载）。
"""

from __future__ import annotations

import argparse

import uvicorn

from loomvec.core.config import get_settings


def main() -> None:
    parser = argparse.ArgumentParser(description="LoomVec API server")
    parser.add_argument("--reload", action="store_true", help="启用热重载（开发）")
    args = parser.parse_args()

    settings = get_settings()
    uvicorn.run(
        "loomvec.api.main:app",
        host="0.0.0.0",
        port=settings.api_port,
        reload=args.reload,
    )


if __name__ == "__main__":
    main()
