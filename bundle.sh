#!/usr/bin/env sh
# 에어갭 번들. 인터넷 없는 설치처로 가져갈 tar 하나를 만든다.
#
#   ./bundle.sh                 # 이미지 + 소스 + 설정
#   ./bundle.sh --with-models   # + 모델 가중치(별도 준비 필요)
#
# 담는 것: 도커 이미지 tar · 앱 소스 · 설정 · 정적 자산 · 클라이언트 · 문서.
# **외부 CDN 자산은 처음부터 안 쓰므로 따로 담을 것이 없다** — swagger-ui 같은 것을
# 런타임에 받아 오는 설계였다면 여기서 그것도 담아야 했다.
#
# 에어갭 모드에서는 **클라우드 티어가 자동 비활성화되고 UI 에 그 사실이 표시된다.**
# 설정에 남아 있는데 조용히 실패하는 것이 최악이다.

set -eu

VERSION="${LCC_VERSION:-0.1.0}"
STAMP=$(date +%Y%m%d)
OUT="llm-controlcenter-airgap-${VERSION}-${STAMP}.tgz"
WORK=$(mktemp -d)
trap 'rm -rf "$WORK"' EXIT

STAGE="$WORK/llm-controlcenter"
mkdir -p "$STAGE"

printf '\n에어갭 번들 만드는 중 — %s\n\n' "$OUT"

if command -v docker >/dev/null 2>&1 && docker info >/dev/null 2>&1; then
  printf '  · 이미지 빌드\n'
  docker build -t "llm-controlcenter:${VERSION}" . >/dev/null
  printf '  · 이미지 저장 (docker load 로 푸는 tar)\n'
  docker save "llm-controlcenter:${VERSION}" -o "$STAGE/image.tar"
  # nginx 는 TLS 프로파일에서만 쓰지만, 에어갭에서 나중에 받을 수 없으므로 함께 담는다.
  docker pull nginx:1.27-alpine >/dev/null 2>&1 \
    && docker save nginx:1.27-alpine -o "$STAGE/image-nginx.tar" \
    && printf '  · nginx 이미지 포함 (TLS 프로파일용)\n'
else
  printf '  ~ docker 가 없어 이미지를 담지 못했습니다. 소스만 담습니다.\n'
  printf '    설치처에서 pip install -e . 로 네이티브 실행할 수 있습니다.\n'
fi

printf '  · 소스·설정·자산\n'
for item in app config locales static clients pyproject.toml compose.yml Dockerfile \
            preflight.sh doctor.sh backup.sh restore.sh README.md docs; do
  [ -e "$item" ] && cp -r "$item" "$STAGE/"
done

if [ "${1:-}" = "--with-models" ]; then
  if [ -d models ]; then
    printf '  · 모델 가중치\n'
    cp -r models "$STAGE/models"
  else
    printf '  ~ ./models 디렉터리가 없어 가중치를 담지 못했습니다.\n'
  fi
fi

cat > "$STAGE/AIRGAP.md" <<'MD'
# 에어갭 설치

```sh
tar xzf llm-controlcenter-airgap-*.tgz && cd llm-controlcenter
docker load -i image.tar
[ -f image-nginx.tar ] && docker load -i image-nginx.tar
./preflight.sh
LCC_AIRGAP=1 docker compose up -d
docker compose logs controlcenter    # 최초 기동 값이 여기 한 번 찍힌다
```

## 에어갭 모드에서 달라지는 것

- **클라우드 티어가 자동 비활성화됩니다.** `data_boundary: external` 노드는
  등록 자체가 거부되고, 관제 UI 에 에어갭 모드임이 상시 표시됩니다.
  설정에 남아 있는데 조용히 실패하는 것이 최악이기 때문입니다.
- 모델 카탈로그는 이 번들에 담긴 목록이 전부입니다. 제품은 모델 레지스트리를
  **스크레이핑하지 않습니다** — 검색 API 가 없어 HTML 파싱에 기대야 하고,
  그러면 남의 사이트 개편에 제품이 끌려 죽습니다.
- 관제 UI 는 외부 CDN 을 쓰지 않으므로 그대로 뜹니다.

## 업그레이드

새 번들을 받아 `docker load` 하고 `docker compose up -d` 합니다.
스키마는 ADD COLUMN 전용이라 **구버전이 신버전 DB 를 읽을 수 있습니다** —
롤백은 이미지 태그를 되돌리는 것으로 끝납니다.
MD

printf '  · 매니페스트\n'
{
  printf 'version=%s\n' "$VERSION"
  printf 'created_at=%s\n' "$STAMP"
  printf 'airgap=true\n'
  [ -f "$STAGE/image.tar" ] && printf 'image=llm-controlcenter:%s\n' "$VERSION"
  [ -d "$STAGE/models" ] && printf 'models=included\n'
} > "$STAGE/MANIFEST"

tar czf "$OUT" -C "$WORK" llm-controlcenter
printf '\n  번들: %s (%s)\n\n' "$OUT" "$(du -h "$OUT" | cut -f1)"
