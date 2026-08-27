#!/usr/bin/env sh
# 설치 전 점검. **실패 사유를 사람 말로 낸다** — "exit 1" 만 주면 아무도 못 고친다.
#
#   ./preflight.sh
#
# 여기서 걸러야 할 것은 설치 중에 알면 늦는 것들이다: 포트 충돌 · 디스크 부족 ·
# 메모리 부족 · 도커 버전 · 그리고 **노드 구간이 신뢰 네트워크라는 전제**.

set -eu

PORT="${LCC_PORT:-8610}"
MIN_DISK_GB="${LCC_MIN_DISK_GB:-2}"
MIN_MEM_MB="${LCC_MIN_MEM_MB:-512}"

problems=0
warnings=0

ok()    { printf '  · %s\n' "$1"; }
warn()  { printf '  ~ %s\n' "$1"; warnings=$((warnings + 1)); }
fail()  { printf '  ! %s\n' "$1" >&2; problems=$((problems + 1)); }

printf '\nLLM ControlCenter — 설치 전 점검\n\n'

# -- 도커 --
if command -v docker >/dev/null 2>&1; then
  ok "docker $(docker --version 2>/dev/null | sed 's/,.*//')"
  if docker compose version >/dev/null 2>&1; then
    ok "docker compose $(docker compose version --short 2>/dev/null)"
  else
    fail "docker compose 플러그인이 없습니다. Docker 20.10+ 의 compose v2 가 필요합니다."
  fi
  docker info >/dev/null 2>&1 || fail "docker 데몬에 연결할 수 없습니다. 실행 중인지, 권한이 있는지 확인하세요."
else
  warn "docker 가 없습니다. 네이티브 실행 경로를 쓰려면: pip install -e . && python -m app --demo"
fi

# -- 포트 --
if command -v ss >/dev/null 2>&1; then
  ss -ltn 2>/dev/null | awk '{print $4}' | grep -qE "[:.]${PORT}\$" \
    && fail "포트 ${PORT} 가 이미 사용 중입니다. LCC_PORT 로 바꾸거나 그 프로세스를 내리세요." \
    || ok "포트 ${PORT} 사용 가능"
elif command -v netstat >/dev/null 2>&1; then
  netstat -ltn 2>/dev/null | awk '{print $4}' | grep -qE "[:.]${PORT}\$" \
    && fail "포트 ${PORT} 가 이미 사용 중입니다." \
    || ok "포트 ${PORT} 사용 가능"
else
  warn "포트 점검 도구(ss/netstat)가 없어 건너뜁니다."
fi

# -- 디스크 --
avail_kb=$(df -Pk . | awk 'NR==2 {print $4}')
avail_gb=$((avail_kb / 1024 / 1024))
if [ "$avail_gb" -lt "$MIN_DISK_GB" ]; then
  fail "디스크 여유 ${avail_gb}GB — 최소 ${MIN_DISK_GB}GB 가 필요합니다."
else
  ok "디스크 여유 ${avail_gb}GB"
fi

# -- 메모리 --
if [ -r /proc/meminfo ]; then
  total_mb=$(awk '/MemTotal/ {print int($2/1024)}' /proc/meminfo)
  if [ "$total_mb" -lt "$MIN_MEM_MB" ]; then
    fail "메모리 ${total_mb}MB — 컨트롤 플레인 최소치는 ${MIN_MEM_MB}MB 입니다."
  else
    ok "메모리 ${total_mb}MB"
  fi
  # 데모 노트북 기준. 4GB 에서 모델을 올리면 스왑이 결정적이다.
  swap_mb=$(awk '/SwapTotal/ {print int($2/1024)}' /proc/meminfo)
  [ "$swap_mb" -eq 0 ] && [ "$total_mb" -lt 8192 ] \
    && warn "스왑이 없습니다. 8GB 미만 호스트에서 모델을 올릴 계획이면 zram 을 권장합니다."
else
  warn "메모리 점검을 건너뜁니다(/proc/meminfo 없음)."
fi

# -- 키 디렉터리 소유권 --
#
# **컴포즈가 대신 만들게 두면 root 소유가 된다.** 컨테이너는 uid 10001 로 돌기
# 때문에 거기에 마스터 KEK 를 쓰지 못하고, `restart: unless-stopped` 아래에서
# 그것은 조용한 크래시 루프가 된다 — 설치처는 로그가 흐르는 화면만 본다.
# 그래서 도커가 만들기 **전에** 여기서 올바른 소유권으로 만들어 둔다.
KEYS_DIR="${LCC_KEYS_PATH:-./keys}"
CONTAINER_UID=10001
if [ ! -d "$KEYS_DIR" ]; then
  if mkdir -p "$KEYS_DIR" 2>/dev/null; then
    chmod 700 "$KEYS_DIR" 2>/dev/null || true
    ok "키 디렉터리 생성 (${KEYS_DIR})"
  else
    fail "키 디렉터리를 만들 수 없습니다: ${KEYS_DIR}"
  fi
fi
if [ -d "$KEYS_DIR" ]; then
  keys_uid=$(stat -c '%u' "$KEYS_DIR" 2>/dev/null || echo '')
  if [ -n "$keys_uid" ] && [ "$keys_uid" != "$CONTAINER_UID" ]; then
    # 네이티브 실행이면 문제가 아니다 — 도커로 띄울 때만 걸린다.
    if command -v docker >/dev/null 2>&1; then
      warn "키 디렉터리 ${KEYS_DIR} 가 uid ${keys_uid} 소유입니다. 컨테이너는 uid ${CONTAINER_UID} 로 돕니다.
      마스터 KEK 를 쓰지 못해 기동이 실패합니다. 다음을 실행하세요:
        sudo chown -R ${CONTAINER_UID}:${CONTAINER_UID} ${KEYS_DIR}"
    fi
  else
    ok "키 디렉터리 소유권 OK (${KEYS_DIR})"
  fi
fi

# -- 키 --
if [ -f "${KEYS_DIR}/master.key" ]; then
  ok "마스터 KEK 있음 (${KEYS_DIR}/master.key)"
  perms=$(stat -c '%a' "${KEYS_DIR}/master.key" 2>/dev/null || echo '')
  [ "$perms" = "600" ] || warn "마스터 KEK 권한이 ${perms:-알 수 없음} 입니다. 600 을 권장합니다."
elif [ -n "${LCC_PROMPT_KEY:-}" ]; then
  ok "LCC_PROMPT_KEY 가 설정돼 있습니다."
else
  warn "마스터 KEK 가 없습니다. 최초 기동에서 생성되며, 그때 **한 번만** 표시됩니다."
fi

# -- 전제를 명시한다 --
# 안 적으면 설치처가 노드를 공개망에 열어 두고 "제품이 알아서 지켜주겠지" 로 넘어간다.
printf '\n  전제:\n'
printf '    · data_boundary: internal 노드는 **신뢰 네트워크(사설망·VPN)** 에 있어야 합니다.\n'
printf '      Ollama 는 기본 무인증입니다. 공개망에 열지 마세요.\n'
printf '    · data_boundary: external 노드는 TLS 와 인증이 **필수**입니다(등록 시 강제).\n'
printf '    · 마스터 KEK 와 백업은 **서로 다른 곳**에 보관하세요.\n'

printf '\n'
if [ "$problems" -gt 0 ]; then
  printf '점검 실패 — 고장 %d건\n\n' "$problems" >&2
  exit 1
fi
if [ "$warnings" -gt 0 ]; then
  printf '점검 통과 — 확인이 필요한 항목 %d건\n\n' "$warnings"
else
  printf '점검 통과\n\n'
fi
