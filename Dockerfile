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
COPY pyproject.toml README.md ./
COPY app/__init__.py app/__init__.py
RUN pip install --no-cache-dir . && rm -rf app

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
