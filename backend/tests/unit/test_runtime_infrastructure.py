from types import SimpleNamespace

import app.services.runtime_infrastructure as runtime_infrastructure
from app.config import settings
from app.services.runtime_image_config import StaticRuntimeImageConfigReader


def test_runtime_infrastructure_uses_one_unified_adapter_graph(monkeypatch) -> None:
    constructed: list[tuple[str, object]] = []

    class FakeProvider:
        def __init__(self) -> None:
            constructed.append(("provider", self))

    class FakeKubectlClient:
        def __init__(self, *, kubeconfig: str | None = None) -> None:
            self.kubeconfig = kubeconfig
            constructed.append(("kubectl", self))

    monkeypatch.setattr(runtime_infrastructure, "KnativeRuntimeProvider", FakeProvider)
    monkeypatch.setattr(runtime_infrastructure, "KubectlKnativeClient", FakeKubectlClient)
    runtime_image_config = StaticRuntimeImageConfigReader(
        image="kind.local/hermes-agent:v0.19.1-current",
    )
    monkeypatch.setattr(
        runtime_infrastructure,
        "shared_runtime_image_config_store",
        lambda: runtime_image_config,
    )

    db_session = SimpleNamespace()
    infrastructure = runtime_infrastructure.build_runtime_infrastructure(
        db_session  # type: ignore[arg-type]
    )

    assert infrastructure.ready_service.protocol_adapter is infrastructure.dashboard_adapter
    assert not hasattr(infrastructure, "gateway_adapter")
    assert infrastructure.ready_service.provider is constructed[0][1]
    assert infrastructure.topology_reconciler.ready_service is infrastructure.ready_service
    assert infrastructure.topology_reconciler.k8s_client is constructed[1][1]
    assert infrastructure.warmup_runner.topology_reconciler is infrastructure.topology_reconciler
    assert infrastructure.warmup_runner.binding_store is infrastructure.workspace_store
    assert infrastructure.workspace_service.store is infrastructure.workspace_store
    assert infrastructure.admission_service.session is db_session
    assert infrastructure.activity_store.session is db_session
    assert infrastructure.workspace_store.namespace == settings.HERMES_RUNTIME_NAMESPACE
    assert infrastructure.workspace_store.runtime_image_config is runtime_image_config
    assert (
        infrastructure.workspace_store.ingress_base_url
        == settings.HERMES_RUNTIME_INGRESS_BASE_URL
    )
    assert (
        infrastructure.ready_service.ready_timeout_seconds
        == settings.HERMES_RUNTIME_READY_TIMEOUT_SECONDS
    )
    assert [kind for kind, _instance in constructed] == ["provider", "kubectl"]


def test_runtime_protocol_adapters_are_dashboard_only() -> None:
    protocols = runtime_infrastructure.build_runtime_protocol_adapters()

    assert not hasattr(protocols, "gateway_adapter")
    assert not hasattr(protocols.dashboard_adapter, "http_adapter")
    assert protocols.dashboard_adapter.session_workspace_root is not None
