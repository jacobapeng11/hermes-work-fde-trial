"""Build the production Runtime dependency graph for every Backend surface.

Future Backend-owned IM adapters must consume ``dashboard_adapter`` and
``admission_service`` from this graph; IM transport does not belong in Hermes.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from sqlalchemy.ext.asyncio import AsyncSession

from app.config import settings
from app.database import AsyncSessionLocal
from app.hermes_runtime.activation import RuntimeActivator
from app.hermes_runtime.constants import HERMES_HOME, HERMES_WORKSPACE
from app.hermes_runtime.dashboard_protocol import HermesDashboardProtocolAdapter
from app.hermes_runtime.protocol import shared_runtime_http_client
from app.hermes_runtime.providers.knative import (
    KnativeRuntimeProvider,
    KubectlKnativeClient,
)
from app.services.runtime_activity import SqlAlchemyRuntimeActivityStore
from app.services.runtime_admission import RuntimeAdmissionService
from app.services.default_mcp_bootstrap import DefaultMcpBootstrapService
from app.services.personal_config_bundle import (
    PersonalRuntimeInitialConfigPreparer,
)
from app.services.runtime_reconcile import RuntimeTopologyReconciler
from app.services.runtime_workspace import (
    LocalRuntimeDirectoryProvisioner,
    RuntimeBindingReadyService,
    RuntimeKServiceWarmupRunner,
    RuntimeWorkspaceService,
    SqlAlchemyRuntimeBindingStatusStore,
    SqlAlchemyRuntimeWorkspaceStore,
)
from app.services.runtime_image_config import shared_runtime_image_config_store


class _SharedRuntimeHttpClient:
    """Delegate every request to the process-wide bounded connection pool."""

    async def get(self, url: str, **kwargs: Any) -> Any:
        return await (await shared_runtime_http_client()).get(url, **kwargs)

    async def head(self, url: str, **kwargs: Any) -> Any:
        return await (await shared_runtime_http_client()).head(url, **kwargs)

    async def post(self, url: str, **kwargs: Any) -> Any:
        return await (await shared_runtime_http_client()).post(url, **kwargs)

    async def patch(self, url: str, **kwargs: Any) -> Any:
        return await (await shared_runtime_http_client()).patch(url, **kwargs)

    async def delete(self, url: str, **kwargs: Any) -> Any:
        return await (await shared_runtime_http_client()).delete(url, **kwargs)


@dataclass(frozen=True)
class RuntimeProtocolAdapters:
    activator: RuntimeActivator
    dashboard_adapter: HermesDashboardProtocolAdapter


@dataclass(frozen=True)
class RuntimeInfrastructure:
    workspace_store: SqlAlchemyRuntimeWorkspaceStore
    workspace_service: RuntimeWorkspaceService
    admission_service: RuntimeAdmissionService
    activity_store: SqlAlchemyRuntimeActivityStore
    activator: RuntimeActivator
    dashboard_adapter: HermesDashboardProtocolAdapter
    ready_service: RuntimeBindingReadyService
    topology_reconciler: RuntimeTopologyReconciler
    warmup_runner: RuntimeKServiceWarmupRunner
    default_mcp_bootstrapper: DefaultMcpBootstrapService


def build_runtime_protocol_adapters() -> RuntimeProtocolAdapters:
    client = _SharedRuntimeHttpClient()
    activator = RuntimeActivator(
        client=client,
        ready_timeout_seconds=settings.HERMES_RUNTIME_READY_TIMEOUT_SECONDS,
    )
    dashboard_adapter = HermesDashboardProtocolAdapter(
        session_workspace_root=HERMES_WORKSPACE,
    )
    return RuntimeProtocolAdapters(
        activator=activator,
        dashboard_adapter=dashboard_adapter,
    )


def build_runtime_infrastructure(db: AsyncSession) -> RuntimeInfrastructure:
    protocols = build_runtime_protocol_adapters()
    provider = KnativeRuntimeProvider()
    k8s_client = KubectlKnativeClient(kubeconfig=settings.KUBECONFIG or None)
    runtime_image_config = shared_runtime_image_config_store()
    workspace_store = SqlAlchemyRuntimeWorkspaceStore(
        db,
        namespace=settings.HERMES_RUNTIME_NAMESPACE,
        runtime_image_config=runtime_image_config,
        ingress_base_url=settings.HERMES_RUNTIME_INGRESS_BASE_URL,
        hermes_home_mount_path=HERMES_HOME,
        workspace_mount_path=HERMES_WORKSPACE,
    )
    workspace_service = RuntimeWorkspaceService(workspace_store)
    admission_service = RuntimeAdmissionService(db)
    activity_store = SqlAlchemyRuntimeActivityStore(db)
    async def prepare_schema_startup(spec, ingress_base_url: str):
        from app.services.runtime_fresh_init import (
            RuntimeFreshInitAuthoringStore,
            RuntimeFreshInitProvisioner,
        )

        return await RuntimeFreshInitProvisioner(
            kubernetes=k8s_client,
            provider=provider,
            authoring_store=RuntimeFreshInitAuthoringStore(AsyncSessionLocal),
            timeout_seconds=settings.HERMES_RUNTIME_READY_TIMEOUT_SECONDS,
        ).prepare(spec, ingress_base_url)

    async def complete_fresh_bootstrap(spec) -> None:
        if not spec.bootstrap_intent_id:
            raise RuntimeError("fresh bootstrap intent is unavailable")
        from app.services.runtime_fresh_init import RuntimeFreshInitAuthoringStore

        await RuntimeFreshInitAuthoringStore(AsyncSessionLocal).mark_completed(
            intent_id=spec.bootstrap_intent_id
        )

    ready_service = RuntimeBindingReadyService(
        k8s_client=k8s_client,
        binding_store=SqlAlchemyRuntimeBindingStatusStore(db),
        protocol_adapter=protocols.dashboard_adapter,
        provider=provider,
        ready_timeout_seconds=settings.HERMES_RUNTIME_READY_TIMEOUT_SECONDS,
        startup_reconciler=None,
        schema_startup_preparer=prepare_schema_startup,
        bootstrap_completion=complete_fresh_bootstrap,
    )
    topology_reconciler = RuntimeTopologyReconciler(
        admission_service=admission_service,
        activity_store=activity_store,
        k8s_client=k8s_client,
        ready_service=ready_service,
        directory_provisioner=LocalRuntimeDirectoryProvisioner(),
        config_preparer=PersonalRuntimeInitialConfigPreparer(db),
        namespace=settings.HERMES_RUNTIME_NAMESPACE,
        service_account=settings.HERMES_RUNTIME_SERVICE_ACCOUNT,
        ingress_base_url=settings.HERMES_RUNTIME_INGRESS_BASE_URL,
        runtime_node_name=settings.HERMES_RUNTIME_NODE_NAME,
        drain_timeout_seconds=settings.HERMES_RUNTIME_LONG_REQUEST_TIMEOUT_SECONDS,
        platform_api_base_url=settings.platform_api_base_url,
        enterprise_knowledge_host_path=(settings.ENTERPRISE_KNOWLEDGE_HOST_PATH or None),
    )
    default_mcp_bootstrapper = DefaultMcpBootstrapService(
        session=db,
    )
    warmup_runner = RuntimeKServiceWarmupRunner(
        binding_store=workspace_store,
        topology_reconciler=topology_reconciler,
        admission_service=admission_service,
        ready_hook=default_mcp_bootstrapper,
    )
    return RuntimeInfrastructure(
        workspace_store=workspace_store,
        workspace_service=workspace_service,
        admission_service=admission_service,
        activity_store=activity_store,
        activator=protocols.activator,
        dashboard_adapter=protocols.dashboard_adapter,
        ready_service=ready_service,
        topology_reconciler=topology_reconciler,
        warmup_runner=warmup_runner,
        default_mcp_bootstrapper=default_mcp_bootstrapper,
    )
