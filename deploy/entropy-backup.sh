#!/usr/bin/env bash
# entropy-arb 配置备份：config.yaml + profiles/ + .env（三样都在 gitignore 里）
# 由 entropy-backup.timer 每周日 03:00 触发；保留最近 8 份，归档权限 0600。
set -eu
ROOT=/root/code/entropy
DEST=/root/backups
KEEP=8
STAMP=$(date +%F)

FILES=()
[ -f "$ROOT/config.yaml" ] && FILES+=(config.yaml)
[ -d "$ROOT/profiles" ] && FILES+=(profiles)
[ -f "$ROOT/.env" ] && FILES+=(".env")
if [ ${#FILES[@]} -eq 0 ]; then
  echo "nothing to back up (no config.yaml / profiles / .env yet)"
  exit 0
fi

mkdir -p "$DEST"
chmod 700 "$DEST"
tar -C "$ROOT" -czf "$DEST/entropy-$STAMP.tar.gz" "${FILES[@]}"
chmod 600 "$DEST/entropy-$STAMP.tar.gz"

# 只保留最近 KEEP 份
ls -1t "$DEST"/entropy-*.tar.gz 2>/dev/null | tail -n +$((KEEP + 1)) | \
  xargs -r rm -f --
echo "backup ok: $DEST/entropy-$STAMP.tar.gz (${FILES[*]})"
