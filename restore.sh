#!/usr/bin/env sh
# 복원. **복원 절차가 없으면 백업은 없는 것과 같다.**
#
#   ./restore.sh ./backups/llmcc-backup-20260101-120000.tgz
#
# 두 가지를 반드시 알린다:
#   ① 스키마 버전 호환성 — 구버전 백업을 신버전에 넣을 수 있는지
#   ② **복원이 역할 오버라이드를 되돌린다** — 오버라이드는 코드가 아니라 데이터라서
#      백업 시점의 모델 선택이 조용히 되살아난다
#
# 조용히 데이터를 깨뜨리는 부분(낡은 `-wal` 제거 · 스키마 게이트 · 설정 되돌림)은
# `app/restore.py` 에 있다. 쉘에 두면 테스트가 닿지 않고, 닿지 않는 코드는 검증되지
# 않는다. 이 스크립트는 사람과 대화하고 컨테이너를 세웠다 켜는 일만 한다.

set -eu

ARCHIVE="${1:-}"
[ -n "$ARCHIVE" ] || { printf '사용법: %s <백업.tgz>\n' "$0" >&2; exit 2; }
[ -f "$ARCHIVE" ] || { printf '백업 파일이 없습니다: %s\n' "$ARCHIVE" >&2; exit 2; }

PY="${LCC_PYTHON:-python3}"
command -v "$PY" >/dev/null 2>&1 || PY=python

WORK=$(mktemp -d)
chmod 755 "$WORK"     # 컨테이너의 uid 10001 도 읽어야 한다
trap 'rm -rf "$WORK"' EXIT
tar xzf "$ARCHIVE" -C "$WORK"

[ -f "$WORK/controlcenter.db" ] || { printf '백업에 DB 가 없습니다.\n' >&2; exit 2; }

printf '\n복원 전 확인\n\n'
[ -f "$WORK/MANIFEST" ] && sed 's/^/  · /' "$WORK/MANIFEST"

# 스키마 호환성은 **여기서 막는다.** 숫자 두 개를 나란히 찍고 사람이 비교하기를
# 기대하는 것은 검사가 아니다 — 복원은 사고 난 뒤에 하는 일이라 그때 가장 급하다.
"$PY" -m app.restore inspect "$WORK/controlcenter.db" || exit 1

printf '\n계속하려면 정확히 "restore" 를 입력하세요: '
read -r answer
[ "$answer" = "restore" ] || { printf '취소했습니다.\n'; exit 1; }

if docker compose ps --status running 2>/dev/null | grep -q controlcenter; then
  docker compose stop controlcenter

  # **컨테이너 안에서, 앱과 같은 사용자로** 설치한다.
  #
  # 호스트에서 `docker compose cp` 로 DB 를 밀어 넣으면 파일이 root 소유로 들어간다.
  # 컨테이너는 uid 10001 로 돌기 때문에 그 DB 에 쓰지 못하고, 복원 직후
  # "attempt to write a readonly database" 로 죽는다. 여기서는 백업을 읽기 전용으로
  # 마운트하고 대상 파일을 **앱 사용자가 직접 만든다** — 소유권이 처음부터 맞는다.
  docker compose run --rm -v "$WORK:/restore:ro" --entrypoint python controlcenter \
    -m app.restore install /restore/controlcenter.db /data

  docker compose start controlcenter

  # 설정은 호스트의 바인드 마운트(`./config:/app/config:ro`)라 호스트에서 되돌린다.
  # 컨테이너 안에서는 읽기 전용이다.
  [ -d "$WORK/config" ] && "$PY" -m app.restore config "$WORK/config" ./config
else
  DATA="${LCC_DATA_DIR:-./data}"
  if [ -d "$WORK/config" ]; then
    "$PY" -m app.restore install "$WORK/controlcenter.db" "$DATA" "$WORK/config" ./config
  else
    "$PY" -m app.restore install "$WORK/controlcenter.db" "$DATA"
  fi
fi

printf '\n복원 완료. ./doctor.sh 로 확인하세요.\n\n'
