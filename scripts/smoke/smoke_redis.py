"""P0-INF-06 冒烟：Redis 可达（ping）。"""

from __future__ import annotations

import argparse
import sys

URL = "redis://localhost:36379/0"


def main(url: str) -> int:
    import redis

    client = redis.from_url(url, decode_responses=True, socket_connect_timeout=5)
    assert client.ping() is True, "PING 失败"
    print(f"[smoke:redis] OK — PING 通（{url}）")
    return 0


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--url", default=URL)
    args = parser.parse_args()
    try:
        raise SystemExit(main(args.url))
    except Exception as e:
        print(f"[smoke:redis] FAIL — {type(e).__name__}: {e}", file=sys.stderr)
        raise SystemExit(1) from e
