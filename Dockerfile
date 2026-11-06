# LLM ControlCenter.
#
# 의존성이 이미지에 갇히는 것이 이 배포 형태의 요지다 — 파이썬 버전도,
# cryptography 의 네이티브 휠도, DB 도 전부 여기 안에 있어서 설치처의 환경이
# 무엇이든 같은 것이 돈다. **지원 비용이 가장 낮은 형태다.**

FROM python:3.12-slim AS base

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PIP_NO_CACHE_DIR=1 \
    LCC_DATA_DIR=/data \
    LCC_KEYS_DIR=/keys

WORKDIR /app

# 의존성만 먼저 넣는다. 앱 코드가 바뀌어도 이 레이어는 다시 안 받는다.
#
# **`pip install .` 을 쓰지 않는다.** pyproject 가 패키지 디렉터리를 이름으로
# 나열하므로(`app.providers`·번들 자산) 소스가 없는 이 레이어에서는 메타데이터
# 생성부터 죽는다 — 실제로 `package directory 'app/providers' does not exist` 로
# 빌드가 실패했다(1차 배포 리허설). 휠 검증은 전체 트리에서 빌드하므로 이 실패를
# 못 봤다. 목록은 pyproject 한 곳에서 읽는다: 여기 다시 적으면 두 벌이 되고, 두 벌은
# 어긋난다. 앱은 WORKDIR 의 소스 트리로 돈다(`python -m app`) — 설치된 패키지가
# 필요 없다.
COPY pyproject.toml ./
RUN python -c "import tomllib; print('\n'.join(tomllib.load(open('pyproject.toml', 'rb'))['project']['dependencies']))" > /tmp/requirements.txt \
 && pip install --no-cache-dir -r /tmp/requirements.txt \
 && rm /tmp/requirements.txt

COPY app/ app/
COPY config/ config/
COPY locales/ locales/
COPY static/ static/
COPY clients/ clients/

# **루트로 돌지 않는다.** 마스터 KEK 와 프롬프트 암호문을 들고 있는 프로세스다.
RUN useradd --system --create-home --uid 10001 llmcc \
 && mkdir -p /data /keys \
 && chown -R llmcc:llmcc /app /data /keys
USER llmcc

VOLUME ["/data", "/keys"]
EXPOSE 8610

# 컨테이너 헬스체크는 DB 를 안 만지는 경로를 쓴다 — DB 가 느릴 때 헬스체크까지
# 느려지면 오케스트레이터가 멀쩡한 컨테이너를 죽인다.
HEALTHCHECK --interval=30s --timeout=5s --start-period=15s --retries=3 \
  CMD python -c "import urllib.request,sys; sys.exit(0 if urllib.request.urlopen('http://127.0.0.1:8610/healthz', timeout=3).status==200 else 1)"

ENTRYPOINT ["python", "-m", "app"]
CMD ["serve", "--host", "0.0.0.0", "--port", "8610"]
