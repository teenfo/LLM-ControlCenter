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
from .crypto import KeyVault
from .evals import Evaluator
from .guard import Guard
from .i18n import Translator
from .models import ModelRegistrar
from .notify import Notifier, channels_from_env
from .observability import configure_logging, diagnostic_bundle
from .pipeline import Pipeline
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
        self.pipeline = Pipeline(
            config, store, self.cluster, self.guard,
            vault=vault, accountant=self.accountant, evaluator=self.evaluator,
        )
        # **스케줄러는 싱글턴이다.** 워커마다 돌면 잡이 중복 배치된다 —
        # 성능이 필요해서 워커를 늘리는 순간 조용히 깨지는 구조를 만들지 않는다.
        self.scheduler = Scheduler(
            config, store, self.cluster, accountant=self.accountant,
            registrar=self.registrar, notifier=self.notifier,
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
