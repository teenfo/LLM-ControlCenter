"""설정 로딩·검증 계약.

여기서 고정하는 것 중 두 개는 제품의 안전 보증 그 자체다:
  - 데이터 경계의 기본값이 external 이다 (실수가 "새는 쪽"으로 향하지 않게)
  - internal_only 역할은 어떤 설정으로도 경계 밖에 닿지 않는다
"""

from __future__ import annotations

import textwrap
from pathlib import Path

import pytest

from app.config import (
    EXTERNAL,
    INTERNAL,
    ConfigError,
    Node,
    Role,
    apply_override,
    load_config,
    merge_overrides,
    validate_role_fields,
)

REPO_CONFIG = Path(__file__).resolve().parents[1] / "config"


def write_config(tmp_path: Path, **files: str) -> Path:
    """최소 설정 디렉터리를 만든다. 넘긴 파일만 덮어쓴다."""
    defaults = {
        "nodes.yaml": """
            n-int:
              provider: mock
              data_boundary: internal
              mem_budget_gb: 16
              tags: [internal]
            """,
        "roles.yaml": """
            r:
              model: m
              lane: interactive
              placement: [internal]
            """,
        "lanes.yaml": "interactive:\n  max_concurrent: 1\n",
        "guard.yaml": "settings: {}\n",
        "pricing.yaml": "mock:\n  \"*\": {input_per_mtok: 0.0, output_per_mtok: 0.0}\n",
        "thresholds.yaml": "max_retries: 3\n",
        "catalog.yaml": "models: []\n",
    }
    defaults.update(files)
    for name, body in defaults.items():
        (tmp_path / name).write_text(textwrap.dedent(body).lstrip(), encoding="utf-8")
    return tmp_path


# ── 데이터 경계 ──────────────────────────────────────────────────────────────


def test_data_boundary_defaults_to_external(tmp_path):
    """경계를 안 적은 노드는 external 로 간주된다.

    반대로 기본값을 internal 로 두면, 경계를 적는 것을 깜빡한 노드가 조용히
    "안전한 곳" 취급을 받는다. 실수는 새는 쪽이 아니라 막는 쪽으로 향해야 한다.
    """
    cfg = load_config(
        write_config(
            tmp_path,
            **{
                "nodes.yaml": """
                    unspecified:
                      provider: mock
                      tags: [internal]
                    """
            },
        )
    )
    assert cfg.nodes["unspecified"].data_boundary == EXTERNAL
    assert cfg.nodes["unspecified"].is_internal is False


def test_ollama_provider_does_not_imply_internal():
    """provider 가 ollama 라고 경계 안인 것이 아니다.

    임대 GPU(vast.ai·RunPod)의 Ollama 는 소프트웨어가 같아도 프롬프트가 남의 기계로 나간다.
    경계를 프로바이더에 걸면 가드 분류기의 "내부 전용" 보장이 그 순간 무너진다.
    """
    rented = Node(name="rented", provider="ollama", data_boundary=EXTERNAL)
    on_prem = Node(name="on-prem", provider="ollama", data_boundary=INTERNAL)

    assert rented.is_internal is False
    assert on_prem.is_internal is True


def test_internal_only_role_cannot_reach_external_node(tmp_path):
    """internal_only 역할이 external 노드에 닿으면 기동을 거부한다."""
    with pytest.raises(ConfigError, match="internal_only"):
        load_config(
            write_config(
                tmp_path,
                **{
                    "nodes.yaml": """
                        leaky:
                          provider: mock
                          data_boundary: external
                          tags: [anywhere]
                        """,
                    "roles.yaml": """
                        _guard_classify:
                          model: guard
                          lane: interactive
                          placement: [anywhere]
                          internal_only: true
                        """,
                },
            )
        )


def test_invalid_boundary_value_is_rejected(tmp_path):
    with pytest.raises(ConfigError, match="data_boundary"):
        load_config(
            write_config(
                tmp_path,
                **{"nodes.yaml": "n:\n  provider: mock\n  data_boundary: maybe\n  tags: [internal]\n"},
            )
        )


# ── 티어 매칭과 모델 해석 ─────────────────────────────────────────────────────


def test_tier_matches_name_or_tag():
    node = Node(name="gpu-01", provider="mock", tags=("internal", "gpu"))
    assert node.matches_tier("gpu-01")   # 노드 이름
    assert node.matches_tier("internal")  # 태그
    assert not node.matches_tier("external")


def test_tier_models_override_base_model():
    role = Role(
        name="summarize",
        model="small-local",
        placement=("internal", "external"),
        tier_models={"external": "cloud-model"},
    )
    assert role.model_for_tier("internal") == "small-local"
    assert role.model_for_tier("external") == "cloud-model"


def test_tenant_affinity_restricts_node():
    shared = Node(name="shared", provider="mock")
    dedicated = Node(name="ded", provider="mock", tenant_affinity=("acme",))

    assert shared.allows_tenant("acme") and shared.allows_tenant("globex")
    assert dedicated.allows_tenant("acme")
    assert not dedicated.allows_tenant("globex")


# ── 검증 ────────────────────────────────────────────────────────────────────


def test_unknown_lane_is_rejected(tmp_path):
    with pytest.raises(ConfigError, match="레인"):
        load_config(
            write_config(
                tmp_path,
                **{"roles.yaml": "r:\n  model: m\n  lane: nonexistent\n  placement: [internal]\n"},
            )
        )


def test_placement_matching_no_node_is_rejected(tmp_path):
    with pytest.raises(ConfigError, match="placement"):
        load_config(
            write_config(
                tmp_path,
                **{"roles.yaml": "r:\n  model: m\n  lane: interactive\n  placement: [typo-tier]\n"},
            )
        )


@pytest.mark.parametrize("timeout", [0, -1, 3601])
def test_timeout_bounds(tmp_path, timeout):
    with pytest.raises(ConfigError, match="timeout"):
        load_config(
            write_config(
                tmp_path,
                **{
                    "roles.yaml": f"r:\n  model: m\n  lane: interactive\n"
                    f"  placement: [internal]\n  timeout: {timeout}\n"
                },
            )
        )


def test_bad_guard_regex_fails_at_startup(tmp_path):
    """잘못된 정규식은 런타임이 아니라 기동 시점에 잡는다."""
    with pytest.raises(ConfigError, match="정규식"):
        load_config(
            write_config(
                tmp_path,
                **{
                    "guard.yaml": """
                        locale_packs:
                          common:
                            rules:
                              - id: broken
                                kind: pattern
                                pattern: '([unclosed'
                                action: audit
                        """
                },
            )
        )


def test_llm_rule_requires_description(tmp_path):
    """맥락 규칙은 관리자가 문장으로 정의하는 것이 전부다."""
    with pytest.raises(ConfigError, match="description"):
        load_config(
            write_config(
                tmp_path,
                **{
                    "guard.yaml": """
                        context_rules:
                          - id: vague
                            kind: llm
                            action: audit
                        """
                },
            )
        )


# ── 오버라이드 ──────────────────────────────────────────────────────────────


@pytest.mark.parametrize("frozen", ["kind", "system", "internal_only"])
def test_frozen_fields_cannot_be_overridden(frozen):
    """kind 는 큐를 우회하는 동기 경로로 새어나가고, system 은 "프롬프트는 호출자 소유"와
    충돌하며, internal_only 는 설정으로 풀리면 안전장치가 아니다."""
    errors = validate_role_fields({frozen: "anything"})
    assert frozen in errors


def test_overridable_fields_pass_validation():
    assert validate_role_fields(
        {"model": "m2", "lane": "batch", "timeout": 60, "placement": ["internal"]}
    ) == {}


def test_apply_override_replaces_only_given_fields():
    role = Role(name="r", model="m", system="원래 프롬프트", timeout=120)
    updated = apply_override(role, {"model": "m2", "timeout": 60})

    assert updated.model == "m2"
    assert updated.timeout == 60
    assert updated.system == "원래 프롬프트"  # 건드리지 않은 필드는 그대로


def test_invalid_override_does_not_block_startup():
    """잘못된 오버라이드 한 줄 때문에 서비스가 안 뜨면 롤백이 더 어려워진다.

    건너뛰고 사유를 함께 돌려주며, 상태 API 가 그것을 노출한다.
    """
    roles = {"good": Role(name="good", model="m"), "bad": Role(name="bad", model="m")}
    merged, invalid = merge_overrides(
        roles,
        {
            "good": {"timeout": 60},
            "bad": {"timeout": 99999},        # 범위 초과
            "ghost": {"model": "m"},          # 없는 역할
        },
    )

    assert merged["good"].timeout == 60       # 정상 행은 적용된다
    assert merged["bad"].timeout == 120       # 잘못된 행은 기본값 유지
    assert set(invalid) == {"bad", "ghost"}   # 사유가 드러난다


# ── 실제 번들 설정 ───────────────────────────────────────────────────────────


def test_shipped_config_loads():
    """번들에 담기는 기본 설정이 실제로 유효한지."""
    cfg = load_config(REPO_CONFIG)

    assert cfg.roles["_guard_classify"].internal_only is True
    assert cfg.roles["_guard_classify"].lane == "guard"
    assert "guard" in cfg.lanes, "가드 레인이 없으면 보안 경로가 대형 잡 뒤에 줄 선다"
    assert cfg.nodes["mock-cloud"].is_internal is False


def test_shipped_config_has_no_baseline_block_rules():
    """베이스라인에 block 이 없다.

    새 규칙을 바로 block 으로 켜면 오탐이 프로덕션을 세우고, 그러면 관리자가 규칙을 꺼버린다.
    차단은 설치처가 자기 데이터를 보고 결정할 일이다.
    """
    cfg = load_config(REPO_CONFIG)
    for rule in cfg.guard_rules:
        for boundary in (INTERNAL, EXTERNAL):
            if rule.action_for_boundary(boundary) == "block":
                # 티어별로 준 것(내부는 보되 밖으로는 막는)만 예외로 허용한다.
                assert not isinstance(rule.action, str), (
                    f"규칙 {rule.id} 이 전 티어 block 으로 시작한다"
                )


def test_checksum_validated_rules_mask_by_default():
    """체크섬으로 검증되는 규칙은 오탐률이 낮으므로 audit 에 머물지 않는다.

    마스킹은 요청을 막지 않으므로 도입 첫날 프로덕션을 세우지 않는다.
    """
    cfg = load_config(REPO_CONFIG)
    checked = [r for r in cfg.guard_rules if r.checksum]
    assert checked, "체크섬 검증 규칙이 하나도 없다"
    for rule in checked:
        assert rule.action_for_boundary(EXTERNAL) in ("partial", "full", "block"), (
            f"체크섬 규칙 {rule.id} 이 통과 등급으로 시작한다"
        )


def test_locale_pack_filtering():
    """팩을 안 켜면 그 나라 PII 는 안 잡힌다 — 이 동작 자체를 고정한다."""
    cfg = load_config(REPO_CONFIG)

    ko_ids = {r.id for r in cfg.rules_for_locales(["ko_KR"])}
    en_ids = {r.id for r in cfg.rules_for_locales(["en_US"])}

    assert "kr_rrn" in ko_ids
    assert "kr_rrn" not in en_ids, "로케일 팩을 안 켰는데 그 나라 규칙이 적용됐다"
    assert "us_ssn" in en_ids
    assert "credit_card" in ko_ids and "credit_card" in en_ids, "common 팩은 항상 켜진다"
