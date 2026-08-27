#!/usr/bin/env sh
# 복원. **복원 절차가 없으면 백업은 없는 것과 같다.**
#
#   ./restore.sh ./backups/llmcc-backup-20260101-120000.tgz
#
# 두 가지를 반드시 알린다:
#   ① 스키마 버전 호환성 — 구버전 백업을 신버전에 넣을 수 있는지
#   ② **복원이 역할 오버라이드를 되돌린다** — 오버라이드는 코드가 아니라 데이터라서
#      백업 시점의 모델 선택이 조용히 되살아난다

set -eu

ARCHIVE="${1:-}"
[ -n "$ARCHIVE" ] || { printf '사용법: %s <백업.tgz>\n' "$0" >&2; exit 2; }
[ -f "$ARCHIVE" ] || { printf '백업 파일이 없습니다: %s\n' "$ARCHIVE" >&2; exit 2; }

WORK=$(mktemp -d)
trap 'rm -rf "$WORK"' EXIT
tar xzf "$ARCHIVE" -C "$WORK"

[ -f "$WORK/controlcenter.db" ] || { printf '백업에 DB 가 없습니다.\n' >&2; exit 2; }

printf '\n복원 전 확인\n\n'
[ -f "$WORK/MANIFEST" ] && sed 's/^/  · /' "$WORK/MANIFEST"

python - "$WORK/controlcenter.db" <<'PY'
import sqlite3, sys
conn = sqlite3.connect(sys.argv[1])
row = conn.execute("SELECT value FROM meta WHERE key='schema_version'").fetchone()
version = int(row[0]) if row else 0
overrides = conn.execute("SELECT COUNT(*) FROM role_overrides").fetchone()[0]
tenants = conn.execute("SELECT COUNT(*) FROM tenants WHERE purged_at IS NULL").fetchone()[0]
jobs = conn.execute("SELECT COUNT(*) FROM jobs").fetchone()[0]
cipher = conn.execute("SELECT COUNT(*) FROM jobs WHERE prompt_cipher IS NOT NULL").fetchone()[0]
conn.close()

print(f"  · 스키마 버전 {version}")
print(f"  · 테넌트 {tenants} · 작업 {jobs}")
print(f"  · 암호문 {cipher}건" + ("" if cipher else " (백업에서 제외됨)"))
if overrides:
    print()
    print(f"  ! 역할 오버라이드 {overrides}건이 이 백업 시점 값으로 **되돌아갑니다.**")
    print("    오버라이드는 코드가 아니라 데이터이므로, 그 사이에 바꾼 모델 선택이")
    print("    조용히 되살아납니다. 복원 후 관제 UI 에서 확인하세요.")
PY

CURRENT_SCHEMA=$(python -c "
import sys
sys.path.insert(0, '.')
from app.store import SCHEMA_VERSION
print(SCHEMA_VERSION)
" 2>/dev/null || echo "?")
printf '  · 현재 제품의 스키마 버전 %s\n' "$CURRENT_SCHEMA"

printf '\n계속하려면 정확히 "restore" 를 입력하세요: '
read -r answer
[ "$answer" = "restore" ] || { printf '취소했습니다.\n'; exit 1; }

if docker compose ps --status running 2>/dev/null | grep -q controlcenter; then
  docker compose stop controlcenter
  docker compose cp "$WORK/controlcenter.db" controlcenter:/data/controlcenter.db
  docker compose start controlcenter
else
  DATA="${LCC_DATA_DIR:-./data}"
  mkdir -p "$DATA"
  cp "$DATA/controlcenter.db" "$DATA/controlcenter.db.before-restore" 2>/dev/null || true
  cp "$WORK/controlcenter.db" "$DATA/controlcenter.db"
fi

printf '\n복원 완료. ./doctor.sh 로 확인하세요.\n\n'
