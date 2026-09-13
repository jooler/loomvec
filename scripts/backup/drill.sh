#!/usr/bin/env bash
# P4-INF-03 恢复演练：备份 → 破坏性写入 → 恢复 → 校验行数一致。
# 在演练库（非生产）执行；通过标准：恢复后资产行数与备份时一致。
#
# 用法：./scripts/backup/drill.sh <演练pg_url> <storage_endpoint> <工作目录>
set -euo pipefail

PG_URL="${1:?用法: drill.sh <pg_url> <storage_endpoint> <workdir>}"
STORAGE="${2:?缺少 storage endpoint}"
WORK="${3:-/tmp/loomvec-drill}"
mkdir -p "$WORK"

echo "[drill] 1/4 备份"
"$(dirname "$0")/backup.sh" "$PG_URL" "$STORAGE" "$WORK" >/dev/null
LATEST="$(ls -t "$WORK" | head -1)"

BEFORE_ASSETS="$(psql "$PG_URL" -tAc 'select count(*) from asset')"
BEFORE_TENANTS="$(psql "$PG_URL" -tAc 'select count(*) from tenant')"

echo "[drill] 2/4 破坏性写入（模拟故障）"
psql "$PG_URL" -q -c "insert into tenant (name) values ('drill-tombstone') returning id" >/dev/null
psql "$PG_URL" -q -c "delete from asset where true" >/dev/null

echo "[drill] 3/4 恢复 $WORK/$LATEST"
"$(dirname "$0")/restore.sh" "$PG_URL" "$STORAGE" "$WORK/$LATEST" >/dev/null

echo "[drill] 4/4 校验"
AFTER_ASSETS="$(psql "$PG_URL" -tAc 'select count(*) from asset')"
AFTER_TENANTS="$(psql "$PG_URL" -tAc 'select count(*) from tenant')"
TOMBSTONE="$(psql "$PG_URL" -tAc "select count(*) from tenant where name = 'drill-tombstone'")"

if [ "$AFTER_ASSETS" = "$BEFORE_ASSETS" ] && [ "$AFTER_TENANTS" = "$BEFORE_TENANTS" ] && [ "$TOMBSTONE" = "0" ]; then
  echo "[drill] ✅ 恢复演练通过（assets=$AFTER_ASSETS tenants=$AFTER_TENANTS，破坏性写入被回滚）"
  exit 0
else
  echo "[drill] ❌ 恢复演练失败：assets $BEFORE_ASSETS→$AFTER_ASSETS tenants $BEFORE_TENANTS→$AFTER_TENANTS tombstone=$TOMBSTONE"
  exit 1
fi
