"""모델 생애주기 — 탐지 · 승인 · 설치 · 삭제.

**설치처가 노드를 등록한 뒤 모델을 어떻게 얹는가가 온보딩의 절반이다.**

```
역할이 미설치 모델을 참조
  → 배치 필터에서 그 노드만 스킵 (레인을 막지 않는다)
  → model_requests 에 pending 생성 + 알림
  → 승인                                  ← 플랫폼 관리자 권한
  → 컨트롤 플레인이 그 노드의 pull 을 직접 호출 — progress 0~100
  → ready → 대기하던 잡이 자동 재개
```

승인이 플랫폼 관리자 권한인 이유: 승인은 그 노드 디스크에 수 GB 를 내려받는 상태
변경이고, **노드는 테넌트 공유 자원이다.** 테넌트 관리자가 남의 테넌트도 쓰는 노드의
디스크를 채울 수 없어야 한다.
"""

from __future__ import annotations

import time
from dataclasses import dataclass
from typing import Any, Callable, Iterable, Sequence

from .cluster import Cluster
from .config import CatalogEntry, Config
from .i18n import ApiError
from .providers import BackendError
from .store import SqliteStore

PENDING = "pending"
APPROVED = "approved"
PULLING = "pulling"
READY = "ready"
REJECTED = "rejected"
FAILED = "failed"

#: 이 상태의 요청을 기다리던 잡은 **무한 대기 대신 명확한 오류**로 끝난다.
DEAD_STATUSES = frozenset({REJECTED, FAILED})

#: 삭제 차단 사유. `force` 는 없다 — 다섯 가지가 전부 실제 고장으로 이어진다.
BLOCK_ROLE_IN_USE = "role_in_use"
BLOCK_QUEUED_JOBS = "queued_jobs"
BLOCK_RUNNING = "running"
BLOCK_INSTALLING = "installing"
BLOCK_EMBEDDING_ROLE = "embedding_role"


@dataclass(frozen=True)
class InstallRequest:
    id: str
    node: str
    model: str
    status: str
    progress: int = 0
    est_size_gb: float = 0.0
    roles: tuple[str, ...] = ()
    error: str | None = None


class ModelRegistrar:
    """모델 설치 요청과 삭제를 관리한다."""

    def __init__(
        self,
        config: Config,
        cluster: Cluster,
        store: SqliteStore,
        *,
        now: Callable[[], float] = time.time,
        notify: Callable[[str, dict[str, Any]], None] | None = None,
    ) -> None:
        self._config = config
        self._cluster = cluster
        self._store = store
        self._now = now
        self._notify = notify or (lambda event, detail: None)
        self._catalog = {e.name: e for e in config.catalog}

    # -- 카탈로그 -------------------------------------------------------------

    def catalog_search(self, query: str = "") -> list[CatalogEntry]:
        """번들에 담긴 큐레이션 목록에서 찾는다.

        **모델 레지스트리를 스크레이핑하지 않는다** — 검색 API 가 없어 HTML 파싱에
        기대야 하고, 그러면 남의 사이트 개편에 제품이 끌려 죽는다. 에어갭 설치에서는
        이 목록이 오프라인 번들에 담긴 모델 목록이 된다.
        """
        needle = query.strip().lower()
        return [
            entry
            for entry in self._config.catalog
            if not needle or needle in entry.name.lower() or needle in entry.note.lower()
        ]

    def estimated_size_gb(self, model: str) -> float:
        entry = self._catalog.get(model)
        return entry.est_size_gb if entry else 0.0

    # -- 요청 -----------------------------------------------------------------

    def request_install(
        self,
        node: str,
        model: str,
        *,
        requested_by: str = "",
        roles: Sequence[str] = (),
        auto: bool = False,
    ) -> InstallRequest:
        """설치 요청을 만든다. 이미 있으면 그것을 돌려준다.

        **크기 게이트가 여기 있다.** 추정 크기가 대상 노드의 메모리 예산을 넘으면
        설치 *전에* 거부한다 — 안 그러면 20GB 를 잘 받아 놓고 실행할 때마다 잡이 죽는다.
        """
        state = self._cluster.state(node)
        if state is None:
            raise ApiError("node_unreachable", status=404, params={"node": node})

        if not state.provider.capabilities.requires_model_install:
            # 클라우드 모델은 설치가 없다. 요청 자체가 성립하지 않는다.
            raise ApiError("unsupported_operation", status=400, params={"node": node})

        existing = self._store.get_model_request(node, model)
        if existing is not None and existing["status"] not in DEAD_STATUSES:
            return _to_request(existing)

        size = self.estimated_size_gb(model)
        budget = state.node.mem_budget_gb
        if size and budget is not None and size > budget:
            raise ApiError(
                "oversized_model", status=409,
                params={"size": size, "node": node, "budget": budget},
            )

        if existing is not None:
            # 거부·실패했던 요청을 되살린다. 사람이 다시 올리는 경우다.
            self._store.update_model_request(
                existing["id"], status=PENDING, error=None, progress=0, est_size_gb=size
            )
            request_id = existing["id"]
        else:
            request_id = self._store.create_model_request(
                node, model, requested_by=requested_by, roles=roles, est_size_gb=size
            )

        self._store.audit(
            requested_by or "auto", "request_model_install",
            target=f"{node}/{model}", detail={"auto": auto, "est_size_gb": size},
        )
        # 사람이 모르면 조용히 멈추는 지점 — 알림의 기준이 정확히 이것이다.
        self._notify("model_approval_pending", {"node": node, "model": model})

        return _to_request(self._store.get_model_request_by_id(request_id))

    def detect_missing(self) -> list[InstallRequest]:
        """역할이 참조하는데 어느 노드에도 없는 모델을 찾아 요청을 만든다.

        배치 필터가 그 노드만 스킵하므로 **레인은 막히지 않는다.** 여기서는 요청만 남긴다.
        """
        created: list[InstallRequest] = []

        for role in self._config.roles.values():
            for tier in role.placement:
                model = role.model_for_tier(tier)
                for state in self._cluster.nodes.values():
                    if not state.node.matches_tier(tier) or not state.node.enabled:
                        continue
                    if not state.provider.capabilities.requires_model_install:
                        continue
                    # 프로브 전에는 인벤토리를 모른다. 모른다는 이유로 요청하지 않는다.
                    if not state.models or model in state.models:
                        continue

                    try:
                        created.append(
                            self.request_install(
                                state.name, model, requested_by="auto",
                                roles=[role.name], auto=True,
                            )
                        )
                    except ApiError:
                        # 크기 게이트에 걸린 노드는 건너뛴다. 다른 노드가 받을 수 있다.
                        continue
        return created

    # -- 승인 -----------------------------------------------------------------

    def approve(self, request_id: str, *, actor: str) -> InstallRequest:
        """승인. 권한 확인은 API 계층이 하고 여기서는 상태만 옮긴다."""
        row = self._store.get_model_request_by_id(request_id)
        if row is None:
            raise ApiError("job_not_found", status=404)

        self._store.update_model_request(request_id, status=APPROVED, decided_at=self._now())
        self._store.audit(
            actor, "approve_model_install", target=f"{row['node']}/{row['model']}"
        )
        return _to_request(self._store.get_model_request_by_id(request_id))

    def reject(self, request_id: str, *, actor: str, reason: str = "") -> InstallRequest:
        """거부. 거부한 모델은 다시 물어보지 않는다 — 기다리던 잡은 명확한 오류로 끝난다."""
        row = self._store.get_model_request_by_id(request_id)
        if row is None:
            raise ApiError("job_not_found", status=404)

        self._store.update_model_request(
            request_id, status=REJECTED, error=reason or "관리자가 거부함",
            decided_at=self._now(),
        )
        self._store.audit(
            actor, "reject_model_install",
            target=f"{row['node']}/{row['model']}", detail={"reason": reason},
        )
        return _to_request(self._store.get_model_request_by_id(request_id))

    # -- 설치 -----------------------------------------------------------------

    async def process_approved(self) -> list[InstallRequest]:
        """승인된 요청을 실제로 내려받는다. 배경 루프가 주기적으로 부른다."""
        results: list[InstallRequest] = []

        for row in self._store.list_model_requests(status=APPROVED):
            results.append(await self._pull(row))
        return results

    async def _pull(self, row: Any) -> InstallRequest:
        request_id, node, model = row["id"], row["node"], row["model"]
        state = self._cluster.state(node)
        if state is None:
            self._store.update_model_request(
                request_id, status=FAILED, error=f"노드 {node} 가 없다"
            )
            return _to_request(self._store.get_model_request_by_id(request_id))

        self._store.update_model_request(request_id, status=PULLING, progress=0)

        def on_progress(percent: int) -> None:
            self._store.update_model_request(request_id, progress=percent)

        try:
            await state.provider.pull(model, on_progress=on_progress)
        except BackendError as exc:
            self._store.update_model_request(
                request_id, status=FAILED, error=str(exc), decided_at=self._now()
            )
            self._notify("model_failed", {"node": node, "model": model, "reason": str(exc)})
            return _to_request(self._store.get_model_request_by_id(request_id))

        # 인벤토리를 즉시 갱신한다 — 다음 헬스 주기를 기다리면 대기 잡이 그만큼 더 굶는다.
        state.models = frozenset(state.models | {model})
        self._store.update_model_request(
            request_id, status=READY, progress=100, decided_at=self._now()
        )
        self._notify("model_ready", {"node": node, "model": model})
        return _to_request(self._store.get_model_request_by_id(request_id))

    def dead_request_for(self, node: str, model: str) -> str | None:
        """이 (노드, 모델) 의 요청이 죽었는가. 대기 잡을 끝낼 사유를 준다."""
        row = self._store.get_model_request(node, model)
        if row is not None and row["status"] in DEAD_STATUSES:
            return row["error"] or row["status"]
        return None

    # -- 삭제 -----------------------------------------------------------------

    def deletion_blockers(self, node: str, model: str) -> list[str]:
        """삭제를 막는 사유들. **`force` 는 없다** — 다섯 가지가 전부 실제 고장으로 이어진다."""
        blockers: list[str] = []

        roles_using = [
            role
            for role in self._config.roles.values()
            if any(role.model_for_tier(t) == model for t in role.placement)
        ]
        if roles_using:
            # 지워도 다음 요청에서 곧바로 재설치 대기 — 역할을 먼저 바꿔야 한다.
            blockers.append(BLOCK_ROLE_IN_USE)

        if any(role.is_embed for role in roles_using):
            # 임베딩은 동기 경로라 소비자가 즉시 503 을 받는다.
            blockers.append(BLOCK_EMBEDDING_ROLE)

        queued_by_role = self._store.queued_roles()
        if any(queued_by_role.get(role.name) for role in roles_using):
            blockers.append(BLOCK_QUEUED_JOBS)

        counts = self._store.infra_job_counts(node=node, model=model)
        if counts.get("running"):
            blockers.append(BLOCK_RUNNING)

        request = self._store.get_model_request(node, model)
        if request is not None and request["status"] in (APPROVED, PULLING):
            blockers.append(BLOCK_INSTALLING)

        return blockers

    async def delete(self, node: str, model: str, *, actor: str) -> None:
        blockers = self.deletion_blockers(node, model)
        if blockers:
            raise ApiError(
                "model_in_use", status=409,
                params={"model": model, "reason": ",".join(blockers)},
            )

        state = self._cluster.state(node)
        if state is None:
            raise ApiError("node_unreachable", status=404, params={"node": node})

        await state.provider.delete(model)
        state.models = frozenset(state.models - {model})

        # 요청 행 자체를 지운다 — ready 로 두면 되살아나고, rejected 로 두면
        # 이후 잡이 "설치가 거부됨" 이라는 거짓 사유로 하드 실패한다.
        self._store.delete_model_request(node, model)
        self._store.audit(actor, "delete_model", target=f"{node}/{model}")

    # -- 관제 -----------------------------------------------------------------

    def pending_count(self) -> int:
        return len(self._store.list_model_requests(status=PENDING))

    def snapshot(self) -> list[dict[str, Any]]:
        """관제 UI 용. 삭제 차단 사유를 함께 실어 왜 못 지우는지 보여준다."""
        rows = []
        for state in self._cluster.nodes.values():
            for model in sorted(state.models):
                rows.append(
                    {
                        "node": state.name,
                        "model": model,
                        "est_size_gb": self.estimated_size_gb(model),
                        "loaded": state.loaded_model == model,
                        "deletion_blockers": self.deletion_blockers(state.name, model),
                    }
                )
        return rows


def _to_request(row: Any) -> InstallRequest:
    import json

    return InstallRequest(
        id=row["id"],
        node=row["node"],
        model=row["model"],
        status=row["status"],
        progress=int(row["progress"] or 0),
        est_size_gb=float(row["est_size_gb"] or 0.0),
        roles=tuple(json.loads(row["roles_json"] or "[]")),
        error=row["error"],
    )
