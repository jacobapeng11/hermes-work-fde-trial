from __future__ import annotations

from datetime import datetime, timezone

import pytest
from fastapi import HTTPException

from app.api.v1.admin import usage as usage_api
from app.schemas.admin import AdminPresenceSummaryResponse
from app.services.admin_auth import AdminPrincipal
from app.services.admin_kubernetes_client import AdminKubernetesError
from app.services.platform_presence import (
    PlatformPresenceDashboard,
    PlatformPresenceMonitor,
    PlatformPresenceUser,
)
from app.services.platform_session import (
    PlatformPresenceSummary,
    PlatformSessionUnavailable,
)


class FakePresenceStore:
    def __init__(self, *, unavailable: bool = False) -> None:
        self.unavailable = unavailable

    async def presence_summary(
        self,
        org_id: int,
        *,
        window_seconds: int,
    ) -> PlatformPresenceSummary:
        if self.unavailable:
            raise PlatformSessionUnavailable("platform session store unavailable")
        return PlatformPresenceSummary(
            web_user_ids=frozenset({7, 8}),
            desktop_user_ids=frozenset({7}),
            window_seconds=window_seconds,
            observed_at=datetime(2026, 10, 13, 8, 30, tzinfo=timezone.utc),
        )


class FakeDirectoryService:
    def __init__(self, _db: object) -> None:
        pass

    async def dashboard(
        self,
        presence: PlatformPresenceSummary,
        *,
        org_id: int,
    ) -> PlatformPresenceDashboard:
        assert org_id == 1
        users = {
            7: PlatformPresenceUser(user_id=7, name="张三", user_code="10001"),
            8: PlatformPresenceUser(user_id=8, name="李四", user_code="10002"),
            9: PlatformPresenceUser(user_id=9, name="王五", user_code="10003"),
        }

        def ordered(user_ids: frozenset[int]) -> tuple[PlatformPresenceUser, ...]:
            return tuple(
                sorted(
                    (users[user_id] for user_id in user_ids),
                    key=lambda user: user.display_name,
                )
            )

        return PlatformPresenceDashboard(
            web_users=ordered(presence.web_user_ids),
            desktop_users=ordered(presence.desktop_user_ids),
            daily_login_users=(
                PlatformPresenceUser(user_id=9, name="王五", user_code="10003"),
            ),
            daily_login_since=datetime(2026, 10, 13, 0, 0, tzinfo=timezone.utc),
            window_seconds=presence.window_seconds,
            observed_at=presence.observed_at,
        )


class FakeRuntimeGate:
    def __init__(self, ready_user_ids: frozenset[int]) -> None:
        self.ready_user_ids = ready_user_ids

    async def personal_runtime_ready_user_ids(
        self,
        principal: AdminPrincipal,
    ) -> frozenset[int]:
        assert principal.org_id == 1
        return self.ready_user_ids


class UnavailableRuntimeGate:
    async def personal_runtime_ready_user_ids(
        self,
        _principal: AdminPrincipal,
    ) -> frozenset[int]:
        raise AdminKubernetesError("Kubernetes runtime data is unavailable")


def principal() -> AdminPrincipal:
    return AdminPrincipal(org_id=1, user_id=1, roles=("admin",))


async def invoke_presence(
    monitor: PlatformPresenceMonitor,
    runtime_gate: FakeRuntimeGate | UnavailableRuntimeGate,
) -> AdminPresenceSummaryResponse:
    return await usage_api.get_admin_usage_presence(
        principal=principal(),
        monitor=monitor,
        runtime_gate=runtime_gate,
        db=object(),
    )


async def test_admin_presence_requires_personal_runtime_and_keeps_client_groups(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(usage_api, "PlatformPresenceDirectoryService", FakeDirectoryService)
    response = await invoke_presence(
        PlatformPresenceMonitor(
            FakePresenceStore(),
            window_seconds=120,
            cache_ttl_seconds=0,
        ),
        FakeRuntimeGate(frozenset({7, 9})),
    )

    assert response.model_dump(mode="json") == {
        "web_online": {
            "count": 1,
            "users": ["张三（10001）"],
        },
        "desktop_online": {
            "count": 1,
            "users": ["张三（10001）"],
        },
        "daily_logins": {
            "count": 1,
            "users": ["王五（10003）"],
        },
        "window_seconds": 120,
        "daily_login_since": "2026-10-13T00:00:00Z",
        "observed_at": "2026-10-13T08:30:00Z",
        "source": "platform-session-user-directory",
    }
    serialized = response.model_dump_json().lower()
    assert "session_id" not in serialized
    assert "user_id" not in serialized


async def test_admin_presence_maps_store_unavailable_to_service_unavailable(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(usage_api, "PlatformPresenceDirectoryService", FakeDirectoryService)
    with pytest.raises(HTTPException) as exc_info:
        await invoke_presence(
            PlatformPresenceMonitor(
                FakePresenceStore(unavailable=True),
                window_seconds=120,
                cache_ttl_seconds=0,
            ),
            FakeRuntimeGate(frozenset({7})),
        )

    assert exc_info.value.status_code == 503
    assert exc_info.value.detail["code"] == "session_store_unavailable"


async def test_admin_presence_maps_runtime_status_unavailable_to_kubernetes_error(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(usage_api, "PlatformPresenceDirectoryService", FakeDirectoryService)
    with pytest.raises(HTTPException) as exc_info:
        await invoke_presence(
            PlatformPresenceMonitor(
                FakePresenceStore(),
                window_seconds=120,
                cache_ttl_seconds=0,
            ),
            UnavailableRuntimeGate(),
        )

    assert exc_info.value.status_code == 502
    assert exc_info.value.detail == "Kubernetes resource data is unavailable"
