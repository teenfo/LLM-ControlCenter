"""복원 — **파일 하나를 덮어쓰는 것이 아니다.**

`restore.sh` 가 쉘로 하던 일 중 **틀리면 조용히 데이터를 깨뜨리는 부분**을 여기로
옮겼다. 쉘에 두면 테스트가 닿지 않고, 닿지 않는 코드는 검증되지 않는다.

세 가지가 각각 사고를 낸다:

1. **낡은 `-wal`·`-shm` 을 안 지운다.** WAL 모드 DB 는 파일 세 개가 한 벌이다.
   본체만 갈아 끼우면 SQLite 가 **이전 DB 의 WAL 을 새 본체 위에 얹는다.**
   백업이 원본에서 온 것이라 헤더가 맞아떨어질 수 있어서, 운이 나쁘면 깨진 것을
   깨진 줄 모르고 쓰게 된다. SQLite 문서가 본체 교체 시 세 파일을 함께 다루라고
   못박는 이유다.

2. **스키마 버전을 찍기만 하고 비교하지 않는다.** 신버전에서 뜬 백업을 구버전
   제품에 넣으면 없는 컬럼을 읽는다. 화면에 숫자 두 개를 나란히 찍어 놓고 사람이
   비교하기를 기대하는 것은 검사가 아니다 — 복원은 사고 난 뒤에 하는 일이라
   그때 사람은 가장 급하다.

3. **설정을 안 되돌린다.** 백업은 `config/` 를 담는데 복원은 DB 만 넣었다.
   역할·노드·가드 베이스라인이 백업 시점과 어긋난 채로 뜬다.

되돌릴 자리도 남긴다 — 복원이 잘못됐을 때 **복원 이전으로 돌아갈 방법**이 없으면
그것대로 막다른 길이다. 기존 DB 는 온라인 백업 API 로 일관된 사본을 뜬다
(`cp` 로 뜨면 §backup.py 가 적은 그 문제가 여기서 재현된다).
"""

from __future__ import annotations

import shutil
import sqlite3
import sys
from pathlib import Path

from .store import SCHEMA_VERSION

#: WAL 모드 DB 의 곁다리 파일. 본체와 **한 벌**이다.
SIDECARS = ("-wal", "-shm")

DB_NAME = "controlcenter.db"


class RestoreRefused(RuntimeError):
    """복원을 진행하지 않는다. 메시지는 사람이 읽고 조치할 수 있어야 한다."""


def read_schema_version(db: str | Path) -> int:
    conn = sqlite3.connect(f"file:{db}?mode=ro", uri=True)
    try:
        row = conn.execute(
            "SELECT value FROM meta WHERE key='schema_version'"
        ).fetchone()
        return int(row[0]) if row else 0
    finally:
        conn.close()


def check_compatible(backup_version: int, current_version: int = SCHEMA_VERSION) -> None:
    """호환되지 않으면 **막는다.** 경고만 찍고 진행하는 것은 검사가 아니다.

    구버전 백업 → 신버전 제품은 통과시킨다. 마이그레이션이 ADD COLUMN 전용이라
    빠진 컬럼은 기본값으로 채워진다(전진 호환).

    반대는 막는다. 신버전에서 뜬 백업에는 구버전이 모르는 컬럼이 있고, 구버전
    코드는 그것을 읽지 않으므로 **조용히 값이 사라진다.**
    """
    if backup_version > current_version:
        raise RestoreRefused(
            f"백업의 스키마 버전({backup_version})이 이 제품({current_version})보다 새롭습니다.\n"
            "  구버전 제품으로 신버전 백업을 복원하면 모르는 컬럼이 조용히 사라집니다.\n"
            f"  제품을 스키마 {backup_version} 이상으로 올린 뒤 다시 시도하세요."
        )


def summarize(db: str | Path) -> dict[str, int]:
    """복원 전에 사람에게 보여줄 요약."""
    conn = sqlite3.connect(f"file:{db}?mode=ro", uri=True)
    try:
        def count(sql: str) -> int:
            return conn.execute(sql).fetchone()[0]

        return {
            "schema_version": read_schema_version(db),
            "tenants": count("SELECT COUNT(*) FROM tenants WHERE purged_at IS NULL"),
            "jobs": count("SELECT COUNT(*) FROM jobs"),
            "role_overrides": count("SELECT COUNT(*) FROM role_overrides"),
            "prompt_cipher": count(
                "SELECT COUNT(*) FROM jobs WHERE prompt_cipher IS NOT NULL"
            ),
        }
    finally:
        conn.close()


def _consistent_copy(source: Path, target: Path) -> None:
    """살아 있는 WAL DB 의 일관된 사본. `cp` 로는 `-wal` 내용이 빠진다."""
    source_conn = sqlite3.connect(f"file:{source}?mode=ro", uri=True)
    target.unlink(missing_ok=True)
    target_conn = sqlite3.connect(target)
    try:
        source_conn.backup(target_conn)
    finally:
        source_conn.close()
        target_conn.close()


def install(source_db: str | Path, data_dir: str | Path) -> dict[str, object]:
    """백업 DB 를 데이터 디렉터리에 설치한다.

    순서가 중요하다. ① 기존 DB 의 되돌림 사본 → ② 곁다리 파일 제거 → ③ 본체 교체.
    ②를 빠뜨리는 것이 이 함수가 존재하는 이유이고, ①을 ②보다 뒤로 미루면
    되돌림 사본이 이미 깨진 상태에서 만들어진다.
    """
    source = Path(source_db)
    if not source.is_file():
        raise RestoreRefused(f"백업 DB 가 없습니다: {source}")

    check_compatible(read_schema_version(source))

    directory = Path(data_dir)
    directory.mkdir(parents=True, exist_ok=True)
    target = directory / DB_NAME

    rollback: Path | None = None
    if target.is_file():
        rollback = directory / f"{DB_NAME}.before-restore"
        _consistent_copy(target, rollback)

    # **곁다리를 지운다.** 남겨 두면 이전 DB 의 WAL 이 새 본체 위에 얹힌다.
    removed = []
    for suffix in SIDECARS:
        sidecar = directory / f"{DB_NAME}{suffix}"
        if sidecar.exists():
            sidecar.unlink()
            removed.append(sidecar.name)

    target.unlink(missing_ok=True)
    shutil.copyfile(source, target)

    return {
        "target": str(target),
        "rollback": str(rollback) if rollback else None,
        "sidecars_removed": removed,
    }


def install_config(source_dir: str | Path, target_dir: str | Path) -> list[str]:
    """백업에 담긴 `config/` 를 되돌린다.

    백업은 DB 와 설정을 함께 담는데 복원이 DB 만 넣으면, 역할·노드·가드 베이스라인이
    백업 시점과 어긋난 채로 뜬다. **반쪽만 되돌아간 상태가 가장 나쁘다** — 어느
    시점의 구성인지 아무도 말할 수 없다.
    """
    source = Path(source_dir)
    if not source.is_dir():
        return []

    target = Path(target_dir)
    target.mkdir(parents=True, exist_ok=True)
    restored = []
    for item in sorted(source.iterdir()):
        if not item.is_file():
            continue
        existing = target / item.name
        if existing.is_file():
            shutil.copyfile(existing, target / f"{item.name}.before-restore")
        shutil.copyfile(item, existing)
        restored.append(item.name)
    return restored


def main(argv: list[str] | None = None) -> int:
    args = list(sys.argv[1:] if argv is None else argv)
    if not args or args[0] in ("-h", "--help"):
        print(
            "사용법:\n"
            "  python -m app.restore inspect <백업.db>\n"
            "  python -m app.restore install <백업.db> <데이터디렉터리> [설정원본 설정대상]\n"
            "  python -m app.restore config  <설정원본> <설정대상>",
            file=sys.stderr,
        )
        return 2

    command, rest = args[0], args[1:]
    try:
        if command == "inspect":
            info = summarize(rest[0])
            check_compatible(int(info["schema_version"]))
            print(f"  · 스키마 버전 {info['schema_version']} (제품 {SCHEMA_VERSION})")
            print(f"  · 테넌트 {info['tenants']} · 작업 {info['jobs']}")
            print(
                f"  · 암호문 {info['prompt_cipher']}건"
                + ("" if info["prompt_cipher"] else " (백업에서 제외됨)")
            )
            if info["role_overrides"]:
                print()
                print(
                    f"  ! 역할 오버라이드 {info['role_overrides']}건이 이 백업 시점 값으로"
                    " **되돌아갑니다.**"
                )
                print("    오버라이드는 코드가 아니라 데이터이므로, 그 사이에 바꾼 모델 선택이")
                print("    조용히 되살아납니다. 복원 후 관제 UI 에서 확인하세요.")
            return 0

        if command == "install":
            result = install(rest[0], rest[1])
            if result["sidecars_removed"]:
                print(f"  · 낡은 WAL 파일 제거: {', '.join(result['sidecars_removed'])}")
            if result["rollback"]:
                print(f"  · 복원 이전 DB: {result['rollback']}")
            print(f"  · 복원: {result['target']}")
            if len(rest) >= 4:
                names = install_config(rest[2], rest[3])
                if names:
                    print(f"  · 설정 {len(names)}개 되돌림: {', '.join(names)}")
            return 0

        if command == "config":
            names = install_config(rest[0], rest[1])
            print(f"  · 설정 {len(names)}개 되돌림: {', '.join(names) or '없음'}")
            return 0
    except RestoreRefused as exc:
        print(f"\n복원을 중단했습니다.\n  {exc}\n", file=sys.stderr)
        return 1
    except Exception as exc:  # pragma: no cover - 예기치 못한 실패도 사람 말로
        print(f"복원 실패: {exc}", file=sys.stderr)
        return 1

    print(f"알 수 없는 명령: {command}", file=sys.stderr)
    return 2


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
