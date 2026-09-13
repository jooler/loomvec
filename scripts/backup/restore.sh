#!/usr/bin/env bash
# P4-INF-03 恢复脚本：从备份目录恢复 PG 与对象存储。
#
# 用法：
#   ./scripts/backup/restore.sh <pg_url> <storage_endpoint> <backup_dir>
# 注意：恢复会覆盖目标库/桶内容——仅限演练环境或灾难恢复场景。
set -euo pipefail

PG_URL="${1:?用法: restore.sh <pg_url> <storage_endpoint> <backup_dir>}"
STORAGE="${2:?缺少 storage endpoint}"
BACKUP_DIR="${3:?缺少 backup_dir}"

[ -f "$BACKUP_DIR/db.dump" ] || { echo "缺少 $BACKUP_DIR/db.dump" >&2; exit 1; }

echo "[restore] 从 $BACKUP_DIR 恢复"
pg_restore --clean --if-exists --no-owner -d "$PG_URL" "$BACKUP_DIR/db.dump"
echo "[restore] PG 恢复完成"

if command -v mc >/dev/null 2>&1 && [ -d "$BACKUP_DIR/objects" ]; then
  mc alias set loomvec-rs "$STORAGE" "${LOOMVEC_STORAGE_KEY:-loomvec}" "${LOOMVEC_STORAGE_SECRET:-loomvec-secret}" >/dev/null
  mc mirror --overwrite "$BACKUP_DIR/objects/loomvec-raw" "loomvec-rs/loomvec-raw"
  mc mirror --overwrite "$BACKUP_DIR/objects/loomvec-derived" "loomvec-rs/loomvec-derived"
  echo "[restore] 对象存储恢复完成"
fi
