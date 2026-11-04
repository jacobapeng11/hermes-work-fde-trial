from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import Any, Protocol

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models import CloudHermesProfile, HermesRuntimeBinding, User
from app.services.runtime_workspace import Principal


logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class RuntimeStartupContext:
    runtime_workspace_id: int | None
    context: str


class RuntimePodLister(Protocol):
    async def list_kservice_pods(
        self,
        *,
        namespace: str,
        service_name: str,
    ) -> list[dict[str, Any]]:
        ...


def _pod_is_running_and_ready(pod: dict[str, Any]) -> bool:
    status = pod.get("status")
    if not isinstance(status, dict) or status.get("phase") != "Running":
        return False
    conditions = status.get("conditions")
    if not isinstance(conditions, list):
        return False
    return any(
        isinstance(condition, dict)
        and condition.get("type") == "Ready"
        and condition.get("status") == "True"
        for condition in conditions
    )


class RuntimeStartupContextService:
    def __init__(
        self,
        session: AsyncSession,
        *,
        pod_lister: RuntimePodLister,
        namespace: str,
    ) -> None:
        self.session = session
        self.pod_lister = pod_lister
        self.namespace = namespace

    async def resolve(self, principal: Principal) -> RuntimeStartupContext:
        try:
            user = await self.session.scalar(
                select(User).where(
                    User.id == principal.user_id,
                    User.org_id == principal.org_id,
                )
            )
            if user is None:
                return RuntimeStartupContext(None, "none")

            profile = await self.session.scalar(
                select(CloudHermesProfile)
                .where(
                    CloudHermesProfile.org_id == principal.org_id,
                    CloudHermesProfile.user_id == principal.user_id,
                    CloudHermesProfile.profile_type == "default_chat_agent",
                    CloudHermesProfile.status == "active",
                )
                .order_by(CloudHermesProfile.id)
            )
            is_first_login = int(user.login_count or 0) == 1
            if profile is None:
                context = "first_allocation" if is_first_login else "none"
                return RuntimeStartupContext(None, context)

            binding = await self.session.scalar(
                select(HermesRuntimeBinding).where(
                    HermesRuntimeBinding.runtime_workspace_id == int(profile.id)
                )
            )
            if binding is None:
                context = "first_allocation" if is_first_login else "none"
                return RuntimeStartupContext(int(profile.id), context)

            pods = await self.pod_lister.list_kservice_pods(
                namespace=binding.k8s_namespace or self.namespace,
                service_name=binding.knative_service,
            )
            if any(_pod_is_running_and_ready(pod) for pod in pods):
                context = "none"
            elif is_first_login:
                context = "first_allocation"
            else:
                context = "environment_recovery"
            return RuntimeStartupContext(int(profile.id), context)
        except Exception:
            logger.warning("Runtime startup context lookup unavailable", exc_info=True)
            return RuntimeStartupContext(None, "none")
