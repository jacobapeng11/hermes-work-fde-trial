from __future__ import annotations

from typing import Any

from sqlalchemy import Insert, Select, Update, insert, select, update

from app.models import CloudHermesProfile, HermesRuntimeBinding, RuntimeRun


class RuntimeWorkspaceRepository:
    @staticmethod
    def initial_enterprise_knowledge_config() -> dict[str, Any]:
        """Default personal-agent enterprise knowledge access for new users."""
        return {
            "knowledge": {
                "enterprise": {
                    "enabled": True,
                    "mode": "full",
                }
            }
        }

    @staticmethod
    def default_profile_statement(*, org_id: int, user_id: int) -> Select:
        return select(CloudHermesProfile).where(
            CloudHermesProfile.org_id == org_id,
            CloudHermesProfile.user_id == user_id,
            CloudHermesProfile.profile_type == "default_chat_agent",
            CloudHermesProfile.status == "active",
        )

    @staticmethod
    def create_default_profile_statement(
        *,
        org_id: int,
        user_id: int,
        name: str = "默认数字人",
    ) -> Insert:
        return insert(CloudHermesProfile).values(
            org_id=org_id,
            user_id=user_id,
            profile_type="default_chat_agent",
            name=name,
            status="active",
            profile_config={
                "default_mcp_bootstrap": {"status": "pending"},
                **RuntimeWorkspaceRepository.initial_enterprise_knowledge_config(),
            },
        )

    @staticmethod
    def binding_by_workspace_statement(*, runtime_workspace_id: int) -> Select:
        return select(HermesRuntimeBinding).where(
            HermesRuntimeBinding.runtime_workspace_id == runtime_workspace_id
        )

    @staticmethod
    def binding_by_service_statement(
        *,
        namespace: str,
        service_name: str,
    ) -> Select:
        return select(HermesRuntimeBinding).where(
            HermesRuntimeBinding.k8s_namespace == namespace,
            HermesRuntimeBinding.knative_service == service_name,
        )

    @staticmethod
    def create_runtime_binding_statement(
        *,
        org_id: int,
        user_id: int,
        runtime_workspace_id: int,
        runtime_id: str,
        service_name: str,
        namespace: str,
        image: str,
        image_digest: str | None = None,
        api_key_encrypted: str,
        runtime_root_pvc_name: str,
        runtime_root_host_path: str,
        hermes_home_sub_path: str,
        workspace_sub_path: str,
        container_mounts: dict[str, str],
    ) -> Insert:
        values: dict[str, Any] = {
            "org_id": org_id,
            "user_id": user_id,
            "runtime_workspace_id": runtime_workspace_id,
            "provider": "knative",
            "runtime_id": runtime_id,
            "config_generation": 1,
            "runtime_config_source": "pvc",
            "image": image,
            "image_digest": image_digest,
            "k8s_namespace": namespace,
            "knative_service": service_name,
            "runtime_root_pvc": runtime_root_pvc_name,
            "runtime_root_host_path": runtime_root_host_path,
            "hermes_home_sub_path": hermes_home_sub_path,
            "workspace_sub_path": workspace_sub_path,
            "hermes_home_volume_name": "hermes-home",
            "workspace_volume_name": "workspace",
            "hermes_home_mount_path": container_mounts["hermes_home"],
            "workspace_mount_path": container_mounts["workspace"],
            "api_key_encrypted": api_key_encrypted,
            "status": "starting",
            "pending_reload": True,
        }
        return insert(HermesRuntimeBinding).values(**values)

    @staticmethod
    def update_runtime_binding_status_statement(
        *,
        runtime_workspace_id: int,
        values: dict[str, Any],
    ) -> Update:
        allowed = {
            "knative_status_url",
            "route_host",
            "endpoint_base_url",
            "endpoint_host_header",
            "status",
            "last_error",
            "pending_reload",
        }
        update_values = {key: value for key, value in values.items() if key in allowed}
        return (
            update(HermesRuntimeBinding)
            .where(HermesRuntimeBinding.runtime_workspace_id == runtime_workspace_id)
            .values(**update_values)
        )


class RuntimeRunRepository:
    ACTIVE_STATUSES = ("pending", "running")

    @classmethod
    def active_run_statement(
        cls,
        *,
        runtime_workspace_id: int,
        runtime_run_id: str,
    ) -> Select:
        return select(RuntimeRun).where(
            RuntimeRun.runtime_workspace_id == runtime_workspace_id,
            RuntimeRun.runtime_run_id == runtime_run_id,
            RuntimeRun.status.in_(cls.ACTIVE_STATUSES),
        )
