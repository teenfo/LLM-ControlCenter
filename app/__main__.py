"""`python -m app` 진입점.

도커 없는 실행 경로다 — 의존성이 5개뿐이라 현실적이고, 데모 노트북에서
Docker Desktop 이 VM 으로 2GB 를 먼저 먹는 것보다 훨씬 가볍다.
"""

import sys

from .cli import main

if __name__ == "__main__":
    sys.exit(main())
