"""번들 자산의 위치 — **저장소 배치와 설치본 배치가 다르다.**

저장소에서는 `config/`·`locales/`·`static/`·`clients/` 가 루트에 있고, 휠로
설치하면 `app/bundled_*/` 안에 들어간다(패키징이 그렇게 매핑한다 — 디렉터리를
실제로 옮기면 컴포즈의 `./config:/app/config:ro` 마운트와 문서가 전부 어긋난다).

한쪽만 보면 다른 쪽에서 깨진다. 실제로 `pip install .` 로 설치한 것은 **기동조차
못 했다** — `-e` 설치와 도커 WORKDIR 에서만 도는 패키징이었다.

별도 모듈인 이유는 `main` 과 `cli` 가 둘 다 이것을 필요로 하고, `main` → `cli`
임포트는 순환이 되기 때문이다.
"""

from __future__ import annotations

from pathlib import Path

PACKAGE = Path(__file__).resolve().parent
ROOT = PACKAGE.parent


def bundled(name: str) -> Path:
    """`config` · `locales` · `static` · `clients` 중 하나의 실제 위치."""
    repo = ROOT / name
    if repo.is_dir():
        return repo
    installed = PACKAGE / f"bundled_{name}"
    if installed.is_dir():
        return installed
    # 둘 다 없으면 저장소 경로로 보고한다 — 오류 메시지가 사람이 아는 경로여야 한다.
    return repo
