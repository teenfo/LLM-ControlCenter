"""명령줄 진입점 — `serve` · `--demo` · `doctor` · `bootstrap`.

**도커 없는 실행 경로를 함께 제공한다.** 의존성이 5개뿐이라 현실적이고, 데모
노트북에서 Docker Desktop 이 VM 으로 2GB 를 먼저 먹는 것보다 훨씬 가볍다.

    pip install -e . && python -m app --demo

`--demo` 는 마지막에 붙이는 포장이 아니다. 목 프로바이더가 1단계부터 있어서 실제
노드 없이 테스트가 돌고 있고, **그게 그대로 데모가 된다** — GPU 없는 노트북 한 대로
클러스터 제품을 시연할 수 있다는 것이 영업·PoC 에서 큰 차이를 만든다.
"""

from __future__ import annotations

import argparse
import json
import os
import sys
from pathlib import Path
from typing import Any

from .bootstrap import (
    GRACE_KEY,
    bootstrap,
    demo_seed,
    KeyDirectoryUnwritable,
    ensure_master_key,
    is_bootstrapped,
    load_master_key_from,
)
from .cluster import Cluster
from .config import Config, ConfigError, load_config, validate_cross_references
from .cost import CostAccountant
from .crypto import CryptoError, KeyVault
from .evals import Evaluator
from .guard import Guard
from .keyrotation import (
    RotationRefused,
    interrupted as interrupted_rotation,
    latest_retired,
    rotate_master_kek,
    vault_from_file,
)
from .i18n import Translator
from .models import ModelRegistrar
from .notify import Notifier, channels_from_env
from .observability import configure_logging, diagnostic_bundle
from .pipeline import Pipeline
from .completion import CompletionSignal
from .scheduler import Scheduler
from .store import SqliteStore

from .cli_paths import ROOT, bundled  # noqa: E402  (경로 해석은 한 곳에)

#: 번들의 기본 설정. **데모 전용 설정 디렉터리를 따로 두지 않는다** — 시드 노드가
#: 이미 목 프로바이더이고, 실제 설치는 관제 UI 에서 노드를 등록해 덮어쓴다.
#: 설정을 두 벌 유지하면 둘이 갈리고, 갈린 쪽이 하필 데모에서만 도는 경로가 된다.
DEFAULT_CONFIG_DIR = bundled("config")
DEFAULT_DATA_DIR = Path(os.environ.get("LCC_DATA_DIR", ROOT / "data"))
DEFAULT_KEYS_DIR = Path(os.environ.get("LCC_KEYS_DIR", ROOT / "keys"))


class Assembly:
    """조립된 부품 한 벌. `build_app` 에 그대로 넘긴다."""

    def __init__(self, config: Config, store: SqliteStore, vault: KeyVault, *, airgap: bool):
        self.config = config
        self.store = store
        self.vault = vault
        self.airgap = airgap

        self.translator = Translator.from_dir(bundled("locales"))
        self.accountant = CostAccountant(config.pricing, store)
        self.notifier = Notifier(channels_from_env(), translator=self.translator)
        self.cluster = Cluster(
            config, store, accountant=self.accountant, notifier=self.notifier,
            airgap=airgap,
        )
        self.guard = Guard(config, grace_mode=bool(store.platform_setting(GRACE_KEY, False)))
        self.evaluator = Evaluator(config, store, self.guard)
        self.registrar = ModelRegistrar(
            config, self.cluster, store, notify=self.notifier.as_callable()
        )
        # **하나의 신호를 둘이 공유한다.** 각자 만들면 보내는 쪽과 받는 쪽이
        # 갈려서 신호가 아무 데도 안 닿고, 증상은 "대기가 늘 최대치" 뿐이라
        # 아무도 눈치채지 못한다.
        self.completion = CompletionSignal()
        self.pipeline = Pipeline(
            config, store, self.cluster, self.guard,
            vault=vault, accountant=self.accountant, evaluator=self.evaluator,
            completion=self.completion,
        )
        # **스케줄러는 싱글턴이다.** 워커마다 돌면 잡이 중복 배치된다 —
        # 성능이 필요해서 워커를 늘리는 순간 조용히 깨지는 구조를 만들지 않는다.
        self.scheduler = Scheduler(
            config, store, self.cluster, accountant=self.accountant,
            registrar=self.registrar, notifier=self.notifier,
            # 출력 축 — 스케줄러가 응답을 쓰는 유일한 지점이라 가드와 금고가 여기 온다.
            guard=self.guard, vault=vault, completion=self.completion,
            # 분류 모델 인증 배선(QA R-HIGH). 이것이 없으면 신규 설치에서 인증을
            # 수행할 제품 경로가 없다 — 라우팅은 전건 기본 모델, 가드 2단은 매
            # 요청 실패였고, 인증 시드가 테스트에만 있어 테스트는 전부 초록이었다.
            evaluator=self.evaluator, certifier_factory=self.pipeline.make_certifier,
        )

    def build(self, *, version: str, start_scheduler: bool):
        from .main import build_app

        return build_app(
            config=self.config, store=self.store, cluster=self.cluster, guard=self.guard,
            scheduler=self.scheduler, pipeline=self.pipeline, translator=self.translator,
            vault=self.vault, evaluator=self.evaluator, registrar=self.registrar,
            accountant=self.accountant, notifier=self.notifier, airgap=self.airgap,
            version=version, start_scheduler=start_scheduler,
        )


def _airgap_from_env(env: dict[str, str] | None = None) -> bool:
    env = env if env is not None else dict(os.environ)
    return (env.get("LCC_AIRGAP") or "").lower() in ("1", "true", "yes")


def assemble(
    *,
    config_dir: Path,
    data_dir: Path,
    keys_dir: Path | None,
    airgap: bool,
) -> tuple[Assembly, Any]:
    """설정을 읽고 부품을 만든다. 부트스트랩 결과를 함께 돌려준다."""
    config = load_config(config_dir)
    validate_cross_references(config)

    data_dir.mkdir(parents=True, exist_ok=True)
    store = SqliteStore(data_dir / "controlcenter.db")

    master_key, key_path = ensure_master_key(keys_dir)
    vault = KeyVault(load_master_key_from(keys_dir))

    result = bootstrap(store, vault, master_key=master_key, master_key_path=key_path)
    return Assembly(config, store, vault, airgap=airgap), result


# ── 명령 ────────────────────────────────────────────────────────────────────


def cmd_serve(args: argparse.Namespace) -> int:
    import uvicorn

    from . import main as main_module

    config_dir = Path(args.config or DEFAULT_CONFIG_DIR)
    keys_dir = None if args.no_keys else Path(args.keys or DEFAULT_KEYS_DIR)

    assembly, result = assemble(
        config_dir=config_dir,
        data_dir=Path(args.data or DEFAULT_DATA_DIR),
        keys_dir=keys_dir,
        airgap=args.airgap or _airgap_from_env(),
    )

    # 부트스트랩 값은 **stdout 한 번**이다. 구조화 로그로 보내면 수집기에 남는다.
    print(result.banner(), file=sys.stderr)

    if args.demo:
        handles = demo_seed(assembly.store, assembly.vault, config=assembly.config)
        print(_demo_banner(handles, args.host, args.port), file=sys.stderr)

    configure_logging(args.log_level)
    app = assembly.build(version=main_module.VERSION, start_scheduler=not args.no_scheduler)
    uvicorn.run(app, host=args.host, port=args.port, log_config=None)
    return 0


def cmd_bootstrap(args: argparse.Namespace) -> int:
    """부트스트랩만 하고 나간다. 컴포즈의 초기화 잡이 쓴다."""
    assembly, result = assemble(
        config_dir=Path(args.config or DEFAULT_CONFIG_DIR),
        data_dir=Path(args.data or DEFAULT_DATA_DIR),
        keys_dir=None if args.no_keys else Path(args.keys or DEFAULT_KEYS_DIR),
        airgap=args.airgap or _airgap_from_env(),
    )
    print(result.banner())
    assembly.store.close()
    return 0


def cmd_doctor(args: argparse.Namespace) -> int:
    """진단. **실패 사유를 사람 말로** 낸다 — 종료 코드만 주면 아무도 못 고친다.

    고장(`problems`)과 주의(`warnings`)를 나눈다. 갓 설치한 시스템은 유예 모드로
    시작하는데, 그걸 고장으로 세면 **설치 직후 doctor 가 항상 실패한다** — 그러면
    설치처는 doctor 의 종료 코드를 안 보게 되고, 진짜 고장도 같이 묻힌다.
    """
    problems: list[str] = []
    warnings: list[str] = []
    notes: list[str] = []

    config_dir = Path(args.config or DEFAULT_CONFIG_DIR)
    data_dir = Path(args.data or DEFAULT_DATA_DIR)
    keys_dir = Path(args.keys or DEFAULT_KEYS_DIR)

    try:
        config = load_config(config_dir)
        validate_cross_references(config)
        notes.append(
            f"설정 OK — 노드 {len(config.nodes)} · 역할 {len(config.roles)} "
            f"· 레인 {len(config.lanes)} · 가드 규칙 {len(config.guard_rules)}"
        )
    except ConfigError as exc:
        print(f"설정 오류: {exc}", file=sys.stderr)
        return 2

    db_path = data_dir / "controlcenter.db"
    if not db_path.exists():
        # **진단이 트레이스백으로 끝나면 진단이 아니다.** 여기서 사람 말로 멈춘다.
        for note in notes:
            print(f"  · {note}")
        print(
            f"  ! 아직 부트스트랩되지 않았습니다 (DB 없음: {db_path}). "
            "`bootstrap` 을 먼저 실행하세요.",
            file=sys.stderr,
        )
        return 1

    store = SqliteStore(db_path)
    notes.append(f"스키마 버전 {store.schema_version}")

    if not is_bootstrapped(store):
        problems.append("아직 부트스트랩되지 않았습니다. `bootstrap` 을 먼저 실행하세요.")

    vault = KeyVault(load_master_key_from(keys_dir))
    if vault.enabled:
        notes.append(f"마스터 KEK 있음 — 원문 암호화 보관이 켜져 있습니다 ({keys_dir})")
    else:
        notes.append("마스터 KEK 없음 — 마스킹본만 저장됩니다.")

    # **키가 있다는 것과 그 키가 맞는다는 것은 다르다.**
    #
    # 예전에는 위 한 줄이 전부였다. 키 파일을 잘못 바꾼 설치처는 사용자가 원문을
    # 열려는 순간에야 알게 되고, 그 증상은 "복호화 실패" 로만 보여서 원인을 찾는 데
    # 한참 걸린다. 진단은 그 순간보다 먼저 와야 한다.
    wrapped = store.wrapped_deks() if vault.enabled else {}
    unopenable = sorted(t for t, w in wrapped.items() if not vault.can_open(w))

    stale = interrupted_rotation(keys_dir)
    if stale:
        # **어느 쪽이 사실인지 사람에게 묻지 않는다 — 열어 보면 안다.**
        #
        # "DB 가 새 키로 감싸여 있다면 이렇게 하세요" 라고만 적으면, 그 판단을
        # 유출 대응 중인 운영자에게 떠넘기는 것이다. 틀리면 어떤 키로도 못 여는
        # 상태가 되므로 진단이 대신 판정한다.
        #
        # **`master.key` 가 없어도 판정한다.** 이름 바꾸기 두 번 사이에서 죽으면
        # `master.key` 가 없는 창이 생기는데, 예전 코드는 래핑 읽기가
        # `vault.enabled` 에 걸려 있어 그 창에서 래핑을 아예 안 읽었고, 그래서
        # 무조건 "회전 반영 안 됨 → rm" 가지로 갔다 — 그 rm 이 **DB 를 열 수 있는
        # 유일한 키**를 지운다(QA V1). 래핑은 금고 없이도 읽을 수 있다.
        wrapped_now = wrapped if vault.enabled else store.wrapped_deks()
        live_missing = not vault.enabled
        staged_vault = vault_from_file(stale)
        staged_opens = staged_vault is not None and all(
            staged_vault.can_open(w) for w in wrapped_now.values()
        )
        if staged_opens and (wrapped_now or live_missing):
            # 래핑이 없으면(빈 DB) 어느 키든 열므로, `master.key` 가 살아 있는 한
            # 아래 rm 가지가 안전하다 — 그때만 이 가지를 비워 둔다.
            problems.append(
                f"KEK 회전이 파일 교체 직전에 중단됐습니다: {stale}\n"
                "    DB 는 이미 **새 키**로 감싸여 있습니다. 그 파일을 제자리로 옮기세요:\n"
                f"      mv {stale} {keys_dir / 'master.key'}"
            )
        elif not live_missing:
            problems.append(
                f"중단된 KEK 회전의 잔여 파일이 있습니다: {stale}\n"
                "    DB 는 아직 **현재 키**로 감싸여 있습니다 — 회전은 반영되지 않았습니다.\n"
                f"      rm {stale}    # 그 뒤 다시 `rotate-kek`"
            )
        else:
            # `master.key` 도 없고 무대의 키로도 안 열린다. 물러난 키로 열어 본다 —
            # 여기서 무엇이든 지우라고 말하는 순간 진단이 데이터 손실의 공범이 된다.
            retired = latest_retired(keys_dir)
            retired_vault = vault_from_file(retired) if retired else None
            if retired_vault is not None and all(
                retired_vault.can_open(w) for w in wrapped_now.values()
            ):
                problems.append(
                    f"KEK 회전이 중단됐고 `master.key` 가 없습니다.\n"
                    f"    DB 는 **물러난 키**로 열립니다. 그 키를 제자리로 옮기세요:\n"
                    f"      mv {retired} {keys_dir / 'master.key'}\n"
                    f"    무대의 키({stale.name})는 판정이 끝날 때까지 두세요."
                )
            else:
                problems.append(
                    "KEK 회전이 중단됐고, 남아 있는 어느 키로도 DB 가 열리지 않습니다.\n"
                    "    **아무 파일도 지우지 마세요.** 키 디렉터리를 통째로 보존한 채\n"
                    "    백업의 키로 복구하세요: docs/runbook-key-compromise.md"
                )

    # **검증을 안 돌리면 끊긴 것을 아무도 모른다.**
    #
    # 해시 체인의 값어치는 전부 "언젠가 확인한다" 에 있다. 확인하는 자리가 없으면
    # 그냥 컬럼 두 개를 더 쓰는 것일 뿐이다.
    chain = store.verify_audit_chain()
    if not chain["ok"]:
        spot = chain["broken_at"]
        problems.append(
            f"감사 체인이 어긋납니다 — {chain['reason']}\n"
            f"    id={spot['id']} · {spot['actor']} · {spot['action']}\n"
            "    docs/runbook-audit-integrity.md 를 보세요."
        )
    elif chain["checked"]:
        note = f"감사 체인 OK — {chain['checked']}행"
        if chain["unchained"]:
            note += f" (체인 이전 {chain['unchained']}행은 검증 대상 아님)"
        notes.append(note)

    agrees = store.audit_export_still_agrees()
    if agrees is False:
        # **체인 검증만으로는 절대 못 잡는 사건이다.** 체인이 통째로 다시 계산되면
        # 내부 검증은 통과하고, 밖에 내보낸 팁만이 그 사실을 안다.
        problems.append(
            "마지막으로 내보낸 감사 팁이 지금 체인에 없습니다 — "
            "체인이 재계산됐을 수 있습니다.\n"
            "    내보낸 사본과 대조하세요: docs/runbook-audit-integrity.md"
        )
    elif agrees is None and chain["checked"]:
        warnings.append(
            "감사를 밖으로 내보낸 적이 없습니다. 체인은 조작을 **드러낼** 뿐 막지 못하고, "
            "재계산은 외부 사본과의 대조로만 걸립니다 — `audit-export` 를 정기 실행하세요."
        )

    if unopenable:
        problems.append(
            f"현재 마스터 KEK 로 열리지 않는 테넌트 {len(unopenable)}개: "
            f"{', '.join(unopenable[:5])}"
            f"{' …' if len(unopenable) > 5 else ''}\n"
            "    키를 바꿨다면 되돌리세요. 회전하려면 `rotate-kek` 를 쓰세요."
        )
    elif wrapped:
        notes.append(f"KEK 검증 OK — 테넌트 {len(wrapped)}개의 DEK 를 모두 풉니다")

    if store.platform_setting(GRACE_KEY, False):
        warnings.append(
            "가드 유예 모드가 켜져 있습니다 — 차단 규칙이 실제로 막지 않습니다. "
            "오탐률을 확인한 뒤 관제 UI 에서 해제하세요."
        )
    if not vault.enabled:
        warnings.append(
            "원문을 보관하지 않습니다. 필요하면 마스터 KEK 를 설정하세요."
        )

    assembly = Assembly(config, store, vault, airgap=_airgap_from_env())
    if args.probe:
        import asyncio

        results = asyncio.run(assembly.cluster.probe_all())
        for node, ok in results.items():
            # 노드가 안 붙는 것은 고장이다. 관제 센터만 살아 있고 추론이 안 되는
            # 상태를 "정상" 으로 보고하면 진단의 의미가 없다.
            (notes if ok else problems).append(
                f"노드 {node}: {'도달 가능' if ok else '도달 불가'}"
            )

    # 맥락 규칙이 있는데 분류기가 판정을 못 하면, 그 규칙들은 있으나 마나다.
    if any(rule.is_llm for rule in config.guard_rules):
        ready, reason = assembly.evaluator.classifier_ready("_guard_classify")
        if not ready:
            warnings.append(
                f"2단 분류가 판정하지 못합니다({reason}). 맥락 규칙이 "
                "on_classifier_error 정책으로만 처리됩니다."
            )

    # 라우팅을 켠 역할의 분류기도 같다 — 미인증이면 라우팅은 조용히 전건 기본
    # 모델로 간다(fail-to-default). 안전하지만, 켜 놓은 관리자는 그 사실을
    # 여기서라도 봐야 한다. 인증은 스케줄러가 기동 시 자동으로 시도한다.
    for name, role in config.roles.items():
        if role.routing is None:
            continue
        ready, reason = assembly.evaluator.classifier_ready(role.routing.classifier)
        if not ready:
            warnings.append(
                f"역할 {name} 의 라우팅 분류기가 준비되지 않았습니다({reason}) — "
                "판정 없이 전부 기본 모델로 갑니다. 스케줄러 기동 후 자동 인증을 기다리거나 "
                "`doctor` 를 다시 실행해 보세요."
            )

    single_homed = assembly.cluster.single_homed_roles()
    if single_homed:
        notes.append(f"단일 호밍 역할: {', '.join(sorted(single_homed))}")

    if args.bundle:
        bundle = diagnostic_bundle(
            store=store, cluster=assembly.cluster, config=config,
            scheduler=assembly.scheduler, registrar=assembly.registrar,
            notifier=assembly.notifier, vault=vault, env=dict(os.environ),
            version=args.version or "", airgap=assembly.airgap,
        )
        target = Path(args.bundle)
        target.write_text(json.dumps(bundle, ensure_ascii=False, indent=2), encoding="utf-8")
        notes.append(f"진단 번들: {target} (비밀은 마스킹됨)")

    for note in notes:
        print(f"  · {note}")
    for warning in warnings:
        print(f"  ~ {warning}")
    for problem in problems:
        print(f"  ! {problem}", file=sys.stderr)

    store.close()
    if problems:
        print(f"\n진단 실패 — 고장 {len(problems)}건", file=sys.stderr)
        return 1
    if warnings:
        print(f"\n진단 통과 — 확인이 필요한 항목 {len(warnings)}건")
    else:
        print("\n진단 통과")
    return 0


def cmd_rotate_kek(args: argparse.Namespace) -> int:
    """마스터 KEK 회전. **암호문은 재암호화하지 않는다** — 래핑만 바꾼다.

    절차와 각 단계의 근거는 `app/keyrotation.py` 와
    `docs/runbook-key-compromise.md` 에 있다. 여기서는 사람과 말을 주고받는 몫만 한다.
    """
    data_dir = Path(args.data or DEFAULT_DATA_DIR)
    keys_dir = Path(args.keys or DEFAULT_KEYS_DIR)
    db_path = data_dir / "controlcenter.db"

    if not db_path.exists():
        print(f"DB 가 없습니다: {db_path}", file=sys.stderr)
        return 2

    store = SqliteStore(db_path)
    try:
        vault = KeyVault(load_master_key_from(keys_dir))
        tenants = len(store.wrapped_deks()) if vault.enabled else 0

        if not args.yes:
            print(f"테넌트 {tenants}개의 DEK 래핑을 새 마스터 KEK 로 교체합니다.")
            print(f"  키 디렉터리  {keys_dir}")
            print("  저장된 프롬프트·응답 암호문은 재암호화하지 않습니다.")
            print("  옛 키는 지우지 않고 밀어 둡니다 — 서비스 확인 뒤 직접 파기하세요.")
            if input("진행할까요? [y/N] ").strip().lower() not in ("y", "yes"):
                print("취소했습니다. 아무것도 바뀌지 않았습니다.")
                return 1

        new_key = os.environ.get(args.new_key_env) or None
        result = rotate_master_kek(
            store, keys_dir=keys_dir, old_vault=vault, new_key=new_key,
            actor=f"cli:{os.environ.get('USER', 'unknown')}",
        )
    except (RotationRefused, CryptoError) as exc:
        print(f"회전 중단: {exc}", file=sys.stderr)
        return 2
    finally:
        store.close()

    print(f"\n  회전 완료 — 테넌트 {result.tenants}개")
    print(f"  새 키       {result.new_key_path}")
    if result.retired_key_path:
        print(f"  옛 키       {result.retired_key_path}  ← 확인 뒤 파기하세요")
    if result.generated_key:
        # **여기서만 표시한다.** 부트스트랩이 마스터 KEK 를 1회만 표시하는 것과 같은
        # 규칙이다. 운영자가 준 키(환경 변수)는 절대 되찍지 않는다 — 남의 시크릿
        # 매니저에서 온 값을 이쪽 로그에 흘리는 것이 되기 때문이다.
        print(f"\n  새 마스터 KEK  {result.generated_key}")
        print("  이 값은 여기서만 표시됩니다. 백업과 **다른 곳에** 보관하세요.")
    print("\n  다음: 워커를 새 키로 재기동한 뒤 `doctor` 로 확인하세요.")
    return 0


def cmd_audit_export(args: argparse.Namespace) -> int:
    """감사를 밖으로 내보낸다. **이것이 체인을 의미 있게 만드는 절반이다.**

    체인만으로는 조작을 드러낼 뿐이고, DB 에 쓸 수 있는 공격자는 체인을 통째로 다시
    계산할 수 있다. 그 재계산은 **밖에 있는 사본**과 대조할 때만 걸린다.
    """
    data_dir = Path(args.data or DEFAULT_DATA_DIR)
    db_path = data_dir / "controlcenter.db"
    if not db_path.exists():
        print(f"DB 가 없습니다: {db_path}", file=sys.stderr)
        return 2

    store = SqliteStore(db_path)
    try:
        chain = store.verify_audit_chain()
        if not chain["ok"] and not args.force:
            spot = chain["broken_at"]
            print(
                f"체인이 이미 어긋나 있습니다 (id={spot['id']}, {chain['reason']}).\n"
                "  이 상태를 내보내면 어긋난 사본이 '정본' 이 됩니다.\n"
                "  런북을 먼저 보세요. 그래도 내보내려면 --force.",
                file=sys.stderr,
            )
            return 2

        previous = store.last_audit_export()
        since = 0 if args.full else int(previous.get("last_id") or 0)
        rows = store.export_audit_chain(since_id=since)
        if not rows:
            print(f"내보낼 새 감사가 없습니다 (마지막 id={since}).")
            return 0

        target = Path(args.out)
        # **덮어쓰지 않고 이어 붙인다.** 내보내기의 목적이 밖에 사본을 쌓는 것인데
        # 매번 덮어쓰면 마지막 회차만 남는다.
        mode = "w" if args.full else "a"
        with target.open(mode, encoding="utf-8") as handle:
            for row in rows:
                handle.write(json.dumps(row, ensure_ascii=False) + "\n")

        store.record_audit_export(tip=rows[-1]["row_hash"], last_id=rows[-1]["id"])
    finally:
        store.close()

    print(f"  {len(rows)}행을 {target} 에 {'썼습니다' if args.full else '이어 붙였습니다'}")
    print(f"  마지막 id {rows[-1]['id']} · 팁 {rows[-1]['row_hash'][:16]}…")
    print(
        "\n  이 파일을 **다른 저장소로** 옮기세요. 같은 호스트에 두면 DB 를 고칠 수 있는\n"
        "  사람이 이 파일도 고칠 수 있어서 대조의 의미가 없습니다."
    )
    return 0


def _demo_banner(handles: dict[str, Any], host: str, port: int) -> str:
    base = f"http://{host if host != '0.0.0.0' else 'localhost'}:{port}"
    lines = [
        "",
        "-" * 72,
        "  데모 프로파일 — 목 프로바이더로 GPU 없이 전체를 시연합니다.",
        "-" * 72,
        f"  관제 UI     {base}/ui/",
        f"  통합 가이드  {base}/v1/integration",
        "",
        "  시연 가능:  테넌시 격리 · 가드 1단(패턴) · 배치 라우팅 ·",
        "              장애 폴백 · 비용 예약/정산 · 관제 UI 전체 · 알림",
        "  안 되는 것: 실제 생성 품질, 가드 2단 LLM 분류(내부 노드 전용)",
        "",
    ]
    for tenant_id, tokens in handles.get("tenants", {}).items():
        lines.append(f"  [{tenant_id}]")
        lines.append(f"    tenant_admin  {tokens['tenant_admin']}")
        lines.append(f"    service       {tokens['service']}")
    lines += ["", "-" * 72, ""]
    return "\n".join(lines)


# ── 파서 ────────────────────────────────────────────────────────────────────


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="llm-controlcenter", description="설치형 LLM 클러스터 관제 센터"
    )
    parser.add_argument("--config", help="설정 디렉터리")
    parser.add_argument("--data", help="데이터 디렉터리 (SQLite)")
    parser.add_argument("--keys", help="마스터 KEK 디렉터리")
    parser.add_argument(
        "--no-keys", action="store_true",
        help="키를 만들지도 읽지도 않는다. 원문을 아예 보관하지 않는 구성",
    )
    parser.add_argument("--airgap", action="store_true", help="에어갭 모드")
    parser.add_argument("--version", default="", help=argparse.SUPPRESS)

    sub = parser.add_subparsers(dest="command")

    serve = sub.add_parser("serve", help="API + 관제 UI 를 띄운다")
    serve.add_argument("--host", default="0.0.0.0")
    serve.add_argument("--port", type=int, default=8610)
    serve.add_argument("--log-level", default="INFO")
    serve.add_argument(
        "--demo", action="store_true",
        help="데모 프로파일 — 목 노드와 시드 테넌트 2개로 뜬다",
    )
    serve.add_argument(
        "--no-scheduler", action="store_true",
        help="스케줄러를 띄우지 않는다. 워커를 여러 개 띄울 때 하나만 켜기 위한 것",
    )
    serve.set_defaults(func=cmd_serve)

    boot = sub.add_parser("bootstrap", help="키·관리자·첫 테넌트를 만들고 끝낸다")
    boot.set_defaults(func=cmd_bootstrap)

    doctor = sub.add_parser("doctor", help="진단")
    doctor.add_argument("--probe", action="store_true", help="노드 도달성까지 확인")
    doctor.add_argument("--bundle", help="진단 번들을 이 경로에 쓴다")
    doctor.set_defaults(func=cmd_doctor)

    rotate = sub.add_parser(
        "rotate-kek",
        help="마스터 KEK 를 회전한다 (암호문 재암호화 없음)",
        description=(
            "테넌트 DEK 의 래핑만 새 KEK 로 교체합니다. 저장된 프롬프트·응답 "
            "암호문은 건드리지 않으므로 데이터 양과 무관하게 빠릅니다.\n"
            "절차와 되돌리기는 docs/runbook-key-compromise.md 를 보세요."
        ),
    )
    # **새 키를 인자로 안 받는다.** argv 는 같은 호스트의 다른 사용자에게 `ps` 로
    # 보이고 셸 히스토리에도 남는다. 시크릿 매니저를 쓰는 설치처는 환경 변수로 준다.
    rotate.add_argument(
        "--new-key-env", default="LCC_PROMPT_KEY_NEW",
        help="새 KEK 를 담은 환경 변수 이름. 비어 있으면 새로 만든다",
    )
    rotate.add_argument(
        "--yes", action="store_true",
        help="확인 프롬프트를 건너뛴다. 무인 실행용",
    )
    rotate.set_defaults(func=cmd_rotate_kek)

    export = sub.add_parser(
        "audit-export",
        help="감사를 JSONL 로 내보낸다 (체인 검증의 나머지 절반)",
        description=(
            "해시 체인은 조작을 **드러낼** 뿐 막지 못합니다. DB 에 쓸 수 있는 "
            "공격자는 체인을 통째로 다시 계산할 수 있고, 그 재계산은 밖에 있는 "
            "사본과 대조할 때만 걸립니다. 정기 실행하고 결과를 다른 저장소에 두세요."
        ),
    )
    export.add_argument("--out", default="audit-export.jsonl", help="쓸 파일")
    export.add_argument(
        "--full", action="store_true",
        help="처음부터 다시 내보낸다(파일을 덮어쓴다). 기본은 증분",
    )
    export.add_argument(
        "--force", action="store_true",
        help="체인이 이미 어긋나 있어도 내보낸다",
    )
    export.set_defaults(func=cmd_audit_export)

    return parser


def main(argv: list[str] | None = None) -> int:
    argv = list(sys.argv[1:] if argv is None else argv)
    # `--demo` 만 주면 `serve --demo` 로 읽는다. 데모 안내가 그렇게 짧아야 한다.
    if argv and argv[0] == "--demo":
        argv = ["serve", "--demo"] + argv[1:]

    parser = build_parser()
    args = parser.parse_args(argv)
    if not getattr(args, "func", None):
        parser.print_help()
        return 0
    try:
        return args.func(args)
    except KeyDirectoryUnwritable as exc:
        # **트레이스백으로 끝내지 않는다.** `restart: unless-stopped` 아래에서는
        # 이것이 크래시 루프가 되고, 설치처는 흐르는 로그만 보게 된다.
        # 무엇이 잘못됐고 무엇을 하면 되는지가 마지막 줄에 있어야 한다.
        print(f"\n기동할 수 없습니다.\n  {exc}\n", file=sys.stderr)
        return 1


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
