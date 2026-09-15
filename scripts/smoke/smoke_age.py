"""P0-INF-06 冒烟：PG18 + Apache AGE 可加载并执行 openCypher。

用法：uv run python scripts/smoke/smoke_age.py [--dsn postgresql://...]
（read-only 验证：建临时图 → CREATE/RETURN → DROP）
"""

from __future__ import annotations

import argparse
import asyncio
import sys

DSN = "postgresql://loomvec:loomvec@localhost:5433/loomvec"
GRAPH = "smoke_age_graph"


async def main(dsn: str) -> int:
    import asyncpg

    conn = await asyncpg.connect(dsn, timeout=10)
    try:
        await conn.execute("CREATE EXTENSION IF NOT EXISTS age")
        await conn.execute("LOAD 'age'")
        await conn.execute('SET search_path = ag_catalog, "$user", public')
        await conn.execute(f"SELECT create_graph('{GRAPH}')")
        try:
            query = (
                f"SELECT * FROM ag_catalog.cypher('{GRAPH}', $$ "
                "CREATE (n:SmokeCheck {name: 'loomvec'}) RETURN n.name "
                "$$) AS (name agtype)"
            )
            rows = await conn.fetch(query)
            name = rows[0]["name"]
            assert "loomvec" in str(name), f"openCypher 返回异常：{name}"
            print(f"[smoke:age] OK — openCypher CREATE/RETURN 成功（{name}）")
        finally:
            await conn.execute(f"SELECT drop_graph('{GRAPH}', true)")
        return 0
    finally:
        await conn.close()


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--dsn", default=DSN)
    args = parser.parse_args()
    try:
        raise SystemExit(asyncio.run(main(args.dsn)))
    except Exception as e:
        print(f"[smoke:age] FAIL — {type(e).__name__}: {e}", file=sys.stderr)
        raise SystemExit(1) from e
