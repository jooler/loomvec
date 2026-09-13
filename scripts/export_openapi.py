"""P0-API-03 契约导出：从 FastAPI app 生成 openapi.json（代码内注解即源）。

用法：uv run python scripts/export_openapi.py [--check]
- 默认写到仓库根 openapi.json（gitignore，不入库）；
- sdk-ts 生成：pnpm sdk:generate（读取本文件产物）；
- `--check` 供 CI 校验快照与当前 app 契约一致（失败即契约漂移）。
"""

from __future__ import annotations

import argparse
import json
import pathlib
import sys

ROOT = pathlib.Path(__file__).resolve().parent.parent
OUTPUT = ROOT / "openapi.json"
SNAPSHOT = ROOT / "services/api/tests/__snapshots__/openapi.json"


def export() -> dict:
    # 延迟导入：避免脚本被 CI 环境无关依赖拖累
    from loomvec.api.app import create_app

    app = create_app()
    spec = app.openapi()
    spec["info"]["version"] = "0.1.0"
    return spec


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--check", action="store_true", help="校验快照一致而非写出")
    args = parser.parse_args()

    spec = export()
    serialized = json.dumps(spec, ensure_ascii=False, indent=2, sort_keys=True) + "\n"

    if args.check:
        if not SNAPSHOT.exists():
            print(f"契约快照缺失：{SNAPSHOT}", file=sys.stderr)
            return 1
        expected = SNAPSHOT.read_text(encoding="utf-8")
        if expected != serialized:
            print("契约漂移：openapi 快照与当前 app 不一致。", file=sys.stderr)
            print(
                "请运行 uv run python scripts/export_openapi.py 后同步更新快照。",
                file=sys.stderr,
            )
            return 1
        print("契约快照校验通过。")
        return 0

    OUTPUT.write_text(serialized, encoding="utf-8")
    SNAPSHOT.write_text(serialized, encoding="utf-8")
    print(f"OpenAPI 契约已导出：{OUTPUT}（快照已同步：{SNAPSHOT}）")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
