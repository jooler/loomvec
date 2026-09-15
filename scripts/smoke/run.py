"""P0-INF-06 冒烟编排器：校验各组件可达 + AGE/Milvus/MinerU 能力验证。

用法（在 compose 栈就绪后）：
    uv run python scripts/smoke/run.py [--env-file deploy/compose/.env] [--skip api]

退出码 0 = 全绿。各检查项独立脚本位于本目录，可单独执行。
"""

from __future__ import annotations

import argparse
import subprocess
import sys
import urllib.request
from pathlib import Path

SMOKE_DIR = Path(__file__).resolve().parent
ROOT = SMOKE_DIR.parent.parent


def parse_env(path: Path) -> dict[str, str]:
    env: dict[str, str] = {}
    if not path.exists():
        return env
    for line in path.read_text().splitlines():
        line = line.strip()
        if line and not line.startswith("#") and "=" in line:
            key, value = line.split("=", 1)
            env[key] = value
    return env


def http_ok(url: str, timeout: float = 5.0) -> bool:
    try:
        with urllib.request.urlopen(url, timeout=timeout) as resp:
            return resp.status == 200
    except Exception:
        return False


def run_script(script: str, *args: str) -> tuple[bool, str]:
    proc = subprocess.run(
        [sys.executable, str(SMOKE_DIR / script), *args],
        capture_output=True,
        text=True,
        timeout=900,
    )
    return proc.returncode == 0, (proc.stdout + proc.stderr).strip().replace("\n", " | ")


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--env-file", default="deploy/compose/.env")
    parser.add_argument("--api-url", default="http://localhost:8080")  # 8000 是 MinerU
    parser.add_argument("--skip", nargs="*", default=[], help="跳过的检查项名称")
    args = parser.parse_args()

    env = parse_env(Path(args.env_file))
    pg = (
        f"postgresql://{env.get('POSTGRES_USER', 'loomvec')}"
        f":{env.get('POSTGRES_PASSWORD', 'loomvec')}"
        f"@localhost:{env.get('POSTGRES_PORT', '5433')}"
        f"/{env.get('POSTGRES_DB', 'loomvec')}"
    )
    redis_url = f"redis://localhost:{env.get('REDIS_PORT', '6379')}/0"
    rustfs = f"http://localhost:{env.get('RUSTFS_PORT', '9000')}"
    mineru = f"http://localhost:{env.get('MINERU_PORT', '8000')}"
    milvus = f"http://localhost:{env.get('MILVUS_PORT', '19530')}"

    checks: list[tuple[str, bool, str]] = []

    if "api" not in args.skip:
        ok = http_ok(f"{args.api_url}/healthz")
        checks.append(("api /healthz", ok, "200" if ok else "不可达"))

    script_checks = [
        ("postgres+age", "smoke_age.py", ("--dsn", pg)),
        ("redis", "smoke_redis.py", ("--url", redis_url)),
        ("rustfs", "smoke_storage.py", ("--endpoint", rustfs)),
        ("milvus", "smoke_milvus.py", ("--uri", milvus)),
        ("mineru", "smoke_mineru.py", ("--base-url", mineru)),
    ]
    for name, script, argv in script_checks:
        if name in args.skip:
            continue
        ok, detail = run_script(script, *argv)
        checks.append((name, ok, detail))

    print("LoomVec 冒烟检查（P0-INF-06）")
    print("=" * 64)
    failed = False
    for name, ok, detail in checks:
        print(f"  {'✅' if ok else '❌'} {name:<14} {detail}")
        failed |= not ok
    print("=" * 64)
    print("结果：全部通过 ✅" if not failed else "结果：存在失败项 ❌")
    return 1 if failed else 0


if __name__ == "__main__":
    raise SystemExit(main())
