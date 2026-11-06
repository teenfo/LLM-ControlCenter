"""역할 해석 — 설정 기본값 + 테넌트 오버라이드.

**역할을 읽는 곳이 둘 이상이면 해석도 둘이 된다.** 파이프라인은 제출 시점에
한도·타임아웃을 보고, 스케줄러는 디스패치 시점에 배치 티어와 `internal_only` 를
본다. 각자 `config.roles[...]` 를 직접 읽으면 한쪽만 오버라이드를 반영하는 조합이
생기고, 실제로 **양쪽 다 반영하지 않아 오버라이드가 죽은 기능이었다** —
저장·감사·조회·내보내기까지 전부 동작하는데 요청 처리만 원본 설정을 봤다.

그래서 해석을 여기 하나로 모은다. 스토어의 테넌트 초크포인트와 같은 이유다.

### 캐시를 두지 않는다

`role_overrides` 는 `(tenant_id, role)` 이 기본 키라 테넌트별 조회가 인덱스를 탄다.
요청당 작은 조회 하나이고, 컨트롤 플레인은 추론을 하지 않아 여기가 병목이 아니다.

캐시를 두면 무효화를 해야 하고, **무효화를 빠뜨린 캐시가 이 저장소에서 이미 한 번
사고를 냈다**(가드 정규식 캐시 — 관리자가 고친 규칙이 재기동 전까지 옛 규칙으로
돌았다). 같은 실수를 다른 자리에서 반복하지 않는다.
"""

from __future__ import annotations

from typing import Any, Mapping

from .config import Config, Role, merge_overrides
from .store import SqliteStore, TenantScope


class RoleResolver:
    """테넌트의 실효 역할을 돌려준다.

    잘못된 오버라이드 행은 **건너뛴다** — 데이터 한 줄 때문에 그 테넌트의 요청이
    전부 죽으면 롤백이 더 어려워진다. 건너뛴 사실은 `invalid_for()` 로 드러낸다.
    """

    def __init__(self, config: Config, store: SqliteStore) -> None:
        self._config = config
        self._store = store

    def roles_for(self, tenant_id: str) -> Mapping[str, Role]:
        merged, _ = self._merged(tenant_id)
        return merged

    def get(self, tenant_id: str, role_name: str) -> Role | None:
        return self.roles_for(tenant_id).get(role_name)

    def invalid_for(self, tenant_id: str) -> Mapping[str, Mapping[str, str]]:
        """건너뛴 오버라이드와 사유. 관제 UI 가 드리프트를 보여줄 근거다."""
        _, invalid = self._merged(tenant_id)
        return invalid

    def _merged(self, tenant_id: str) -> tuple[dict[str, Role], dict[str, dict[str, str]]]:
        if not tenant_id:
            return dict(self._config.roles), {}
        try:
            overrides = self._store.get_role_overrides(TenantScope(tenant_id))
        except Exception:
            # 오버라이드를 못 읽는다고 요청을 죽이지 않는다. 원본 설정으로 간다 —
            # 오버라이드는 좁히는 방향이든 넓히는 방향이든 **정책**이고,
            # 정책을 못 읽었을 때의 안전한 기본값은 배포본 그대로다.
            return dict(self._config.roles), {}
        if not overrides:
            return dict(self._config.roles), {}
        return merge_overrides(self._config.roles, overrides)


def resolver_for(config: Config, store: SqliteStore, existing: Any = None) -> RoleResolver:
    """주입된 것이 있으면 그것을, 없으면 새로 만든다."""
    return existing if isinstance(existing, RoleResolver) else RoleResolver(config, store)
