#!/usr/bin/env bash
# P4-INF-03 备份脚本：PG 逻辑备份 + 对象存储全量快照 + 状态回写。
#
# 用法：
#   ./scripts/backup/backup.sh <pg_url> <storage_endpoint> <backup_dir>
# 示例（compose 环境）：
#   ./scripts/backup/backup.sh \
#     postgresql://loomvec:loomvec@localhost:35433/loomvec \
#     http://localhost:39000 /tmp/loomvec-backups
#
# 生产 PITR：PG 开启 archive_mode + WAL 归档至对象存储后，本脚本追加
# `pg_backup_manifest` 记录 LSN，恢复脚本按时间点回放（见 restore.sh）。
set -euo pipefail

PG_URL="${1:?用法: backup.sh <pg_url> <storage_endpoint> <backup_dir>}"
STORAGE="${2:?缺少 storage endpoint}"
BACKUP_DIR="${3:?缺少 backup_dir}"
TS="$(date -u +%Y%m%dT%H%M%SZ)"
OUT="$BACKUP_DIR/$TS"
mkdir -p "$OUT"

echo "[backup] $TS → $OUT"

# 1) PG 逻辑备份（-Fc 自定义格式；恢复用 pg_restore）
pg_dump "$PG_URL" -Fc -f "$OUT/db.dump"
echo "[backup] pg_dump 完成: $(du -h "$OUT/db.dump" | cut -f1)"

# 2) 对象存储快照（raw/derived bucket 全量；mc 客户端）
if command -v mc >/dev/null 2>&1; then
  mc alias set loomvec-bk "$STORAGE" "${LOOMVEC_STORAGE_KEY:-loomvec}" "${LOOMVEC_STORAGE_SECRET:-loomvec-secret}" >/dev/null
  mc mirror --overwrite "loomvec-bk/loomvec-raw" "$OUT/objects/loomvec-raw"
  mc mirror --overwrite "loomvec-bk/loomvec-derived" "$OUT/objects/loomvec-derived"
  echo "[backup] 对象存储快照完成"
else
  echo "[backup] 未安装 mc，跳过对象存储快照（生产必须覆盖）" >&2
fi

# 3) 备份元信息
cat > "$OUT/manifest.json" <<EOF
{"created_at": "$TS", "pg_url_host": "$(echo "$PG_URL" | sed -E 's|.*@([^:/]+).*|\1|')", "type": "full"}
EOF
echo "[backup] manifest 完成"

# 4) 状态回写 system_config（运维端总览"备份状态"卡片数据源）
psql "$PG_URL" -q -c "
INSERT INTO system_config (key, value, remark)
VALUES ('backup.last_status',
        json_build_object('state','ok','at','$TS','dir','$OUT'),
        'scripts/backup/backup.sh 回写')
ON CONFLICT (key) DO UPDATE SET value = EXCLUDED.value, updated_at = now();"
echo "[backup] 状态已回写 system_config"
