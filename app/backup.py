"""백업 스냅샷 — **`cp` 로 뜨지 않는다.**

살아 있는 WAL 데이터베이스를 파일 복사로 뜨면 `-wal` 에 있는 내용이 통째로
빠진다. 조용히 **빈 백업**이 만들어지고, 그 사실은 복원할 때에야 드러난다.
"백업이 없는 것" 보다 "백업이 있다고 믿는 것" 이 나쁘다.

그래서 SQLite 의 온라인 백업 API 를 쓴다. 표준 라이브러리에 있으므로 `sqlite3`
CLI 도 필요 없고, 컨테이너 이미지가 얇아진다.

    python -m app.backup /data/controlcenter.db /tmp/backup.db

`prompt_cipher` 는 스냅샷을 뜬 **뒤에** 지운다 — 원본을 건드리지 않기 위해서다.
그리고 **결과를 검증한다**: 테이블이 있고 행 수가 말이 되는지 확인하고, 아니면
0 이 아닌 코드로 끝낸다.
"""

from __future__ import annotations

import sqlite3
import sys
from contextlib import suppress
from pathlib import Path

#: 이 테이블들이 비어 있으면 스냅샷이 잘못됐다고 본다. 정상 설치라면 최소한
#: 테넌트 하나와 스키마 버전은 있다.
REQUIRED_TABLES = ("meta", "tenants", "jobs", "usage")


def snapshot(source: str | Path, target: str | Path) -> dict[str, int]:
    source_conn = sqlite3.connect(f"file:{source}?mode=ro", uri=True)
    target_path = Path(target)
    target_path.unlink(missing_ok=True)
    target_conn = sqlite3.connect(target_path)
    counts: dict[str, int] = {}
    try:
        source_conn.backup(target_conn)   # WAL 을 포함한 일관된 스냅샷

        names = {
            row[0]
            for row in target_conn.execute(
                "SELECT name FROM sqlite_master WHERE type='table'"
            )
        }
        missing = [t for t in REQUIRED_TABLES if t not in names]
        if missing:
            raise RuntimeError(f"백업에 테이블이 없습니다: {missing}")

        # 검증의 기준은 **원본**이다. 절대값을 못박으면 갓 설치한 시스템이 항상 실패한다.
        source_counts = {
            table: source_conn.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0]
            for table in ("tenants", "jobs")
        }

        # **암호문을 검증보다 먼저 지운다.** 검증 뒤에 지우면, 검증에 실패했을 때
        # 원문 암호문이 든 파일이 디스크에 남는다 — 백업에서 빼기로 한 바로 그것이
        # 실패 경로에서만 남는 셈이다.
        counts["prompt_cipher_removed"] = target_conn.execute(
            "UPDATE jobs SET prompt_cipher = NULL, prompt_nonce = NULL "
            "WHERE prompt_cipher IS NOT NULL"
        ).rowcount
        target_conn.commit()
        target_conn.execute("VACUUM")
        target_conn.commit()

        for table in ("tenants", "services", "jobs", "usage", "filter_events"):
            counts[table] = target_conn.execute(
                f"SELECT COUNT(*) FROM {table}"
            ).fetchone()[0]

        # **행 수가 말이 되는지 실제로 본다.**
        #
        # 테이블이 있는지만 보는 것은 이 모듈이 스스로 적은 계약("행 수가 말이
        # 되는지 확인하고, 아니면 0 이 아닌 코드로 끝낸다")의 절반이다. 빈
        # 스냅샷도 성공으로 나가면 `cp` 로 뜨던 시절과 같은 실패가 —
        # **백업이 있다고 믿는 것** — 그대로 돌아온다.
        #
        # 기준은 원본이다. 절대값을 못박으면 갓 설치한 시스템이 항상 실패한다.
        expected = source_counts["tenants"]
        if expected and counts["tenants"] < expected:
            raise RuntimeError(
                f"백업의 테넌트가 원본보다 적습니다 (원본 {expected} → 백업 {counts['tenants']})"
            )
        if source_counts["jobs"] and counts["jobs"] < source_counts["jobs"]:
            raise RuntimeError(
                f"백업의 작업이 원본보다 적습니다 "
                f"(원본 {source_counts['jobs']} → 백업 {counts['jobs']})"
            )
    except Exception:
        with suppress(Exception):
            target_conn.close()
        # 검증에 실패한 스냅샷은 남기지 않는다. 남기면 누군가 그것으로 복원한다.
        target_path.unlink(missing_ok=True)
        raise
    finally:
        with suppress(Exception):
            source_conn.close()
        with suppress(Exception):
            target_conn.close()
    return counts


def main(argv: list[str] | None = None) -> int:
    args = list(sys.argv[1:] if argv is None else argv)
    if len(args) != 2:
        print("사용법: python -m app.backup <원본.db> <대상.db>", file=sys.stderr)
        return 2
    try:
        counts = snapshot(args[0], args[1])
    except Exception as exc:
        print(f"백업 실패: {exc}", file=sys.stderr)
        return 1

    print(
        f"  · 스냅샷: 테넌트 {counts['tenants']} · 작업 {counts['jobs']} "
        f"· 사용량 {counts['usage']}"
    )
    print(f"  · 암호문 {counts['prompt_cipher_removed']}건을 백업에서 제외했습니다.")
    return 0


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
