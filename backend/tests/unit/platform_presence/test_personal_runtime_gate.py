from __future__ import annotations

from app.services.admin_auth import AdminPrincipal
from app.services.admin_kubernetes_resources import (
    AdminKubernetesResourceService,
    AdminRuntimeKubernetesScope,
)


def scope(
    *,
    user_id: int,
    runtime_kind: str = "personal",
) -> AdminRuntimeKubernetesScope:
    runtime_workspace_id = user_id
    application_agent_id = runtime_workspace_id if runtime_kind == "application" else None
    return AdminRuntimeKubernetesScope(
        org_id=1,
        user_id=user_id,
        runtime_workspace_id=runtime_workspace_id,
        namespace="hermes-runtimes",
        kservice_name=f"runtime-{runtime_kind}-{user_id}",
        binding_status="ready",
        active_run_count=0,
        runtime_kind=runtime_kind,
        runtime_id=f"runtime-{runtime_kind}-{user_id}",
        application_agent_id=application_agent_id,
    )


def kservice(name: str) -> dict:
    revision = f"{name}-00001"
    return {
        "metadata": {"name": name, "namespace": "hermes-runtimes"},
        "status": {
            "latestCreatedRevisionName": revision,
            "latestReadyRevisionName": revision,
            "traffic": [{"revisionName": revision, "percent": 100}],
            "conditions": [{"type": "Ready", "status": "True"}],
        },
    }


def kpa(name: str, desired: int, actual: int) -> dict:
    return {
        "metadata": {"name": f"{name}-00001"},
        "status": {
            "desiredScale": desired,
            "actualScale": actual,
            "conditions": [{"type": "Active", "status": "True" if desired else "False"}],
        },
    }


def pod(name: str) -> dict:
    return {
        "metadata": {
            "name": f"{name}-pod",
            "labels": {
                "serving.knative.dev/revision": f"{name}-00001",
                "hermes.platform/runtime-id": name,
            },
        },
        "spec": {
            "containers": [
                {"name": "user-container"},
                {"name": "queue-proxy"},
            ]
        },
        "status": {
            "phase": "Running",
            "containerStatuses": [
                {"name": "user-container", "ready": True, "restartCount": 0},
                {"name": "queue-proxy", "ready": True, "restartCount": 0},
            ],
        },
    }


class FakeScopeStore:
    async def list_runtime_scopes(self, *, org_id: int) -> list[AdminRuntimeKubernetesScope]:
        assert org_id == 1
        return [
            scope(user_id=7),
            scope(user_id=8),
            scope(user_id=9, runtime_kind="application"),
        ]


class FakeKubernetesClient:
    async def get_json(
        self,
        resource: str,
        *,
        namespace: str | None = None,
        name: str | None = None,
        labels: dict[str, str] | None = None,
    ) -> dict:
        if resource == "services.serving.knative.dev":
            names = ["runtime-personal-7", "runtime-personal-8", "runtime-application-9"]
            return {"items": [kservice(item) for item in names]}
        if resource == "podautoscalers.autoscaling.internal.knative.dev":
            return {
                "items": [
                    kpa("runtime-personal-7", 1, 1),
                    kpa("runtime-personal-8", 0, 0),
                    kpa("runtime-application-9", 1, 1),
                ]
            }
        if resource == "pods":
            if labels is None:
                return {
                    "items": [
                        pod("runtime-personal-7"),
                        pod("runtime-application-9"),
                    ]
                }
            if labels and labels.get("hermes.platform/runtime-id") == "runtime-personal-7":
                return {"items": [pod("runtime-personal-7")]}
            if labels and labels.get("hermes.platform/runtime-id") == "runtime-application-9":
                return {"items": [pod("runtime-application-9")]}
            return {"items": []}
        return {"items": []}


async def test_presence_gate_counts_only_ready_personal_runtimes() -> None:
    service = AdminKubernetesResourceService(
        scope_store=FakeScopeStore(),  # type: ignore[arg-type]
        client=FakeKubernetesClient(),  # type: ignore[arg-type]
    )

    ready_user_ids = await service.personal_runtime_ready_user_ids(
        AdminPrincipal(org_id=1, user_id=1, roles=("admin",))
    )

    assert ready_user_ids == frozenset({7})
