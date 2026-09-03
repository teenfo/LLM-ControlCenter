#!/usr/bin/env sh
# 백업. **`prompt_cipher` 를 제외한다.**
#
#   ./backup.sh /mnt/nas/llmcc
#
# 7일 뒤 암호문을 지워도 30일 전 백업을 복원하면 지워졌어야 할 원문이 되살아난다.
# 그래서 백업에 암호문을 담지 않는 것이 기본이다 — 보관 기간 설정이 백업 앞에서
# 무의미해지지 않게.
#
# **마스터 KEK 는 이 백업에 담기지 않는다.** 같은 곳에 두면 백업 유출이 곧
# 원문 유출이다. KEK 는 따로, 다른 곳에 보관하세요.

set -eu

# 호스트의 파이썬. **`python` 이 아니라 `python3` 이다** — README 가 권장하는
# 데모 OS(Xubuntu 24.04)에 `python` 바이너리가 없어서, 그대로 두면 이 스크립트가
# 설치처의 절반에서 "command not found" 로 끝난다.
PY="${LCC_PYTHON:-python3}"
command -v "$PY" >/dev/null 2>&1 || PY=python

DEST="${1:-./backups}"
STAMP=$(date +%Y%m%d-%H%M%S)
WORK=$(mktemp -d)
trap 'rm -rf "$WORK"' EXIT

mkdir -p "$DEST"

if docker compose ps --status running 2>/dev/null | grep -q controlcenter; then
  docker compose exec -T controlcenter python -m app.backup /data/controlcenter.db /tmp/backup.db
  docker compose cp controlcenter:/tmp/backup.db "$WORK/controlcenter.db" >/dev/null
  docker compose exec -T controlcenter rm -f /tmp/backup.db
else
  DB="${LCC_DATA_DIR:-./data}/controlcenter.db"
  [ -f "$DB" ] || { printf 'DB 를 찾을 수 없습니다: %s\n' "$DB" >&2; exit 1; }
  "$PY" -m app.backup "$DB" "$WORK/controlcenter.db"
fi

cp -r config "$WORK/config"
{
  printf 'created_at=%s\n' "$STAMP"
  printf 'prompt_cipher=excluded\n'
  printf 'master_key=NOT_INCLUDED\n'
} > "$WORK/MANIFEST"

TARBALL="$DEST/llmcc-backup-$STAMP.tgz"
tar czf "$TARBALL" -C "$WORK" .
printf '  · 백업: %s\n' "$TARBALL"
printf '  ~ 마스터 KEK 는 이 파일에 없습니다. **다른 곳에** 따로 보관하세요.\n'
