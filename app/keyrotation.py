"""마스터 KEK 회전 — **유출 대응은 절차가 코드여야 한다.**

설계 문서(`design-decisions.md` D4/G5)는 오래 이렇게만 적혀 있었다: *"re-wrap 은 DEK
래핑만 교체하면 되므로 암호문 재암호화 없이 가능한데 그 사실조차 어디에도 없었다 —
유출 의심 상황에서 그것을 그 자리에서 알아내는 것은 최악의 시점이다."* 이 모듈이
그 절차이고, `docs/runbook-key-compromise.md` 가 사람이 읽는 쪽이다.

### 무엇을 바꾸고 무엇을 안 바꾸는가

프롬프트·응답 암호문은 **테넌트 DEK** 로 봉인돼 있고, DEK 는 **마스터 KEK** 로 감싸여
`tenants.dek_wrapped` 에 산다. KEK 를 바꾼다는 것은 그 래핑을 다시 하는 것이지
암호문을 다시 만드는 것이 아니다. 저장된 원문이 몇 기가바이트든 회전은 테넌트 수만큼의
행 갱신으로 끝난다.

### 중단돼도 복구되는 순서

DB 커밋과 키 파일 교체 사이에는 **반드시 창이 있다.** 어느 쪽이든 한쪽만 반영된
상태에서 프로세스가 죽으면 시스템은 아무것도 못 연다. 없앨 수 없는 창이므로,
없애는 대신 **그 창에서 두 키가 모두 디스크에 있도록** 순서를 짠다:

1. 옛 KEK 가 **모든** 테넌트 DEK 를 여는지 먼저 확인한다. 못 여는 것이 하나라도
   있으면 아무것도 건드리지 않고 멈춘다 — 회전은 고장을 고치는 도구가 아니다.
2. 새 KEK 를 `master.key.new` 에 쓴다. 이 시점부터 두 키가 다 디스크에 있다.
3. 래핑을 전부 다시 만들어 **한 트랜잭션으로** 쓴다.
4. 옛 키를 `master.key.rotated-<ts>` 로 밀고, `master.key.new` 를 제자리로 옮긴다.
5. 새 키만으로 전 테넌트를 다시 열어 본다.

어디서 죽어도 복구할 수 있다. 그리고 남아 있는 `master.key.new` 자체가 **회전이
중단됐다는 증거**다 — `doctor` 가 그것을 보고 사람 말로 알려 준다.

### 옛 키를 지우지 않는다

3번이 끝난 순간 옛 KEK 는 아무것도 못 연다. 그래도 지우지 않고 밀어 두는 이유는,
유출 대응 중인 운영자가 **서비스가 실제로 도는 것을 확인하기 전까지** 되돌릴 길을
남겨야 하기 때문이다. 언제 지울지는 런북이 말한다.
"""

from __future__ import annotations

import os
import shutil
from dataclasses import dataclass
from pathlib import Path

from .bootstrap import fsync_directory, write_key_file
from .crypto import CryptoError, KeyVault, generate_master_key

#: 회전 중에만 존재하는 파일. 남아 있으면 회전이 중단된 것이다.
STAGED_NAME = "master.key.new"

#: 회전이 끝난 뒤 옛 키가 밀려나는 이름. 뒤에 타임스탬프가 붙는다.
RETIRED_PREFIX = "master.key.rotated-"

KEY_NAME = "master.key"


class RotationRefused(RuntimeError):
    """회전을 시작할 수 없다. **아무것도 안 바뀐 상태에서만 난다.**"""


@dataclass(frozen=True)
class RotationResult:
    tenants: int
    retired_key_path: Path | None
    new_key_path: Path | None
    #: 새 KEK 를 이 함수가 만들었다면 그 값. 운영자가 준 것이면 `None` —
    #: 남의 시크릿 매니저에서 온 값을 로그에 흘리지 않는다.
    generated_key: str | None = None


def staged_key_path(keys_dir: Path | str) -> Path:
    return Path(keys_dir) / STAGED_NAME


def interrupted(keys_dir: Path | str | None) -> Path | None:
    """중단된 회전이 남긴 파일. 없으면 `None`.

    `doctor` 가 이것을 본다. 중단된 회전을 조용히 두면 다음 기동이 옛 키로 뜨고,
    DB 는 새 키로 감싸여 있어서 **원문 열람이 전부 실패한다** — 그런데 그 증상은
    "복호화 실패" 로만 보여서 원인을 찾는 데 한참 걸린다.
    """
    if not keys_dir:
        return None
    path = staged_key_path(keys_dir)
    return path if path.exists() else None


def vault_from_file(path: Path | str) -> KeyVault | None:
    """키 파일 하나를 금고로. 못 읽거나 형식이 아니면 `None`.

    진단이 **중단된 회전의 어느 쪽이 사실인지**를 판정할 때 쓴다. 판정을 사람에게
    떠넘기면("DB 가 새 키로 감싸여 있다면…") 유출 대응 중에 그 판단을 하게 되고,
    틀리면 어떤 키로도 못 여는 상태가 된다.
    """
    try:
        return KeyVault(_decode(Path(path).read_text(encoding="utf-8")))
    except (OSError, RotationRefused, CryptoError):
        return None


def rotate_master_kek(
    store,
    *,
    keys_dir: Path | str,
    old_vault: KeyVault,
    new_key: str | None = None,
    actor: str = "cli",
) -> RotationResult:
    """마스터 KEK 를 회전한다. 위 독스트링의 순서 그대로다.

    `new_key` 를 주면 그것을 쓰고(시크릿 매니저에서 발급한 경우), 안 주면 만든다.
    """
    directory = Path(keys_dir)

    # ── 0. 시작할 수 있는 상태인가 ──────────────────────────────────────────
    if not old_vault.enabled:
        raise RotationRefused(
            "마스터 KEK 가 없습니다. 원문 보관이 꺼진 설치처에는 회전할 키가 없습니다."
        )
    if interrupted(directory):
        raise RotationRefused(
            f"중단된 회전이 남아 있습니다: {staged_key_path(directory)}\n"
            "  먼저 그것을 정리하세요 — 런북의 '중단된 회전' 절을 보세요.\n"
            "  (DB 가 새 키로 감싸여 있다면 그 파일을 master.key 로 옮기면 됩니다.)"
        )
    if not os.access(directory, os.W_OK | os.X_OK):
        raise RotationRefused(
            f"키 디렉터리에 쓸 수 없습니다: {directory}\n"
            "  회전은 새 키를 파일로 남겨야 합니다. 권한을 고친 뒤 다시 시도하세요."
        )

    # ── 1. 옛 키가 전부 여는가 ──────────────────────────────────────────────
    #
    # **회전은 고장을 고치는 도구가 아니다.** 이미 못 여는 테넌트가 있는데 회전하면
    # 그 테넌트는 영영 못 열게 되고, 운영자는 회전이 그것을 깨뜨렸다고 믿는다.
    wrapped = store.wrapped_deks()
    unopenable = sorted(t for t, w in wrapped.items() if not old_vault.can_open(w))
    if unopenable:
        raise RotationRefused(
            f"현재 KEK 로 열리지 않는 테넌트가 있습니다: {', '.join(unopenable)}\n"
            "  회전은 이것을 고치지 못하고 오히려 되돌릴 길을 없앱니다.\n"
            "  올바른 KEK 를 먼저 찾으세요."
        )

    # ── 2. 새 키를 먼저 디스크에 ────────────────────────────────────────────
    generated = None if new_key else generate_master_key()
    key_value = new_key or generated
    assert key_value is not None
    new_vault = KeyVault(_decode(key_value))
    if not new_vault.enabled:
        raise RotationRefused("새 마스터 KEK 가 유효하지 않습니다.")

    # **fsync 까지 하고 넘어간다.** 이 단계의 존재 이유가 "DB 커밋보다 먼저 디스크에
    # 있다" 인데, 페이지 캐시에만 있으면 그 순서를 OS 가 지켜 줄 이유가 없다.
    staged = staged_key_path(directory)
    write_key_file(staged, key_value)

    # ── 3. 래핑을 다시 만들어 한 트랜잭션으로 ───────────────────────────────
    #
    # **무대에 올린 키를 치우는 것은 DB 를 안 건드렸음이 확실할 때만 한다.**
    #
    # 치우는 이유는 `doctor` 가 있지도 않은 중단을 보고하지 않게 하려는 것뿐이고,
    # 그 편의를 위해 위험을 지면 안 된다 — 커밋이 됐는데 새 키를 지우면 **어떤 키로도
    # 못 여는 DB** 가 남는다. 그래서 순수 계산 구간만 감싼다. 저장소를 부르는
    # 순간부터는 무슨 일이 나든 두 키를 다 남기고, 어느 쪽이 사실인지는 `doctor` 가
    # 실제로 열어 보고 판정한다.
    try:
        rewrapped = {
            tenant_id: old_vault.rewrap(blob, new_vault)
            for tenant_id, blob in wrapped.items()
        }
    except BaseException:
        staged.unlink(missing_ok=True)
        raise

    count = store.replace_wrapped_deks(rewrapped, actor=actor)

    # ── 4. 파일 교체 ────────────────────────────────────────────────────────
    live = directory / KEY_NAME
    retired: Path | None = None
    if live.exists():
        retired = directory / f"{RETIRED_PREFIX}{int(store._now())}"
        shutil.move(str(live), str(retired))
        os.chmod(retired, 0o600)
    shutil.move(str(staged), str(live))
    os.chmod(live, 0o600)
    # 이름 변경도 디스크에 남겨야 한다. 안 그러면 크래시 뒤 `master.key` 가 아직
    # 옛 키이거나 아예 없을 수 있고, 둘 다 이 절차가 막으려던 상태다.
    fsync_directory(directory)

    # ── 5. 새 키만으로 다시 열어 본다 ───────────────────────────────────────
    #
    # 여기까지 왔으면 논리적으로는 열려야 한다. 그래도 확인하는 이유: 이 검증이
    # 실패하는 유일한 경우가 **파일이 의도한 내용이 아닌 것**이고, 그것을 지금
    # 못 잡으면 다음 기동에서 잡게 된다.
    verify = KeyVault(_decode(live.read_text(encoding="utf-8").strip()))
    broken = sorted(t for t, w in store.wrapped_deks().items() if not verify.can_open(w))
    if broken:
        raise CryptoError(
            f"회전 후 검증 실패 — 새 키로 열리지 않는 테넌트: {', '.join(broken)}\n"
            f"  옛 키가 {retired} 에 있습니다. 런북의 '되돌리기' 절을 보세요."
        )

    return RotationResult(
        tenants=count, retired_key_path=retired, new_key_path=live,
        generated_key=generated,
    )


def _decode(value: str) -> bytes:
    import base64

    try:
        return base64.b64decode(value.strip(), validate=True)
    except Exception as exc:
        raise RotationRefused("새 마스터 KEK 가 유효한 base64 가 아닙니다.") from exc
