"""계획서가 못박으라고 한 53개 항목 ↔ 실제 테스트 대조표.

계획서에는 "반드시 테스트로 못박을 것" 53개가 적혀 있다. 그런 목록은 **반드시
어긋난다** — 테스트 이름을 바꾸거나 지우면 표는 조용히 거짓말이 되고, 그때부터
"53개 다 덮었다" 는 말은 아무 의미가 없다.

그래서 표를 여기 코드로 두고 **표에 적힌 테스트가 실제로 존재하는지 매번 확인한다.**
`test_meta.py::test_every_route_has_a_summary` 가 계약 문서에 대해 하는 일을,
여기서는 요구사항 목록에 대해 한다.

이 파일은 새 테스트를 만들지 않는다. 있는 테스트를 가리킬 뿐이다 — 대조표가
스스로 검증까지 하면 그건 표가 아니라 또 하나의 테스트 더미다.
"""

from __future__ import annotations

import ast
import re
from pathlib import Path

import pytest

TESTS_DIR = Path(__file__).resolve().parent

#: 항목 번호 → (계획서의 요구, 그것을 못박는 테스트들).
#:
#: 한 항목에 여러 테스트가 붙는 것은 정상이다 — "테넌트 격리" 는 조회 경로마다
#: 따로 막아야 하고, 한 곳만 막힌 격리는 격리가 아니다.
COVERAGE: dict[int, tuple[str, tuple[str, ...]]] = {
    # ── 테넌트 격리 — 제품의 최대 리스크 ──────────────────────────────────
    1: (
        "테넌트 A 의 토큰으로 B 의 잡·사용량·감사·가드 규칙을 어떤 경로로도 조회할 수 없다",
        (
            "test_one_tenant_cannot_read_another_tenants_job",
            "test_one_tenant_cannot_cancel_another_tenants_job",
            "test_admin_job_list_never_crosses_tenants",
            "test_admin_audit_and_usage_never_cross_tenants",
            "test_guard_rules_are_per_tenant",
            "test_raw_prompt_is_not_readable_across_tenants",
            "test_export_never_crosses_tenants",
            "test_cannot_read_another_tenants_job",
        ),
    ),
    2: (
        "스코프 없는 스토어 조회 경로가 존재하지 않는다(전역 조회는 명시적 API + 감사)",
        (
            "test_tenant_data_methods_require_scope_first",
            "test_tenant_scope_rejects_empty_id",
            "test_platform_scope_requires_a_reason",
            "test_only_the_store_touches_the_connection",
            "test_platform_overview_is_audited",
        ),
    ),
    3: (
        "테넌트 관리자는 플랫폼 베이스라인 가드 규칙을 완화할 수 없다(조이기만 가능)",
        (
            "test_tenant_cannot_loosen_a_baseline_rule",
            "test_tenant_can_tighten_a_baseline_rule",
            "test_tiered_tightening_is_per_boundary",
        ),
    ),
    4: (
        "테넌트 A 의 DEK 로 B 의 암호문이 복호화되지 않는다",
        (
            "test_one_tenants_dek_never_opens_anothers_ciphertext",
            "test_one_tenants_dek_cannot_open_anothers",
        ),
    ),
    5: (
        "tenant_affinity 전용 노드에 다른 테넌트 잡이 배치되지 않는다",
        ("test_tenant_affinity_node_rejects_other_tenants", "test_tenant_affinity_restricts_node"),
    ),
    6: (
        "한 테넌트가 큐를 채워도 다른 테넌트가 굶지 않는다(공정성)",
        ("test_one_tenant_cannot_starve_another", "test_round_robin_interleaves_tenants"),
    ),

    # ── 데이터 경계·거버넌스 ──────────────────────────────────────────────
    7: (
        "placement: [internal] 역할은 어떤 상황에서도 external 노드에 배치되지 않는다",
        (
            "test_internal_only_role_ignores_external_tier_even_if_configured",
            "test_internal_only_role_is_never_placed_externally",
            "test_role_without_external_tier_waits_instead_of_leaking",
            "test_internal_only_role_cannot_reach_external_node",
        ),
    ),
    8: (
        "provider: ollama + data_boundary: external 노드는 내부로 취급되지 않는다",
        (
            "test_data_boundary_is_a_node_property_not_a_provider_property",
            "test_data_boundary_defaults_to_external",
        ),
    ),
    9: (
        "data_boundary 미기재 노드는 external 로 간주된다(fail-safe)",
        ("test_unspecified_data_boundary_defaults_to_external",
         "test_node_without_a_boundary_is_treated_as_external"),
    ),
    10: (
        "필터가 배치보다·저장보다 먼저 돈다. block 된 프롬프트는 어떤 노드에도 안 가고 평문 저장도 안 된다",
        (
            "test_blocked_prompt_never_reaches_storage_or_a_node",
            "test_guard_runs_before_the_job_row_exists",
            "test_job_carries_the_narrowed_boundaries_into_placement",
            "test_narrowed_job_never_lands_on_an_external_node",
            "test_only_the_pipeline_creates_jobs",
        ),
    ),
    11: (
        "2단 분류는 내부 노드에서만 실행된다",
        ("test_classifier_runs_only_on_internal_nodes", "test_classifier_receives_masked_text_not_raw"),
    ),
    12: (
        "KEK 부재 시 원문이 어떤 컬럼에도 평문으로 안 남는다",
        (
            "test_no_ciphertext_is_written_without_a_key",
            "test_vault_disabled_without_a_master_key",
        ),
    ),
    13: (
        "감사 로그 어디에도 매칭된 실제 값이 안 남는다",
        (
            "test_blocked_prompt_still_records_the_detection",
            "test_filter_events_cannot_record_the_matched_value",
            "test_filter_event_api_has_no_value_parameter",
            "test_raw_prompt_read_is_audited",
        ),
    ),
    14: (
        "이메일을 end_user 로 넣어도 DB 에 그 이메일이 남지 않는다",
        (
            "test_end_user_is_hashed_not_stored",
            "test_end_user_that_looks_like_pii_is_flagged",
            "test_same_end_user_hashes_differently_per_tenant",
        ),
    ),

    # ── 클러스터 ──────────────────────────────────────────────────────────
    15: (
        "슬롯 1개 노드에 두 잡이 동시 배치 시도 → 정확히 하나만 성공(메모리 예산도 동일)",
        (
            "test_single_slot_node_accepts_exactly_one",
            "test_concurrent_placement_does_not_oversubscribe",
            "test_memory_budget_is_reserved_not_just_checked",
        ),
    ),
    16: (
        "큐에 잡이 있는 상태에서 cloud 티어를 제거하면 그 잡은 클라우드로 안 간다(안전 필드 교집합)",
        (
            "test_snapshot_intersects_with_current_config",
            "test_empty_intersection_waits_rather_than_fails",
        ),
    ),
    17: (
        "노드를 비활성화해도 큐가 즉시 전멸하지 않는다. 반면 용량으로 못 담는 모델은 즉시 실패한다",
        (
            "test_disabled_node_causes_wait_not_hard_failure",
            "test_oversized_model_fails_immediately",
            "test_draining_blocks_new_but_keeps_running_jobs",
            "test_administrative_wait_eventually_times_out",
        ),
    ),
    18: (
        "metered 노드 실행 중이던 잡은 크래시 복구 시 자동 재큐되지 않고 needs_review 로 남는다",
        (
            "test_crash_recovery_does_not_requeue_metered_jobs",
            "test_crash_recovery_requeues_free_node_jobs",
            "test_start_recovers_running_jobs",
        ),
    ),
    19: (
        "노드 장애 재시도는 같은 노드를 후보에서 제외한다",
        ("test_retry_avoids_the_node_that_just_failed",
         "test_retry_excludes_the_node_that_just_failed",
         "test_another_node_still_wins_over_the_failed_one",
         # 배제는 **다른 후보가 있을 때만**이다. 노드 한 대짜리 구성에서
         # 영구 배제는 곧 재시도 불능이었다(감사 H6).
         "test_a_single_node_install_can_still_retry",
         "test_crash_recovery_does_not_strand_a_single_node_install",
         "test_a_genuinely_dead_node_is_still_refused",
         "test_the_revived_node_is_not_reported_as_rejected"),
    ),
    20: (
        "예산 초과 동시 디스패치는 예약 단계에서 막힌다(완료 후가 아니라)",
        (
            "test_metered_node_reserves_upper_bound",
            "test_budget_exhaustion_blocks_when_no_free_tier",
            "test_budget_exhaustion_demotes_to_the_free_path",
            "test_release_reservation_without_settlement",
            # 예약이 상한이려면 입력 토큰이 들어가야 한다. 스케줄러가 길이를
            # `0` 으로 넘겨서 큐를 지난 모든 잡의 입력이 빠져 있었다(감사 H8).
            "test_the_input_prompt_is_part_of_the_reservation",
            "test_korean_is_not_counted_as_if_it_were_english",
            "test_the_estimator_errs_high_not_low",
        ),
    ),
    21: (
        "헬스 플래핑 방지 · 드레이닝 · 스캔 창 절단 노출 · 임베딩 동기 경로가 배치·경계·비용을 우회하지 않음",
        (
            "test_one_success_does_not_revive_a_node",
            "test_force_drain_clears_immediately",
            "test_scan_window_truncation_is_surfaced",
            "test_no_truncation_flag_when_queue_fits",
            "test_embed_goes_through_the_same_guard",
            "test_embed_settles_cost_and_records_usage",
            "test_embed_frees_the_reservation_when_placement_fails",
        ),
    ),
    22: (
        "배치 우선순위(티어 > 모델 친화 > 부하), 기아 방지가 그 전부를 이김, 재시도 백오프",
        (
            "test_internal_first_tier_wins_over_warm_external_model",
            "test_warm_model_wins_within_the_same_tier",
            "test_least_loaded_wins_when_warmth_is_equal",
            "test_starvation_beats_fairness_and_priority",
            "test_backoff_delays_the_retry",
            "test_backoff_does_not_block_the_lane",
        ),
    ),

    # ── 모델 생애주기 ─────────────────────────────────────────────────────
    23: (
        "미설치 모델 잡이 레인을 막지 않고, 승인 후 자동 재개된다",
        (
            "test_detect_missing_creates_requests_without_blocking_lanes",
            "test_approve_then_pull_reaches_ready",
            "test_install_updates_inventory_immediately",
        ),
    ),
    24: (
        "노드 메모리 예산을 넘는 모델은 설치 전에 거부된다",
        ("test_size_gate_rejects_before_downloading", "test_oversized_model_fails_immediately"),
    ),
    25: (
        "삭제 차단 5종이 각각 동작하고 force 우회 경로가 없다",
        (
            "test_role_in_use_blocks_deletion",
            "test_queued_jobs_block_deletion",
            "test_running_jobs_block_deletion",
            "test_in_progress_install_blocks_deletion",
            "test_embedding_role_blocks_deletion",
            "test_model_deletion_has_no_force_escape_hatch",
            "test_deletion_raises_with_the_blocking_reason",
        ),
    ),
    26: (
        "삭제 시 설치 요청 행이 제거된다(ready 로 두면 부활, rejected 로 두면 거짓 사유로 하드 실패)",
        ("test_deletion_removes_the_request_row",),
    ),
    27: (
        "테넌트 관리자는 모델 설치를 승인할 수 없다(공유 노드 디스크 보호)",
        ("test_tenant_admin_cannot_approve_model_installs",),
    ),

    # ── 계약 자기 서빙 ────────────────────────────────────────────────────
    28: (
        "라우트를 추가하고 요약을 안 달면 테스트가 실패한다",
        (
            "test_every_route_has_a_summary",
            "test_adding_a_route_without_a_summary_is_caught",
            "test_no_orphan_summaries",
            "test_inventory_is_derived_not_handwritten",
        ),
    ),
    29: (
        "OpenAPI 의 role enum 에 다른 테넌트의 역할 이름이 새지 않는다",
        (
            "test_openapi_role_enum_does_not_leak_other_tenants_roles",
            "test_roles_endpoint_lists_only_allowed_roles",
            "test_internal_roles_are_invisible_even_with_wildcard",
            "test_openapi_never_exposes_model_names",
        ),
    ),
    30: (
        "목 서버만으로 통합 코드가 완성 가능하다(노드·토큰 없이)",
        (
            "test_mock_server_uses_only_the_standard_library",
            "test_client_uses_only_the_standard_library",
            "test_client_index_lists_bundled_files",
        ),
    ),
    31: (
        "/healthz 가 인증 없이 응답한다",
        (
            "test_healthz_needs_no_auth",
            "test_healthz_does_not_touch_the_database",
            "test_openapi_marks_healthz_as_unauthenticated",
        ),
    ),

    # ── 가드 품질 ─────────────────────────────────────────────────────────
    32: (
        "오탐률이 임계를 넘으면 audit → block 승격이 거부된다",
        (
            "test_promotion_blocked_when_false_positive_rate_is_high",
            "test_promotion_allowed_when_clean_and_well_sampled",
            "test_promotion_blocked_without_enough_reviews",
        ),
    ),
    33: (
        "구조화 출력 준수율이 임계 미달인 분류 모델은 등록이 거부된다",
        (
            "test_classifier_that_breaks_schema_is_rejected",
            "test_uncertified_classifier_model_is_refused",
            "test_noncompliant_classifier_model_is_refused",
        ),
    ),
    34: (
        "분류 실패가 '민감하지 않음' 판정으로 새지 않고 on_classifier_error 를 탄다",
        (
            "test_classifier_failure_is_not_a_verdict",
            "test_classifier_failures_are_counted_separately",
            "test_classifier_failure_block_policy",
        ),
    ),

    # ── 삭제·내보내기 ─────────────────────────────────────────────────────
    35: (
        "테넌트 DEK 폐기 후 그 테넌트의 기존 암호문이 복호화되지 않는다(crypto-shredding)",
        (
            "test_destroying_the_dek_makes_existing_ciphertext_unreadable",
            "test_tenant_purge_removes_every_scoped_table",
            "test_destroying_the_dek_makes_ciphertext_unreadable",
        ),
    ),
    36: (
        "엔드유저 파기가 그 엔드유저의 데이터만 지운다(다른 엔드유저·테넌트 무영향)",
        (
            "test_purging_one_end_user_leaves_the_others",
            "test_purging_an_end_user_never_touches_another_tenant",
            "test_purge_requires_an_exact_confirmation",
        ),
    ),
    37: (
        "파기 감사에 지워진 내용이 남지 않는다(언제·누가·무엇을만)",
        (
            "test_purge_audit_records_who_and_how_many_not_what",
            "test_purge_unlinks_the_guard_events_but_keeps_the_counts",
            "test_tenant_purge_is_audited_with_the_reason",
        ),
    ),

    # ── 제품 ──────────────────────────────────────────────────────────────
    38: (
        "기본 자격증명이 존재하지 않는다(부트스트랩 랜덤 생성)",
        (
            "test_no_default_credentials_exist",
            "test_bootstrap_tokens_differ_between_installs",
            "test_bootstrap_is_safe_to_rerun",
        ),
    ),
    39: (
        "에어갭 모드에서 클라우드 티어가 자동 비활성화되고 UI 에 표시된다",
        (
            "test_airgap_blocks_external_placement_not_just_registration",
            "test_airgap_still_allows_internal_nodes",
            "test_the_node_grid_marks_what_airgap_disabled",
            "test_session_reports_the_conditions_the_ui_must_show",
        ),
    ),
    40: (
        "구버전이 신버전 스키마 DB 를 읽을 수 있다(전진 호환)",
        (
            "test_schema_migrations_are_add_only",
            "test_an_older_build_can_read_a_newer_schema",
        ),
    ),
    41: (
        "자동 백업에 prompt_cipher 가 포함되지 않는다",
        (
            "test_the_backup_drops_the_ciphertext_and_keeps_the_masked_copy",
            "test_a_backup_of_a_live_wal_database_is_not_empty",
            "test_the_backup_script_says_the_key_is_not_included",
            "test_export_carries_the_masked_copy_never_the_ciphertext",
        ),
    ),
    42: (
        "복원이 스키마 버전을 검사하고, 역할 오버라이드가 되돌아간다는 경고를 띄운다",
        (
            "test_the_restore_warns_about_role_overrides",
            # 감사가 짚은 것: 찍기만 하고 **비교하지 않았다**. 경고는 검사가 아니다.
            "test_restore_refuses_a_newer_schema",
            "test_restore_allows_an_older_schema",
            "test_the_install_path_actually_gates_on_schema",
            "test_the_restore_script_gates_before_asking_for_confirmation",
            # 그리고 복원이 되돌리는 대상은 DB 만이 아니다.
            "test_restore_removes_the_stale_wal",
            "test_restore_leaves_a_way_back",
            "test_the_rollback_copy_includes_unflushed_wal_content",
            "test_restore_puts_the_config_back",
            "test_the_restore_script_does_not_cp_the_database_in_as_root",
        ),
    ),
    43: (
        "data_boundary: external 노드는 TLS·인증 없이 등록되지 않는다",
        (
            "test_external_node_requires_tls_and_auth",
            "test_node_without_a_boundary_is_treated_as_external",
            "test_preflight_states_the_trust_assumption",
        ),
    ),
    44: (
        "알림 실패가 파이프라인을 죽이지 않는다. 로그·알림 어디에도 프롬프트 본문이 없다",
        (
            "test_a_dead_channel_never_kills_the_caller",
            "test_a_notification_never_carries_the_prompt",
            "test_logs_drop_prompt_bodies_and_secrets",
            "test_redact_strips_prompts_and_secrets",
            "test_metrics_never_carry_prompts",
            # **느린 채널은 예외를 내지 않고 그냥 붙잡고 있는다** — 예외만
            # 삼켜서는 "파이프라인을 안 죽인다" 가 반쪽이다(감사 H10).
            "test_a_slow_channel_does_not_stall_the_event_loop",
            "test_a_slow_channel_does_not_stall_a_request",
            "test_the_offloaded_send_still_swallows_failures",
            "test_a_channel_is_assumed_to_block_unless_it_says_otherwise",
        ),
    ),
    45: (
        "진단 번들에 비밀이 마스킹되어 있다",
        (
            "test_diagnostic_bundle_masks_secrets",
            "test_diagnostic_bundle_carries_no_prompts_or_tenant_names",
            "test_doctor_bundle_masks_secrets",
            "test_mask_secret_reveals_length_not_value",
            # `config` 절만 마스킹하고 `cluster` 절이 스냅샷을 통째로 실으면서
            # 같은 값을 원문 테넌트 ID 로 다시 넣고 있었다(감사 H13).
            "test_the_bundle_never_names_a_tenant",
            "test_the_bundle_still_says_how_many_tenants_are_pinned",
            "test_a_budget_alert_does_not_carry_the_tenant_into_the_bundle",
            "test_the_tenant_count_survives_the_strip",
            "test_the_strip_walks_the_whole_structure",
            "test_the_admin_view_still_shows_who_is_pinned",
            # 마스킹돼 있어도 **만들어지지 않으면** 소용이 없다(감사 H11).
            "test_the_bundle_is_produced_even_when_doctor_finds_a_problem",
            "test_the_doctor_exit_code_survives_the_bundle_copy",
            "test_a_failed_bundle_copy_is_reported",
        ),
    ),

    # ── 다국어 ────────────────────────────────────────────────────────────
    46: (
        "로케일을 바꿔도 오류 코드·retryable·규칙 ID 가 바뀌지 않는다 — 사람이 읽는 메시지만 바뀐다",
        (
            "test_locale_changes_the_message_but_never_the_code",
            "test_error_code_is_stable_across_locales",
        ),
    ),
    47: (
        "응답에 기계용 코드와 사람용 메시지가 둘 다 실린다",
        (
            "test_response_carries_both_the_code_and_the_message",
            "test_response_carries_both_code_and_message",
        ),
    ),
    48: (
        "테넌트마다 다른 기본 로케일이 적용된다",
        (
            "test_tenant_default_locale_applies_without_a_header",
            "test_session_falls_back_to_the_tenant_locale",
            "test_tenant_default_applies_when_nothing_else_matches",
        ),
    ),
    49: (
        "로케일 팩을 안 켠 상태에서 그 나라 PII 를 넣으면 안 잡히고, UI 가 그 사실을 표시한다",
        (
            "test_locale_pack_off_means_that_countrys_pii_is_not_caught",
            "test_guard_baseline_shows_which_locale_packs_are_unused",
            "test_session_flags_a_missing_locale_pack",
            "test_meta_names_the_guard_locale_pack",
        ),
    ),

    # ── 프롬프트 해시 ─────────────────────────────────────────────────────
    50: (
        "prompt_hash 가 마스킹 후 + 테넌트 솔트로 만들어진다",
        (
            "test_prompt_hash_is_salted_per_tenant",
            "test_prompt_hash_is_computed_after_masking",
            "test_prompt_hash_is_salted_per_tenant",
        ),
    ),
    51: (
        "system_hash 가 역할의 system 프롬프트 변경에 따라 바뀌고, 평가 결과가 그 값으로 묶인다",
        (
            "test_system_hash_changes_when_the_request_overrides_it",
            "test_system_hash_is_unsalted_so_it_compares_across_tenants",
            "test_system_hash_is_not_salted_so_it_compares_across_tenants",
        ),
    ),

    # ── 3단 레이트리밋 ────────────────────────────────────────────────────
    52: (
        "테넌트가 서비스를 여러 개 만들어도 테넌트 총량 상한을 우회할 수 없다",
        ("test_tenant_ceiling_cannot_be_bypassed_with_more_services",),
    ),
    53: (
        "429 응답이 어느 단계(테넌트·서비스·엔드유저)에서 걸렸는지 알려준다",
        (
            "test_rate_limit_names_the_tier_that_tripped",
            "test_error_names_which_tier_tripped",
        ),
    ),
}


def collect_test_names() -> set[str]:
    """테스트 함수 이름 전부. **파일을 파싱해서 모은다** — 목록을 손으로 적으면
    이 대조표를 검증하려고 만든 장치가 또 하나의 손으로 관리하는 표가 된다."""
    names: set[str] = set()
    for path in TESTS_DIR.glob("test_*.py"):
        tree = ast.parse(path.read_text(encoding="utf-8"))
        for node in ast.walk(tree):
            if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
                if node.name.startswith("test_"):
                    names.add(node.name)
    return names


ALL_TESTS = collect_test_names()


def test_the_plan_has_fifty_three_fixed_items():
    """계획서가 못박으라고 한 항목 수. 빠뜨린 번호가 있으면 여기서 걸린다."""
    assert sorted(COVERAGE) == list(range(1, 54))


@pytest.mark.parametrize("item", sorted(COVERAGE))
def test_every_item_is_covered_by_a_test_that_exists(item):
    """**표에 적힌 테스트가 실제로 있는지 확인한다.**

    테스트 이름을 바꾸거나 지우면 여기서 실패한다 — 그러지 않으면 표는 조용히
    거짓말이 되고, 그때부터 "53개 다 덮었다" 는 말이 아무 의미가 없어진다.
    """
    requirement, tests = COVERAGE[item]
    assert tests, f"{item}. {requirement} — 덮는 테스트가 없다"

    missing = sorted(name for name in tests if name not in ALL_TESTS)
    assert not missing, f"{item}. {requirement}\n  존재하지 않는 테스트: {missing}"


def test_no_item_leans_on_a_single_shared_test():
    """서로 다른 항목이 같은 테스트 **하나만** 가리키면 둘 중 하나는 안 덮인 것이다."""
    solo = {
        item: tests[0] for item, (_, tests) in COVERAGE.items() if len(tests) == 1
    }
    duplicated = [
        (a, b, name)
        for a, name in solo.items()
        for b, other in solo.items()
        if a < b and name == other
    ]
    assert not duplicated, f"같은 테스트 하나에 기대는 항목들: {duplicated}"


def test_the_readme_and_plan_agree_on_what_is_not_covered():
    """부채 목록은 **테스트가 없는 것**을 적는 자리다. 비면 안 적은 것이다."""
    readme = (TESTS_DIR.parent / "README.md").read_text(encoding="utf-8")
    debts = re.findall(r"^\|\s*([^|]+?)\s*\|\s*[^|]+\|\s*$", readme, re.M)
    assert len(debts) > 8, "README 의 부채 목록이 비어 있거나 너무 짧다"


def test_coverage_map_points_at_the_real_suite():
    """대조표가 가리키는 테스트가 전체의 일부인지 — 오타로 전부 통과하지 않게."""
    referenced = {name for _, tests in COVERAGE.values() for name in tests}
    assert len(referenced) > 100
    assert referenced <= ALL_TESTS
