"""최초 기동 — 키 · 관리자 · 첫 테넌트 · 도입 첫날의 가드 등급.

**기본 자격증명이 존재하지 않는다.** 제품에 `admin/admin` 이 있으면 설치처의 절반은
그것을 안 바꾸고, 그 절반이 곧 사고다. 여기서 만드는 값은 전부 무작위이고 **한 번만
표시된다** — 다시 볼 수 없으므로 그때 보관해야 한다.

그리고 **도입 첫날 막히지 않게 시작한다.** 베이스라인 가드 규칙을 처음부터 `block`
으로 켜면 도입 첫날 프로덕션이 서고, 그러면 설치처는 규칙을 통째로 꺼버린다 —
안 켜진 필터는 없는 필터다. 그래서 유예 모드로 시작하고, **그 사실을 화면과 API 가
계속 시끄럽게 알린다.** 유예를 조용히 두면 그게 더 나쁘다.
"""

from __future__ import annotations

import base64
import os
import secrets
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Mapping

from .auth import ROLE_PLATFORM_ADMIN, ROLE_SERVICE, ROLE_TENANT_ADMIN, issue_token
from .crypto import ENV_MASTER_KEY, KeyVault, generate_master_key
from .identity import new_salt
from .store import SqliteStore, TenantScope

#: 유예 모드 플래그. 켜져 있으면 가드가 `block` 을 `audit` 로 낮춘다.
#: 플랫폼 설정이므로 테넌트가 켜거나 끌 수 없다.
GRACE_KEY = "guard_grace_mode"

#: 플랫폼 설정은 예약된 테넌트 id 아래에 둔다. 스토어의 테넌트 초크포인트를
#: 우회하는 별도 테이블을 만들지 않기 위해서다 — 우회로가 생기면 언젠가 쓰인다.
PLATFORM_TENANT = "_platform"

BOOTSTRAP_MARK = "bootstrapped_at"


@dataclass
class BootstrapResult:
    """**한 번만 표시되는 값들.** 로그에 남기지 않는다."""

    master_key: str | None = None
    master_key_path: Path | None = None
    platform_admin_token: str | None = None
    tenant_admin_token: str | None = None
    service_token: str | None = None
    tenant_id: str = ""
    already_done: bool = False
    warnings: list[str] = field(default_factory=list)

    def banner(self) -> str:
        """콘솔에 한 번 찍을 안내. 이 문자열이 사라지면 값도 사라진다."""
        if self.already_done:
            return "이미 부트스트랩된 설치입니다. 새 자격증명을 발급하지 않았습니다."

        lines = [
            "",
            "=" * 72,
            "  LLM ControlCenter — 최초 기동",
            "=" * 72,
            "",
            "  아래 값은 **지금 한 번만** 표시됩니다. 다시 볼 수 없습니다.",
            "",
        ]
        if self.master_key:
            lines += [
                f"  마스터 KEK        {self.master_key}",
                f"    저장 위치       {self.master_key_path}",
                "    ! 백업과 **다른 곳**에 보관하세요. 같은 곳에 두면 백업 유출이",
                "      곧 원문 유출입니다. 이 키를 잃으면 기존 암호문은 영구히",
                "      열 수 없습니다.",
                "",
            ]
        else:
            lines += [
                "  마스터 KEK        (없음) — 원문을 보관하지 않습니다.",
                f"    {ENV_MASTER_KEY} 를 설정하면 원문 암호화 보관이 켜집니다.",
                "",
            ]
        lines += [
            f"  플랫폼 관리자 토큰  {self.platform_admin_token}",
            f"  첫 테넌트          {self.tenant_id}",
            f"    테넌트 관리자     {self.tenant_admin_token}",
            f"    서비스 토큰       {self.service_token}",
            "",
        ]
        for warning in self.warnings:
            lines.append(f"  ! {warning}")
        if self.warnings:
            lines.append("")
        lines += ["=" * 72, ""]
        return "\n".join(lines)


def is_bootstrapped(store: SqliteStore) -> bool:
    return store.platform_setting(BOOTSTRAP_MARK) is not None


class KeyDirectoryUnwritable(RuntimeError):
    """키 디렉터리에 쓸 수 없다. **사람이 읽고 조치할 수 있는 메시지를 나른다.**"""


def _keys_dir_complaint(directory: Path) -> str:
    """왜 못 쓰는지와 무엇을 하면 되는지. 스택트레이스로는 아무도 못 고친다."""
    owner = ""
    try:
        stat = directory.stat()
        owner = f" (현재 소유 uid={stat.st_uid}, 권한 {stat.st_mode & 0o777:03o})"
    except OSError:
        pass
    return (
        f"키 디렉터리에 쓸 수 없습니다: {directory}{owner}\n"
        "  컨테이너는 uid 10001 로 돕니다. 도커가 없는 경로를 대신 만들면 root 소유가\n"
        "  되어 이 프로세스가 마스터 KEK 를 쓰지 못합니다.\n"
        "  호스트에서 다음을 실행한 뒤 다시 기동하세요:\n"
        f"    mkdir -p {directory} && sudo chown -R 10001:10001 {directory}\n"
        "  키를 파일로 두지 않을 거라면 LCC_PROMPT_KEY 환경 변수로 넣으세요."
    )


def ensure_master_key(
    keys_dir: Path | str | None,
    *,
    env: Mapping[str, str] | None = None,
) -> tuple[str | None, Path | None]:
    """마스터 KEK 를 찾거나 만든다.

    환경 변수가 우선이다 — 시크릿 매니저를 쓰는 설치처가 파일을 안 만들 수 있어야 한다.
    디렉터리를 안 주면 만들지 않는다: **키 없이 도는 것은 유효한 구성**이고
    (마스킹본만 저장), 아무 데나 키를 흘려 놓는 것보다 낫다.

    **못 쓰는 디렉터리는 여기서 사람 말로 끝낸다.** 컴포즈가 `./keys` 를 대신 만들면
    root 소유가 되고, uid 10001 로 도는 컨테이너는 거기 쓰지 못한다. 그대로 두면
    `PermissionError` 트레이스백을 남기고 죽는데, `restart: unless-stopped` 가
    그것을 **크래시 루프**로 만든다 — 설치처는 로그가 계속 흐르는 화면만 본다.

    키를 만들기 **전에** 검사한다. 만들고 나서 저장에 실패하면 그 키는 어디에도
    없이 사라지고, 다음 기동이 다른 키를 만든다.
    """
    env = env if env is not None else os.environ
    if env.get(ENV_MASTER_KEY):
        return None, None       # 이미 있다. 새로 표시할 값이 없다.
    if keys_dir is None:
        return None, None

    directory = Path(keys_dir)
    path = directory / "master.key"
    if path.exists():
        return None, path

    try:
        directory.mkdir(parents=True, exist_ok=True)
    except OSError as exc:
        raise KeyDirectoryUnwritable(_keys_dir_complaint(directory)) from exc
    if not os.access(directory, os.W_OK | os.X_OK):
        raise KeyDirectoryUnwritable(_keys_dir_complaint(directory))

    key = generate_master_key()
    try:
        write_key_file(path, key)
    except OSError as exc:
        path.unlink(missing_ok=True)   # 반쯤 쓰인 키 파일을 남기지 않는다
        raise KeyDirectoryUnwritable(_keys_dir_complaint(directory)) from exc
    return key, path


def write_key_file(path: Path, key: str) -> None:
    """마스터 KEK 를 파일로. **디스크에 닿은 것을 확인하고 돌아온다.**

    `write_text` 만으로는 부족하다. 그것은 파이썬 버퍼를 OS 로 넘길 뿐이고 값은
    페이지 캐시에 남는다 — 프로세스가 죽어도 살아남지만 **정전·커널 패닉에는
    사라진다.** 그 사이 DB 는 이 키로 감싼 DEK 를 들고 있을 수 있고, 그러면 남는 것은
    **어떤 키로도 못 여는 DB** 다.

    이것이 특히 회전에서 중요하다. `keyrotation` 의 안전 논거 전체가 "새 키가 DB
    커밋보다 **먼저** 디스크에 있다" 인데, fsync 가 없으면 그 순서를 OS 가 지켜 줄
    이유가 없다. 디렉터리까지 fsync 하는 이유는 파일 내용과 **이름**이 따로 놀기
    때문이다 — 내용만 동기화하면 크래시 뒤 이름이 없을 수 있다.
    """
    with open(path, "w", encoding="utf-8") as handle:
        handle.write(key + "\n")
        handle.flush()
        os.fsync(handle.fileno())
    # 같은 호스트의 다른 사용자가 읽지 못하게. 컨테이너에서도 의미가 있다.
    os.chmod(path, 0o600)
    fsync_directory(path.parent)


def fsync_directory(directory: Path) -> None:
    """디렉터리 엔트리를 디스크에. 이름 변경·생성이 살아남게 한다."""
    fd = os.open(str(directory), os.O_RDONLY)
    try:
        os.fsync(fd)
    finally:
        os.close(fd)


def load_master_key_from(keys_dir: Path | str | None, env: Mapping[str, str] | None = None) -> bytes | None:
    env = env if env is not None else os.environ
    raw = env.get(ENV_MASTER_KEY)
    if not raw and keys_dir:
        path = Path(keys_dir) / "master.key"
        if path.exists():
            raw = path.read_text(encoding="utf-8").strip()
    if not raw:
        return None
    return base64.b64decode(raw, validate=True)


def bootstrap(
    store: SqliteStore,
    vault: KeyVault,
    *,
    tenant_id: str = "default",
    tenant_name: str = "Default",
    locale: str = "ko-KR",
    master_key: str | None = None,
    master_key_path: Path | None = None,
    grace_mode: bool = True,
) -> BootstrapResult:
    """최초 기동. 이미 되어 있으면 아무것도 하지 않는다.

    **재실행이 안전해야 한다** — 컨테이너가 재시작할 때마다 새 관리자 토큰이
    발급되면 이전 토큰이 어디에 쓰이는지 아무도 모르게 된다.
    """
    if is_bootstrapped(store):
        return BootstrapResult(already_done=True)

    result = BootstrapResult(
        master_key=master_key, master_key_path=master_key_path, tenant_id=tenant_id
    )

    if not vault.enabled:
        result.warnings.append(
            "마스터 KEK 가 없어 원문을 보관하지 않습니다. 마스킹본만 저장됩니다."
        )

    # 플랫폼 관리자. 플랫폼 자체도 테넌트 행이 있어야 토큰을 걸 수 있다.
    store.create_tenant(
        PLATFORM_TENANT, "Platform", locale=locale, end_user_salt=new_salt()
    )
    store.create_service(
        TenantScope(PLATFORM_TENANT), "console", "console", allow_roles=[]
    )
    _, result.platform_admin_token = issue_token(
        store, TenantScope(PLATFORM_TENANT), "console",
        role=ROLE_PLATFORM_ADMIN, note="bootstrap", actor="bootstrap",
    )

    # 첫 테넌트. 로케일이 곧 가드 로케일 팩을 정한다.
    store.create_tenant(
        tenant_id, tenant_name, locale=locale,
        end_user_salt=new_salt(), dek_wrapped=vault.create_dek(),
    )
    scope = TenantScope(tenant_id)
    store.create_service(scope, f"{tenant_id}-app", f"{tenant_id}-app")
    _, result.tenant_admin_token = issue_token(
        store, scope, f"{tenant_id}-app", role=ROLE_TENANT_ADMIN,
        note="bootstrap", actor="bootstrap",
    )
    _, result.service_token = issue_token(
        store, scope, f"{tenant_id}-app", role=ROLE_SERVICE,
        note="bootstrap", actor="bootstrap",
    )

    if grace_mode:
        store.set_platform_setting(GRACE_KEY, True)
        result.warnings.append(
            "가드 유예 모드로 시작합니다 — 차단 규칙이 audit 로 낮춰집니다. "
            "오탐률을 확인한 뒤 관제 UI 에서 해제하세요."
        )

    store.set_platform_setting(BOOTSTRAP_MARK, store.schema_version)
    store.audit(
        "bootstrap", "bootstrap", tenant_id=tenant_id,
        detail={
            "raw_prompt_storage": vault.enabled,
            "grace_mode": grace_mode,
            "locale": locale,
        },
    )
    return result


def generate_admin_password() -> str:
    """관리자용 무작위 비밀. 부트스트랩 외에는 쓰지 않는다."""
    return secrets.token_urlsafe(24)


def demo_seed(store: SqliteStore, vault: KeyVault, *, config: Any) -> dict[str, Any]:
    """데모 시드 — 테넌트 2개 · 서비스 · 토큰.

    **격리를 보여주려면 테넌트가 둘이어야 한다.** 하나뿐인 데모에서는 "다른 조직의
    데이터가 안 보인다" 를 시연할 수가 없고, 그게 이 제품의 최대 리스크에 대한
    유일한 대답이다.
    """
    handles: dict[str, Any] = {"tenants": {}}
    for tenant_id, name, locale in (("acme", "Acme", "ko-KR"), ("globex", "Globex", "en-US")):
        fresh = store.get_tenant(tenant_id) is None
        if fresh:
            store.create_tenant(
                tenant_id, name, locale=locale,
                end_user_salt=new_salt(), dek_wrapped=vault.create_dek(),
                budget_usd_per_month=25.0, rate_limit_per_min=120,
            )
        scope = TenantScope(tenant_id)
        if fresh:
            store.create_service(scope, f"{tenant_id}-web", f"{tenant_id}-web")
        # **토큰은 매번 새로 발급한다.** 토큰은 발급 시 한 번만 보이므로, 데모를
        # 다시 띄웠을 때 아무것도 안 찍으면 시연자가 자기 데모에 못 들어간다.
        # 데모 프로파일 한정이다 — 일반 부트스트랩은 재실행해도 새 토큰을 안 만든다.
        _, admin = issue_token(
            store, scope, f"{tenant_id}-web", role=ROLE_TENANT_ADMIN,
            note="demo seed", actor="demo",
        )
        _, service = issue_token(
            store, scope, f"{tenant_id}-web", role=ROLE_SERVICE,
            note="demo seed", actor="demo",
        )
        handles["tenants"][tenant_id] = {"tenant_admin": admin, "service": service}
    return handles


#: 데모에서 가드 1단을 보여줄 샘플. **전부 합성값이다** — 체크섬은 통과하지만
#: 실제로 발급된 적 없는 번호이며, 실존 인물의 정보를 데모에 넣지 않기 위해서다.
DEMO_PII_SAMPLES: tuple[tuple[str, str], ...] = (
    ("credit_card", "결제 카드 4111 1111 1111 1111 로 처리해 주세요"),
    ("email", "회신은 hong@example.com 으로 부탁드립니다"),
    ("kr_rrn", "주민등록번호 990101-1234563 확인 바랍니다"),
    ("ipv4", "서버 192.168.10.24 에서 오류가 납니다"),
    ("clean", "지난 분기 매출 추이를 세 줄로 요약해 주세요"),
)
