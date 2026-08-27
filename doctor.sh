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
  # **doctor 가 고장을 찾으면 0 이 아닌 코드로 끝난다.** `set -e` 아래에서 그것을
  # 그냥 부르면 스크립트가 여기서 죽고 아래 번들 복사에 도달하지 못한다 —
  # 번들이 **정확히 필요할 때** 안 만들어지는 구조였다. 지원 요청용 산출물인데.
  status=0
  # shellcheck disable=SC2086
  docker compose exec -T controlcenter python -m app doctor $ARGS || status=$?

  if [ -n "$BUNDLE" ]; then
    if docker compose cp "controlcenter:/data/$BUNDLE" "./$BUNDLE" >/dev/null 2>&1; then
      printf '  · 진단 번들: ./%s\n' "$BUNDLE"
      docker compose exec -T controlcenter rm -f "/data/$BUNDLE" >/dev/null 2>&1 || true
    else
      # 조용히 넘어가면 사용자는 번들이 있다고 믿고 지원 요청을 보낸다.
      printf '  ! 진단 번들을 가져오지 못했습니다 (컨테이너: /data/%s)\n' "$BUNDLE" >&2
      [ "$status" -eq 0 ] && status=1
    fi
  fi
  exit $status
fi

[ -n "$BUNDLE" ] && ARGS=$(printf '%s' "$ARGS" | sed "s|/data/$BUNDLE|./$BUNDLE|")
# shellcheck disable=SC2086
exec python -m app doctor $ARGS
