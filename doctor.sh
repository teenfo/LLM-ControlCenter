#!/usr/bin/env sh
# 진단. 언제든 돌릴 수 있다.
#
#   ./doctor.sh              # 설정·스키마·키·유예 모드
#   ./doctor.sh --probe      # 노드 도달성까지
#   ./doctor.sh --bundle     # 지원 요청용 번들 (비밀은 마스킹됨)
#
# 도커로 뜬 설치는 컨테이너 안에서, 네이티브 설치는 그대로 돈다.

set -eu

ARGS=""
BUNDLE=""
for arg in "$@"; do
  case "$arg" in
    --bundle) BUNDLE="diagnostics-$(date +%Y%m%d-%H%M%S).json" ;;
    *) ARGS="$ARGS $arg" ;;
  esac
done
[ -n "$BUNDLE" ] && ARGS="$ARGS --bundle /data/$BUNDLE"

if docker compose ps --status running 2>/dev/null | grep -q controlcenter; then
  # shellcheck disable=SC2086
  docker compose exec -T controlcenter python -m app doctor $ARGS
  status=$?
  if [ -n "$BUNDLE" ]; then
    docker compose cp "controlcenter:/data/$BUNDLE" "./$BUNDLE" >/dev/null 2>&1 \
      && printf '  · 진단 번들: ./%s\n' "$BUNDLE"
  fi
  exit $status
fi

[ -n "$BUNDLE" ] && ARGS=$(printf '%s' "$ARGS" | sed "s|/data/$BUNDLE|./$BUNDLE|")
# shellcheck disable=SC2086
exec python -m app doctor $ARGS
