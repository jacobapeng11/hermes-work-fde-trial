import os
import stat
from dataclasses import replace
from types import SimpleNamespace

import pytest
from sqlalchemy.ext.asyncio import AsyncSession

from app.services.runtime_workspace import (
    Principal,
    RuntimeBindingEnsureResult,
    RuntimeBindingReadyService,
    RuntimeBindingStatusPatch,
    RuntimeConfigPreflightError,
    RuntimeKServiceWarmupRunner,
    RuntimeKeepaliveError,
    LocalRuntimeDirectoryProvisioner,
    RuntimeApiKeyCodec,
    RuntimeWorkspaceRecord,
    RuntimeWorkspaceService,
    SqlAlchemyRuntimeWorkspaceStore,
    SqlAlchemyRuntimeBindingStatusStore,
    runtime_root_host_path,
)
from app.hermes_runtime.providers.knative import KnativeRuntimeSpec
from app.services.runtime_image_config import StaticRuntimeImageConfigReader
from app.services.runtime_platform_control import (
    RuntimePlatformControlKeyMaterial,
    RuntimePlatformControlKeyProvision,
    runtime_pvc_storage_identity_hash,
)


CURRENT_RUNTIME_IMAGE = "kind.local/hermes-agent:v0.19.1-cc4cab2f59-32285038ed-df7c7313-candidate"
OLD_RUNTIME_IMAGE = "kind.local/hermes-agent:v0.19.0-f4df260f2-expert-kanban-guard"
PVC_UID = "00000000-0000-0000-0000-000000000123"
PVC_IDENTITY = runtime_pvc_storage_identity_hash(
    namespace="hermes-runtimes",
    pvc_uid=PVC_UID,
)


def _control_plan(plan):
    return replace(
        plan,
        binding_id="123",
        platform_control_secret_name=(f"{plan.service_name}-platform-control"),
        platform_control_key_version=1,
    )


def _control_material(binding_id: int = 123) -> RuntimePlatformControlKeyMaterial:
    return RuntimePlatformControlKeyMaterial(
        binding_type="personal_runtime",
        binding_id=binding_id,
        runtime_id="rt-123",
        key_version=1,
        root_key=b"k" * 32,
    )


class FakePlatformControlKeyStore:
    def __init__(self, *, binding_id: int = 123, created: bool = False) -> None:
        self.material = _control_material(binding_id)
        self.created = created

    async def ensure_active_key(self, **_kwargs):
        return RuntimePlatformControlKeyProvision(
            material=self.material,
            created=self.created,
        )


class FakeStore:
    def __init__(
        self,
        existing: RuntimeWorkspaceRecord | None = None,
        *,
        reconcile_required: bool = True,
    ) -> None:
        self.existing = existing
        self.reconcile_required = reconcile_required
        self.created: list[dict] = []
        self.binding_ids: list[int] = []

    async def get_default_profile(self, *, org_id: int, user_id: int):
        if self.existing and self.existing.org_id == org_id and self.existing.user_id == user_id:
            return self.existing
        return None

    async def create_default_profile(self, *, org_id: int, user_id: int, name: str):
        self.created.append({"org_id": org_id, "user_id": user_id, "name": name})
        self.existing = RuntimeWorkspaceRecord(
            runtime_workspace_id=123,
            cloud_profile_id=123,
            org_id=org_id,
            user_id=user_id,
            user_name="测试用户",
            user_code=f"employee-{user_id}",
            name=name,
            status="active",
        )
        return self.existing

    async def ensure_binding(self, record: RuntimeWorkspaceRecord):
        self.binding_ids.append(record.runtime_workspace_id)
        return RuntimeBindingEnsureResult(
            plan=RuntimeWorkspaceService.build_default_binding_plan(record),
            api_server_key="runtime-api-key",
            created=False,
            reconcile_required=self.reconcile_required,
        )

    async def get_runtime_endpoint(self, record: RuntimeWorkspaceRecord):
        from app.hermes_runtime.schemas import RuntimeEndpoint

        return RuntimeEndpoint(
            runtime_workspace_id=record.runtime_workspace_id,
            runtime_id=f"rt-{record.runtime_workspace_id}",
            endpoint_base_url="http://runtime.example",
            endpoint_host_header="runtime.example",
            api_key="runtime-api-key",
        )


class FakeWarmupRunner:
    def __init__(self) -> None:
        self.records: list[RuntimeWorkspaceRecord] = []

    async def warmup(self, record: RuntimeWorkspaceRecord) -> RuntimeWorkspaceRecord:
        self.records.append(record)
        return RuntimeWorkspaceRecord(
            runtime_workspace_id=record.runtime_workspace_id,
            cloud_profile_id=record.cloud_profile_id,
            org_id=record.org_id,
            user_id=record.user_id,
            user_name=record.user_name,
            user_code=record.user_code,
            name=record.name,
            status=record.status,
            runtime_status="ready",
        )


class OpenWarmupAdmission:
    def __init__(self) -> None:
        self.lock_requests: list[bool] = []

    async def require_new_work_allowed(self, *, lock: bool = False) -> None:
        self.lock_requests.append(lock)


class FrozenWarmupAdmission:
    async def require_new_work_allowed(self, *, lock: bool = False) -> None:
        assert lock is True
        raise RuntimeError("MAINTENANCE_ADMISSION_FROZEN")


class FakeKnativeClient:
    def __init__(
        self,
        kservice: dict | list[dict] | None = None,
        *,
        events: list[str] | None = None,
    ) -> None:
        if isinstance(kservice, list):
            self.kservices = list(kservice)
            self.kservice = self.kservices[-1] if self.kservices else {}
        else:
            self.kservices = []
            self.kservice = kservice or {}
        self.applied: list[dict] = []
        self.requested: list[dict] = []
        self.requested_pvcs: list[dict] = []
        self.deleted_services: list[dict[str, str]] = []
        self.deleted_kservices: list[dict[str, str]] = []
        self.events = events if events is not None else []

    async def apply(self, manifest: dict) -> None:
        self.applied.append(manifest)
        name = manifest["metadata"]["name"]
        if manifest.get("apiVersion") == "serving.knative.dev/v1":
            self.events.append(f"apply:KService/{name}")
        else:
            self.events.append(f"apply:{manifest['kind']}/{name}")

    async def get_kservice(self, *, namespace: str, name: str) -> dict:
        self.requested.append({"namespace": namespace, "name": name})
        self.events.append(f"get:KService/{name}")
        if self.kservices:
            return self.kservices.pop(0)
        return self.kservice

    async def get_persistent_volume_claim(self, *, namespace: str, name: str) -> dict:
        self.requested_pvcs.append({"namespace": namespace, "name": name})
        self.events.append(f"get:PVC/{name}")
        return {
            "metadata": {
                "namespace": namespace,
                "name": name,
                "uid": PVC_UID,
            }
        }

    async def delete_service(self, *, namespace: str, name: str) -> None:
        self.deleted_services.append({"namespace": namespace, "name": name})
        self.events.append(f"delete:Service/{name}")

    async def delete_kservice(self, *, namespace: str, name: str) -> None:
        self.deleted_kservices.append({"namespace": namespace, "name": name})
        self.events.append(f"delete:KService/{name}")

    async def list_kservice_pods(self, *, namespace: str, service_name: str) -> list[dict]:
        self.events.append(f"pods:KService/{service_name}:0")
        return []


class FakeBindingStatusStore:
    def __init__(self, events: list[str] | None = None) -> None:
        self.patches: list[RuntimeBindingStatusPatch] = []
        self.events = events
        self.pvc_identities: list[tuple[int, str, str]] = []

    async def update_binding_status(self, patch: RuntimeBindingStatusPatch) -> None:
        self.patches.append(patch)
        if self.events is not None:
            self.events.append(f"binding:{patch.status}:{patch.endpoint_base_url or ''}")

    async def bind_pvc_identity(
        self,
        *,
        runtime_workspace_id: int,
        pvc_uid: str,
        pvc_storage_identity_hash: str,
    ) -> None:
        self.pvc_identities.append((runtime_workspace_id, pvc_uid, pvc_storage_identity_hash))
        if self.events is not None:
            self.events.append("binding:pvc-identity")


class FakeTopologyReconciler:
    def __init__(self, *, exists: bool = True) -> None:
        self.exists = exists
        self.calls: list[tuple[RuntimeWorkspaceRecord, RuntimeBindingEnsureResult]] = []

    async def kservice_exists(self, _service_name: str) -> bool:
        return self.exists

    async def reconcile(self, record, binding):
        self.calls.append((record, binding))
        return replace(record, runtime_status="ready", pending_reload=False)


class FakeRuntimeReadyHook:
    def __init__(self) -> None:
        self.records: list[RuntimeWorkspaceRecord] = []

    async def ensure_for_runtime(self, record: RuntimeWorkspaceRecord) -> None:
        self.records.append(record)


class FakeProtocolAdapter:
    def __init__(
        self,
        *,
        fail_health: bool = False,
        events: list[str] | None = None,
    ) -> None:
        self.fail_health = fail_health
        self.events = events
        self.health_endpoint = None
        self.capabilities_endpoint = None

    async def health(self, endpoint):
        self.health_endpoint = endpoint
        if self.events is not None:
            self.events.append("dashboard:health")
        if self.fail_health:
            raise RuntimeError("health failed")
        return {"status": "ok"}

    async def capabilities(self, endpoint):
        self.capabilities_endpoint = endpoint
        if self.events is not None:
            self.events.append("dashboard:capabilities")
        return {"tools": []}


class FakeAsyncSession:
    def __init__(self) -> None:
        self.statements: list = []
        self.commits = 0

    async def execute(self, statement):
        self.statements.append(statement)

    async def commit(self):
        self.commits += 1


class FakeScalarResult:
    def __init__(self, value) -> None:
        self.value = value

    def scalar_one_or_none(self):
        return self.value

    def scalar_one(self):
        return self.value

    def scalars(self):
        return self

    def first(self):
        return self.value


class SequencedAsyncSession:
    def __init__(self, *values) -> None:
        self.results = [FakeScalarResult(value) for value in values]
        self.statements: list = []
        self.commits = 0

    async def execute(self, statement):
        self.statements.append(statement)
        if self.results:
            return self.results.pop(0)
        return FakeScalarResult(None)

    async def commit(self):
        self.commits += 1


class SqlAlchemySequencedAsyncSession(AsyncSession):
    """Minimal AsyncSession-shaped double for release-contract selection."""

    def __init__(self, *values) -> None:
        self.results = [FakeScalarResult(value) for value in values]
        self.statements: list = []
        self.commits = 0

    async def execute(self, statement):
        self.statements.append(statement)
        if self.results:
            return self.results.pop(0)
        return FakeScalarResult(None)

    async def commit(self):
        self.commits += 1


async def test_ensure_default_reuses_existing_platform_owner_profile() -> None:
    record = RuntimeWorkspaceRecord(
        runtime_workspace_id=10,
        cloud_profile_id=10,
        org_id=7,
        user_id=9,
        user_name="测试用户",
        user_code="employee-9",
        name="Existing",
        status="active",
    )
    store = FakeStore(existing=record)

    result = await RuntimeWorkspaceService(store).ensure_default_for_user(
        Principal(user_id=9, org_id=7)
    )

    assert result == record
    assert store.created == []
    assert store.binding_ids == []


def test_each_platform_user_gets_a_distinct_runtime_plan() -> None:
    first = RuntimeWorkspaceService.build_default_binding_plan(
        RuntimeWorkspaceRecord(101, 101, 1, 11, "用户 A", "employee-11", "A", "active"),
    )
    second = RuntimeWorkspaceService.build_default_binding_plan(
        RuntimeWorkspaceRecord(102, 102, 1, 12, "用户 B", "employee-12", "B", "active")
    )

    assert first.runtime_workspace_id != second.runtime_workspace_id
    assert first.runtime_id != second.runtime_id
    assert first.service_name != second.service_name
    assert first.runtime_root_pvc_name != second.runtime_root_pvc_name

    first_spec = RuntimeBindingReadyService.build_knative_spec(
        plan=first,
        namespace="hermes-runtimes",
        image="hermes-agent:test",
        service_account="hermes-runtime-workload",
        startup_mode="steady_state",
    )
    second_spec = RuntimeBindingReadyService.build_knative_spec(
        plan=second,
        namespace="hermes-runtimes",
        image="hermes-agent:test",
        service_account="hermes-runtime-workload",
        startup_mode="steady_state",
    )
    assert first_spec.runtime_root_host_path != second_spec.runtime_root_host_path
    assert first_spec.image == "hermes-agent:test"


async def test_ensure_default_creates_profile_for_platform_user_id() -> None:
    store = FakeStore()

    result = await RuntimeWorkspaceService(store).ensure_default_for_user(
        Principal(user_id=9, org_id=7)
    )

    assert result.runtime_workspace_id == 123
    assert store.created == [{"org_id": 7, "user_id": 9, "name": "默认数字人"}]
    assert store.binding_ids == []


async def test_warmup_default_invokes_runner_after_profile_and_binding() -> None:
    store = FakeStore()
    runner = FakeWarmupRunner()

    result = await RuntimeWorkspaceService(store).warmup_default_for_user(
        Principal(user_id=9, org_id=7),
        runner,
    )

    assert result.runtime_status == "ready"
    assert store.binding_ids == []
    assert [record.runtime_workspace_id for record in runner.records] == [123]


async def test_keepalive_default_checks_ready_binding_without_runtime_network() -> None:
    store = FakeStore(
        RuntimeWorkspaceRecord(
            123,
            123,
            7,
            9,
            "测试用户",
            "employee-7",
            "默认数字人",
            "active",
            runtime_status="ready",
        )
    )

    result = await RuntimeWorkspaceService(store).keepalive_default_for_user(
        Principal(user_id=9, org_id=7)
    )

    assert result.runtime_workspace_id == 123
    assert store.binding_ids == []


@pytest.mark.parametrize("runtime_status", [None, "starting", "failed", "force_stopped"])
async def test_keepalive_default_rejects_non_ready_binding(
    runtime_status: str | None,
) -> None:
    store = FakeStore(
        RuntimeWorkspaceRecord(
            123,
            123,
            7,
            9,
            "测试用户",
            "employee-7",
            "默认数字人",
            "active",
            runtime_status=runtime_status,
        )
    )

    with pytest.raises(RuntimeKeepaliveError, match="runtime is not ready"):
        await RuntimeWorkspaceService(store).keepalive_default_for_user(
            Principal(user_id=9, org_id=7)
        )


async def test_kservice_warmup_runner_delegates_slow_path_to_topology_reconciler() -> None:
    store = FakeStore(
        RuntimeWorkspaceRecord(
            runtime_workspace_id=123,
            cloud_profile_id=123,
            org_id=7,
            user_id=9,
            user_name="测试用户",
            user_code="employee-7",
            name="默认数字人",
            status="active",
        )
    )
    topology_reconciler = FakeTopologyReconciler()
    runner = RuntimeKServiceWarmupRunner(
        binding_store=store,
        topology_reconciler=topology_reconciler,
        admission_service=OpenWarmupAdmission(),
    )

    result = await runner.warmup(store.existing)

    assert result.runtime_status == "ready"
    assert result.pending_reload is False
    assert store.binding_ids == [123]
    assert topology_reconciler.calls[0][0].runtime_workspace_id == 123
    assert topology_reconciler.calls[0][1].reconcile_required is True


async def test_kservice_warmup_checks_maintenance_before_binding_creation() -> None:
    store = FakeStore(_workspace_record())
    topology = FakeTopologyReconciler()
    runner = RuntimeKServiceWarmupRunner(
        binding_store=store,
        topology_reconciler=topology,
        admission_service=FrozenWarmupAdmission(),
    )

    with pytest.raises(RuntimeError, match="MAINTENANCE_ADMISSION_FROZEN"):
        await runner.warmup(store.existing)

    assert store.binding_ids == []
    assert topology.calls == []


async def test_kservice_warmup_runner_rejects_handed_off_binding_identity_mismatch() -> None:
    record = _workspace_record()
    store = FakeStore(record)
    binding = RuntimeBindingEnsureResult(
        plan=RuntimeWorkspaceService.build_default_binding_plan(
            replace(record, runtime_workspace_id=999, cloud_profile_id=999),
        ),
        api_server_key="runtime-key",
        created=True,
        reconcile_owned=True,
    )
    topology = FakeTopologyReconciler()

    with pytest.raises(RuntimeConfigPreflightError, match="identity does not match"):
        await RuntimeKServiceWarmupRunner(
            binding_store=store,
            topology_reconciler=topology,
            admission_service=OpenWarmupAdmission(),
        ).warmup_bound(record, binding)

    assert topology.calls == []


async def test_kservice_warmup_runner_bootstraps_default_mcp_only_after_ready() -> None:
    record = _workspace_record()
    store = FakeStore(record, reconcile_required=False)
    hook = FakeRuntimeReadyHook()

    class ForbiddenTopologyReconciler:
        async def kservice_exists(self, _service_name: str) -> bool:
            return True

        async def reconcile(self, *_args):
            raise AssertionError("ready binding must not enter slow reconcile")

    result = await RuntimeKServiceWarmupRunner(
        binding_store=store,
        topology_reconciler=ForbiddenTopologyReconciler(),
        admission_service=OpenWarmupAdmission(),
        ready_hook=hook,
    ).warmup(record)

    assert result.runtime_status == "ready"
    assert hook.records == [result]


async def test_ready_binding_rebuilds_when_kservice_is_missing() -> None:
    record = _workspace_record()
    store = FakeStore(record, reconcile_required=False)
    topology = FakeTopologyReconciler(exists=False)

    result = await RuntimeKServiceWarmupRunner(
        binding_store=store,
        topology_reconciler=topology,
        admission_service=OpenWarmupAdmission(),
    ).warmup(record)

    assert result.runtime_status == "ready"
    assert len(topology.calls) == 1
    assert topology.calls[0][1].reconcile_required is True


async def test_ready_current_binding_does_not_reconcile_kubernetes_or_disk() -> None:
    record = RuntimeWorkspaceRecord(
        runtime_workspace_id=123,
        cloud_profile_id=123,
        org_id=7,
        user_id=9,
        user_name="测试用户",
        user_code="employee-7",
        name="默认数字人",
        status="active",
        runtime_status="ready",
    )
    store = FakeStore(record, reconcile_required=False)

    class ForbiddenTopologyReconciler:
        async def kservice_exists(self, _service_name: str) -> bool:
            return True

        async def reconcile(self, *_args):
            raise AssertionError("ready binding must not enter slow reconcile")

    runner = RuntimeKServiceWarmupRunner(
        binding_store=store,
        topology_reconciler=ForbiddenTopologyReconciler(),
        admission_service=OpenWarmupAdmission(),
    )

    result = await runner.warmup(record)

    assert result.runtime_status == "ready"
    assert result.pending_reload is False
    assert store.binding_ids == [123]


async def test_local_runtime_directory_provisioner_creates_root_and_subpaths(tmp_path) -> None:
    root = tmp_path / "runtimes" / "7" / "123"
    spec = KnativeRuntimeSpec(
        org_id=7,
        user_id=9,
        user_name="测试用户",
        user_code="employee-7",
        runtime_workspace_id=123,
        runtime_id="rt-123",
        namespace="hermes-runtimes",
        service_name="hermes-runtime-123",
        image="test",
        service_account="hermes-runtime-workload",
        api_server_key_secret_name="key",
        runtime_root_pvc_name="root",
        hermes_home_sub_path="home",
        workspace_sub_path="workspace",
        runtime_root_pv_name="root-pv",
        runtime_root_host_path=str(root),
        runtime_node_name="demo",
        upstream_commit="cc4cab2f592e60a197e796506de9168f74baf3ea",
        state_schema=23,
        config_schema=33,
        startup_mode="steady_state",
        runtime_user_uid=os.getuid(),
        runtime_shared_gid=os.getgid(),
    )

    await LocalRuntimeDirectoryProvisioner().ensure_directory(spec)

    assert (root / "home").is_dir()
    assert (root / "workspace").is_dir()
    assert (root / "home" / "platform").is_dir()
    assert (root / "home" / "skills").is_dir()
    assert stat.S_IMODE((root / "home").stat().st_mode) == 0o2770
    assert stat.S_IMODE((root / "home" / "platform").stat().st_mode) == 0o2770
    assert (root / "home").stat().st_uid == os.getuid()
    assert (root / "home").stat().st_gid == os.getgid()
    assert (root / "home" / "platform").stat().st_gid == os.getgid()
    for relative in (
        "kanban",
        "kanban/workspaces",
        "kanban/logs",
        "kanban/boards",
    ):
        path = root / "home" / relative
        assert path.is_dir()
        assert path.stat().st_uid == os.getuid()
        assert path.stat().st_gid == os.getgid()
        assert stat.S_IMODE(path.stat().st_mode) == 0o2770


async def test_local_runtime_directory_provisioner_keeps_bootstrap_home_empty(tmp_path) -> None:
    """Bootstrap 模式（新 config schema）下 Backend 不得向 Hermes home 预写任何文件。

    fresh probe 的空盘合同只允许空 scaffold 目录；预写 USER.md/MEMORY.md 或
    .platform-memory.lock 会让全新 Runtime 在 FRESH_STORAGE_NOT_EMPTY 上 CrashLoop。
    """
    root = tmp_path / "runtimes" / "7" / "247"
    spec = KnativeRuntimeSpec(
        org_id=7,
        user_id=9,
        user_name="胡州",
        user_code="w22737",
        runtime_workspace_id=247,
        runtime_id="rt-247",
        namespace="hermes-runtimes",
        service_name="runtime-w22737",
        image="test",
        service_account="hermes-runtime-workload",
        api_server_key_secret_name="key",
        runtime_root_pvc_name="root",
        hermes_home_sub_path="home",
        workspace_sub_path="workspace",
        runtime_root_pv_name="root-pv",
        runtime_root_host_path=str(root),
        runtime_node_name="demo",
        upstream_commit="cc4cab2f592e60a197e796506de9168f74baf3ea",
        state_schema=23,
        config_schema=34,
        startup_mode="steady_state",
        runtime_user_uid=os.getuid(),
        runtime_shared_gid=os.getgid(),
    )

    await LocalRuntimeDirectoryProvisioner().ensure_directory(spec)

    scaffold_files = [
        path for path in (root / "home").rglob("*") if path.is_file()
    ]
    assert scaffold_files == []


async def test_local_runtime_directory_provisioner_repairs_only_kanban_metadata(
    tmp_path,
) -> None:
    root = tmp_path / "runtimes" / "7" / "126"
    home = root / "home"
    scratch = home / "kanban" / "workspaces"
    logs = home / "kanban" / "logs"
    task_file = scratch / "existing-task" / "result.txt"
    unrelated = home / "keep-me.txt"
    task_file.parent.mkdir(parents=True)
    logs.mkdir(parents=True)
    (home / "kanban" / "boards").mkdir()
    task_file.write_text("result", encoding="utf-8")
    unrelated.write_text("untouched", encoding="utf-8")
    for path in (home / "kanban", scratch, logs, home / "kanban" / "boards"):
        path.chmod(0o755)
    database = home / "kanban.db"
    database.write_bytes(b"existing-database")
    database.chmod(0o644)
    original_unrelated_mode = stat.S_IMODE(unrelated.stat().st_mode)
    spec = KnativeRuntimeSpec(
        org_id=7,
        user_id=9,
        user_name="测试用户",
        user_code="employee-7",
        runtime_workspace_id=126,
        runtime_id="rt-126",
        namespace="hermes-runtimes",
        service_name="hermes-runtime-126",
        image="test",
        service_account="hermes-runtime-workload",
        api_server_key_secret_name="key",
        runtime_root_pvc_name="root",
        hermes_home_sub_path="home",
        workspace_sub_path="workspace",
        runtime_root_pv_name="root-pv",
        runtime_root_host_path=str(root),
        runtime_node_name="demo",
        upstream_commit="cc4cab2f592e60a197e796506de9168f74baf3ea",
        state_schema=23,
        config_schema=33,
        startup_mode="steady_state",
        runtime_user_uid=os.getuid(),
        runtime_shared_gid=os.getgid(),
    )

    await LocalRuntimeDirectoryProvisioner().ensure_directory(spec)

    assert database.read_bytes() == b"existing-database"
    assert stat.S_IMODE(database.stat().st_mode) == 0o660
    assert task_file.read_text(encoding="utf-8") == "result"
    assert stat.S_IMODE(task_file.stat().st_mode) != 0o660
    assert unrelated.read_text(encoding="utf-8") == "untouched"
    assert stat.S_IMODE(unrelated.stat().st_mode) == original_unrelated_mode
    for path in (home / "kanban", scratch, logs, home / "kanban" / "boards"):
        assert stat.S_IMODE(path.stat().st_mode) == 0o2770


async def test_local_runtime_directory_provisioner_repairs_managed_memory_metadata(
    tmp_path,
) -> None:
    root = tmp_path / "runtimes" / "7" / "129"
    memories = root / "home" / "memories"
    memories.mkdir(parents=True)
    user_memory = memories / "USER.md"
    long_term_memory = memories / "MEMORY.md"
    user_memory.write_text("existing user memory", encoding="utf-8")
    long_term_memory.write_text("existing long-term memory", encoding="utf-8")
    user_memory.chmod(0o600)
    long_term_memory.chmod(0o600)
    spec = KnativeRuntimeSpec(
        org_id=7,
        user_id=9,
        user_name="测试用户",
        user_code="employee-7",
        runtime_workspace_id=129,
        runtime_id="rt-129",
        namespace="hermes-runtimes",
        service_name="hermes-runtime-129",
        image="test",
        service_account="hermes-runtime-workload",
        api_server_key_secret_name="key",
        runtime_root_pvc_name="root",
        hermes_home_sub_path="home",
        workspace_sub_path="workspace",
        runtime_root_pv_name="root-pv",
        runtime_root_host_path=str(root),
        runtime_node_name="demo",
        upstream_commit="cc4cab2f592e60a197e796506de9168f74baf3ea",
        state_schema=23,
        config_schema=33,
        startup_mode="steady_state",
        runtime_user_uid=os.getuid(),
        runtime_shared_gid=os.getgid(),
    )

    await LocalRuntimeDirectoryProvisioner().ensure_directory(spec)

    assert user_memory.read_text(encoding="utf-8") == "existing user memory"
    assert long_term_memory.read_text(encoding="utf-8") == "existing long-term memory"
    assert stat.S_IMODE(user_memory.stat().st_mode) == 0o660
    assert stat.S_IMODE(long_term_memory.stat().st_mode) == 0o660


async def test_local_runtime_directory_provisioner_rejects_kanban_symlink(
    tmp_path,
) -> None:
    root = tmp_path / "runtimes" / "7" / "127"
    home = root / "home"
    outside = tmp_path / "outside"
    home.mkdir(parents=True)
    outside.mkdir()
    (home / "kanban").symlink_to(outside, target_is_directory=True)
    spec = KnativeRuntimeSpec(
        org_id=7,
        user_id=9,
        user_name="测试用户",
        user_code="employee-7",
        runtime_workspace_id=127,
        runtime_id="rt-127",
        namespace="hermes-runtimes",
        service_name="hermes-runtime-127",
        image="test",
        service_account="hermes-runtime-workload",
        api_server_key_secret_name="key",
        runtime_root_pvc_name="root",
        hermes_home_sub_path="home",
        workspace_sub_path="workspace",
        runtime_root_pv_name="root-pv",
        runtime_root_host_path=str(root),
        runtime_node_name="demo",
        upstream_commit="cc4cab2f592e60a197e796506de9168f74baf3ea",
        state_schema=23,
        config_schema=33,
        startup_mode="steady_state",
        runtime_user_uid=os.getuid(),
        runtime_shared_gid=os.getgid(),
    )

    with pytest.raises(RuntimeConfigPreflightError, match="kanban"):
        await LocalRuntimeDirectoryProvisioner().ensure_directory(spec)

    assert list(outside.iterdir()) == []


async def test_local_runtime_directory_provisioner_rejects_kanban_database_symlink(
    tmp_path,
) -> None:
    root = tmp_path / "runtimes" / "7" / "128"
    home = root / "home"
    outside = tmp_path / "outside.db"
    home.mkdir(parents=True)
    outside.write_bytes(b"outside")
    (home / "kanban.db").symlink_to(outside)
    spec = KnativeRuntimeSpec(
        org_id=7,
        user_id=9,
        user_name="测试用户",
        user_code="employee-7",
        runtime_workspace_id=128,
        runtime_id="rt-128",
        namespace="hermes-runtimes",
        service_name="hermes-runtime-128",
        image="test",
        service_account="hermes-runtime-workload",
        api_server_key_secret_name="key",
        runtime_root_pvc_name="root",
        hermes_home_sub_path="home",
        workspace_sub_path="workspace",
        runtime_root_pv_name="root-pv",
        runtime_root_host_path=str(root),
        runtime_node_name="demo",
        upstream_commit="cc4cab2f592e60a197e796506de9168f74baf3ea",
        state_schema=23,
        config_schema=33,
        startup_mode="steady_state",
        runtime_user_uid=os.getuid(),
        runtime_shared_gid=os.getgid(),
    )

    with pytest.raises(RuntimeConfigPreflightError, match="kanban.db"):
        await LocalRuntimeDirectoryProvisioner().ensure_directory(spec)

    assert outside.read_bytes() == b"outside"


async def test_local_runtime_directory_provisioner_skips_kanban_for_application(
    tmp_path,
) -> None:
    root = tmp_path / "application-agents" / "25"
    spec = KnativeRuntimeSpec(
        org_id=7,
        user_id=9,
        user_name="测试用户",
        user_code="employee-7",
        runtime_workspace_id=25,
        runtime_id="app-25",
        namespace="hermes-runtimes",
        service_name="application-agent-25",
        image="test",
        service_account="hermes-runtime-workload",
        api_server_key_secret_name="key",
        runtime_root_pvc_name="root",
        hermes_home_sub_path="home",
        workspace_sub_path="workspace",
        runtime_root_pv_name="root-pv",
        runtime_root_host_path=str(root),
        runtime_node_name="demo",
        upstream_commit="cc4cab2f592e60a197e796506de9168f74baf3ea",
        state_schema=23,
        config_schema=33,
        startup_mode="steady_state",
        runtime_user_uid=os.getuid(),
        runtime_shared_gid=os.getgid(),
        application_agent_id=25,
    )

    await LocalRuntimeDirectoryProvisioner().ensure_directory(spec)

    assert not (root / "home" / "kanban").exists()


async def test_local_runtime_directory_provisioner_only_prepares_directories(tmp_path) -> None:
    root = tmp_path / "runtimes" / "7" / "124"
    spec = KnativeRuntimeSpec(
        org_id=7,
        user_id=9,
        user_name="测试用户",
        user_code="employee-7",
        runtime_workspace_id=124,
        runtime_id="rt-124",
        namespace="hermes-runtimes",
        service_name="hermes-runtime-124",
        image="test",
        service_account="hermes-runtime-workload",
        api_server_key_secret_name="key",
        runtime_root_pvc_name="root",
        hermes_home_sub_path="home",
        workspace_sub_path="workspace",
        runtime_root_pv_name="root-pv",
        runtime_root_host_path=str(root),
        runtime_node_name="demo",
        upstream_commit="cc4cab2f592e60a197e796506de9168f74baf3ea",
        state_schema=23,
        config_schema=33,
        startup_mode="steady_state",
        runtime_user_uid=os.getuid(),
        runtime_shared_gid=os.getgid(),
    )

    await LocalRuntimeDirectoryProvisioner().ensure_directory(spec)

    assert not (root / "home" / "config.yaml").exists()


async def test_local_runtime_directory_provisioner_supports_nested_subpaths(tmp_path) -> None:
    root = tmp_path / "runtimes" / "7" / "125"
    spec = KnativeRuntimeSpec(
        org_id=7,
        user_id=9,
        user_name="测试用户",
        user_code="employee-7",
        runtime_workspace_id=125,
        runtime_id="rt-125",
        namespace="hermes-runtimes",
        service_name="hermes-runtime-125",
        image="test",
        service_account="hermes-runtime-workload",
        api_server_key_secret_name="key",
        runtime_root_pvc_name="root",
        hermes_home_sub_path="orgs/7/users/9/profiles/125/home",
        workspace_sub_path="orgs/7/users/9/profiles/125/workspace",
        runtime_root_pv_name="root-pv",
        runtime_root_host_path=str(root),
        runtime_node_name="demo",
        upstream_commit="cc4cab2f592e60a197e796506de9168f74baf3ea",
        state_schema=23,
        config_schema=33,
        startup_mode="steady_state",
        runtime_user_uid=os.getuid(),
        runtime_shared_gid=os.getgid(),
    )

    await LocalRuntimeDirectoryProvisioner().ensure_directory(spec)

    home = root / spec.hermes_home_sub_path
    workspace = root / spec.workspace_sub_path
    assert home.is_dir()
    assert workspace.is_dir()
    assert stat.S_IMODE(home.stat().st_mode) == 0o2770
    assert stat.S_IMODE(workspace.stat().st_mode) == 0o2770


def test_runtime_api_key_codec_round_trips_without_plaintext_value() -> None:
    codec = RuntimeApiKeyCodec()

    encrypted = codec.encrypt("runtime-api-key")

    assert encrypted != "runtime-api-key"
    assert encrypted.startswith("local-v1:")
    assert codec.decrypt(encrypted) == "runtime-api-key"


def _workspace_record(*, user_code: str = "employee-7") -> RuntimeWorkspaceRecord:
    return RuntimeWorkspaceRecord(
        runtime_workspace_id=123,
        cloud_profile_id=123,
        org_id=7,
        user_id=9,
        user_name="测试用户",
        user_code=user_code,
        name="默认数字人",
        status="active",
    )


def test_default_binding_plan_uses_normalized_user_code() -> None:
    record = _workspace_record(user_code=" Employee-7 ")

    plan = RuntimeWorkspaceService.build_default_binding_plan(
        record,
        runtime_root_path="/data/hermes/runtimes",
    )

    assert plan.org_id == 7
    assert plan.user_id == 9
    assert plan.user_name == "测试用户"
    assert plan.user_code == " Employee-7 "
    assert plan.runtime_workspace_id == 123
    assert plan.runtime_id == "rt-123"
    assert plan.service_name == "runtime-employee-7"
    assert plan.secret_name == "runtime-employee-7-api-key"
    assert plan.hermes_home_sub_path == "home"
    assert plan.workspace_sub_path == "workspace"
    assert plan.runtime_root_pvc_name == "runtime-employee-7-root"
    assert plan.runtime_root_host_path == "/data/hermes/runtimes/7/employee-7"


@pytest.mark.parametrize(
    "user_code",
    ["", "-employee", "employee_", "employee/7", "员工7", "a" * 33],
)
def test_default_binding_plan_rejects_unsafe_user_code(user_code: str) -> None:
    with pytest.raises(ValueError, match="runtime resource key"):
        RuntimeWorkspaceService.build_default_binding_plan(
            _workspace_record(user_code=user_code),
            runtime_root_path="/data/hermes/runtimes",
        )


def test_runtime_root_host_path_rejects_boundary_escape() -> None:
    with pytest.raises(ValueError, match="escapes configured boundary"):
        runtime_root_host_path(
            runtime_root_path="/data/hermes/runtimes",
            org_id=7,
            resource_key="../../outside",
        )


def test_existing_binding_keeps_persisted_resource_identity() -> None:
    record = _workspace_record(user_code="renamed-employee")
    binding = SimpleNamespace(
        pending_reload=False,
        id=123,
        runtime_id="rt-123",
        knative_service="runtime-employee-7",
        hermes_home_sub_path="home",
        workspace_sub_path="workspace",
        runtime_root_pvc="runtime-employee-7-root",
        runtime_root_host_path="/data/hermes/runtimes/7/employee-7",
        runtime_config_source="pvc",
        runtime_root_pvc_uid=PVC_UID,
        pvc_storage_identity_hash=PVC_IDENTITY,
    )

    plan = SqlAlchemyRuntimeWorkspaceStore._plan_from_binding(record, binding)

    assert plan.service_name == "runtime-employee-7"
    assert plan.runtime_root_pvc_name == "runtime-employee-7-root"
    assert plan.runtime_root_host_path == "/data/hermes/runtimes/7/employee-7"


@pytest.mark.asyncio
async def test_ready_binding_returns_dashboard_endpoint() -> None:
    binding = SimpleNamespace(
        pending_reload=False,
        org_id=7,
        user_id=9,
        status="ready",
        runtime_id="rt-123",
        endpoint_base_url="http://127.0.0.1:30364",
        endpoint_host_header="runtime-employee-7.hermes-runtimes.hermes.local",
        api_key_encrypted=RuntimeApiKeyCodec().encrypt("runtime-key"),
    )
    store = SqlAlchemyRuntimeWorkspaceStore(
        SequencedAsyncSession(binding),
        namespace="hermes-runtimes",
        image=CURRENT_RUNTIME_IMAGE,
        ingress_base_url="http://127.0.0.1:30364",
        hermes_home_mount_path="/opt/data",
        workspace_mount_path="/data/workspace",
    )

    endpoint = await store.get_runtime_endpoint(_workspace_record())

    assert endpoint.endpoint_base_url == "http://127.0.0.1:30364"
    assert endpoint.endpoint_host_header == ("runtime-employee-7.hermes-runtimes.hermes.local")
    assert endpoint.api_key == "runtime-key"
    assert not hasattr(endpoint, "gateway_api_base_url")


@pytest.mark.asyncio
async def test_ready_binding_with_pending_reload_requires_reconcile() -> None:
    """pending identity Desired 必须触发重投影，不能按 ready 直接返回。"""
    binding = SimpleNamespace(
        id=123,
        runtime_id="rt-123",
        knative_service="runtime-employee-7",
        hermes_home_sub_path="persisted/home",
        workspace_sub_path="persisted/workspace",
        runtime_root_pvc="runtime-employee-7-root",
        runtime_root_host_path="/data/hermes/runtimes/7/employee-7",
        runtime_config_source="pvc",
        runtime_root_pvc_uid=PVC_UID,
        pvc_storage_identity_hash=PVC_IDENTITY,
        image=CURRENT_RUNTIME_IMAGE,
        status="ready",
        pending_reload=True,
        pending_reload_scopes=["identity"],
        last_error=None,
        endpoint_base_url="http://127.0.0.1:30364",
        endpoint_host_header="runtime-employee-7.hermes-runtimes.hermes.local",
        api_key_encrypted=RuntimeApiKeyCodec().encrypt("runtime-key"),
    )
    session = SequencedAsyncSession(binding)
    store = SqlAlchemyRuntimeWorkspaceStore(
        session,
        namespace="hermes-runtimes",
        runtime_image_config=StaticRuntimeImageConfigReader(
            image=CURRENT_RUNTIME_IMAGE,
        ),
        ingress_base_url="http://127.0.0.1:30364",
        workspace_mount_path="/data/workspace",
        hermes_home_mount_path="/data/home",
        platform_control_key_store=FakePlatformControlKeyStore(),
    )

    result = await store.ensure_binding(_workspace_record(user_code="renamed-employee"))

    assert result.reconcile_required is True


@pytest.mark.asyncio
async def test_existing_binding_is_read_without_rewriting_persisted_subpaths() -> None:
    binding = SimpleNamespace(
        id=123,
        runtime_id="rt-123",
        knative_service="runtime-employee-7",
        hermes_home_sub_path="persisted/home",
        workspace_sub_path="persisted/workspace",
        runtime_root_pvc="runtime-employee-7-root",
        runtime_root_host_path="/data/hermes/runtimes/7/employee-7",
        runtime_config_source="pvc",
        runtime_root_pvc_uid=PVC_UID,
        pvc_storage_identity_hash=PVC_IDENTITY,
        image=CURRENT_RUNTIME_IMAGE,
        status="ready",
        pending_reload=False,
        last_error=None,
        endpoint_base_url="http://127.0.0.1:30364",
        endpoint_host_header="runtime-employee-7.hermes-runtimes.hermes.local",
        api_key_encrypted=RuntimeApiKeyCodec().encrypt("runtime-key"),
    )
    session = SequencedAsyncSession(binding)
    store = SqlAlchemyRuntimeWorkspaceStore(
        session,
        namespace="hermes-runtimes",
        runtime_image_config=StaticRuntimeImageConfigReader(
            image=CURRENT_RUNTIME_IMAGE,
        ),
        ingress_base_url="http://127.0.0.1:30364",
        hermes_home_mount_path="/data/hermes/home",
        workspace_mount_path="/data/workspace",
        platform_control_key_store=FakePlatformControlKeyStore(),
    )

    result = await store.ensure_binding(_workspace_record(user_code="renamed-employee"))

    assert result.created is False
    assert result.api_server_key == "runtime-key"
    assert result.plan.service_name == "runtime-employee-7"
    assert result.plan.runtime_root_pvc_name == "runtime-employee-7-root"
    assert result.plan.runtime_root_host_path == "/data/hermes/runtimes/7/employee-7"
    assert result.plan.hermes_home_sub_path == "persisted/home"
    assert result.plan.workspace_sub_path == "persisted/workspace"
    assert result.reconcile_required is False
    assert result.initial_bootstrap_resume_eligible is False
    assert session.commits == 0


@pytest.mark.asyncio
async def test_initial_starting_binding_without_endpoint_is_resume_eligible() -> None:
    binding = SimpleNamespace(
        pending_reload=False,
        id=123,
        runtime_id="rt-123",
        knative_service="runtime-employee-7",
        hermes_home_sub_path="home",
        workspace_sub_path="workspace",
        runtime_root_pvc="runtime-employee-7-root",
        runtime_root_host_path="/data/hermes/runtimes/7/employee-7",
        runtime_config_source="pvc",
        runtime_root_pvc_uid=None,
        pvc_storage_identity_hash=None,
        image=CURRENT_RUNTIME_IMAGE,
        status="starting",
        endpoint_base_url=None,
        endpoint_host_header=None,
        api_key_encrypted=RuntimeApiKeyCodec().encrypt("runtime-key"),
    )
    store = SqlAlchemyRuntimeWorkspaceStore(
        SequencedAsyncSession(binding),
        namespace="hermes-runtimes",
        image=CURRENT_RUNTIME_IMAGE,
        ingress_base_url="http://127.0.0.1:30364",
        hermes_home_mount_path="/opt/data",
        workspace_mount_path="/data/workspace",
        platform_control_key_store=FakePlatformControlKeyStore(),
    )

    result = await store.ensure_binding(_workspace_record())

    assert result.created is False
    assert result.binding_status == "starting"
    assert result.reconcile_owned is False
    assert result.initial_bootstrap_resume_eligible is True


@pytest.mark.parametrize(
    ("endpoint_base_url", "endpoint_host_header"),
    [
        ("http://127.0.0.1:30364", "runtime.example"),
    ],
)
@pytest.mark.asyncio
async def test_starting_binding_with_live_endpoint_is_not_resume_eligible(
    endpoint_base_url: str | None,
    endpoint_host_header: str | None,
) -> None:
    binding = SimpleNamespace(
        pending_reload=False,
        id=123,
        runtime_id="rt-123",
        knative_service="runtime-employee-7",
        hermes_home_sub_path="home",
        workspace_sub_path="workspace",
        runtime_root_pvc="runtime-employee-7-root",
        runtime_root_host_path="/data/hermes/runtimes/7/employee-7",
        runtime_config_source="pvc",
        runtime_root_pvc_uid=None,
        pvc_storage_identity_hash=None,
        image=CURRENT_RUNTIME_IMAGE,
        status="starting",
        endpoint_base_url=endpoint_base_url,
        endpoint_host_header=endpoint_host_header,
        api_key_encrypted=RuntimeApiKeyCodec().encrypt("runtime-key"),
    )
    store = SqlAlchemyRuntimeWorkspaceStore(
        SequencedAsyncSession(binding),
        namespace="hermes-runtimes",
        image=CURRENT_RUNTIME_IMAGE,
        ingress_base_url="http://127.0.0.1:30364",
        hermes_home_mount_path="/opt/data",
        workspace_mount_path="/data/workspace",
        platform_control_key_store=FakePlatformControlKeyStore(),
    )

    result = await store.ensure_binding(_workspace_record())

    assert result.initial_bootstrap_resume_eligible is False


@pytest.mark.asyncio
async def test_existing_binding_keeps_its_bound_image(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    binding = SimpleNamespace(
        id=123,
        runtime_id="rt-123",
        knative_service="runtime-employee-7",
        hermes_home_sub_path="home",
        workspace_sub_path="workspace",
        runtime_root_pvc="runtime-employee-7-root",
        runtime_root_host_path="/data/hermes/runtimes/7/employee-7",
        runtime_config_source="pvc",
        runtime_root_pvc_uid=PVC_UID,
        pvc_storage_identity_hash=PVC_IDENTITY,
        image=OLD_RUNTIME_IMAGE,
        status="ready",
        pending_reload=False,
        last_error=None,
        endpoint_base_url="http://127.0.0.1:30364",
        endpoint_host_header="runtime-employee-7.hermes-runtimes.hermes.local",
        api_key_encrypted=RuntimeApiKeyCodec().encrypt("runtime-key"),
    )
    session = SequencedAsyncSession(binding)
    store = SqlAlchemyRuntimeWorkspaceStore(
        session,
        namespace="hermes-runtimes",
        runtime_image_config=StaticRuntimeImageConfigReader(
            image=CURRENT_RUNTIME_IMAGE,
        ),
        ingress_base_url="http://127.0.0.1:30364",
        hermes_home_mount_path="/opt/data",
        workspace_mount_path="/data/workspace",
        platform_control_key_store=FakePlatformControlKeyStore(),
    )

    result = await store.ensure_binding(_workspace_record())

    assert result.plan.image == OLD_RUNTIME_IMAGE
    assert result.reconcile_required is False
    assert binding.image == OLD_RUNTIME_IMAGE
    assert binding.status == "ready"
    assert binding.pending_reload is False
    assert session.commits == 0


@pytest.mark.asyncio
async def test_failed_never_started_binding_retries_with_current_runtime_image() -> None:
    binding = SimpleNamespace(
        id=123,
        runtime_id="rt-123",
        knative_service="runtime-employee-7",
        hermes_home_sub_path="home",
        workspace_sub_path="workspace",
        runtime_root_pvc="runtime-employee-7-root",
        runtime_root_host_path="/data/hermes/runtimes/7/employee-7",
        runtime_config_source="pvc",
        runtime_root_pvc_uid=PVC_UID,
        pvc_storage_identity_hash=PVC_IDENTITY,
        image=OLD_RUNTIME_IMAGE,
        image_digest=None,
        status="failed",
        pending_reload=True,
        last_error="initial bootstrap failed",
        endpoint_base_url=None,
        endpoint_host_header=None,
        api_key_encrypted=RuntimeApiKeyCodec().encrypt("runtime-key"),
    )
    store = SqlAlchemyRuntimeWorkspaceStore(
        SequencedAsyncSession(binding, None),
        namespace="hermes-runtimes",
        runtime_image_config=StaticRuntimeImageConfigReader(image=CURRENT_RUNTIME_IMAGE),
        ingress_base_url="http://127.0.0.1:30364",
        hermes_home_mount_path="/opt/data",
        workspace_mount_path="/data/workspace",
        platform_control_key_store=FakePlatformControlKeyStore(),
    )

    result = await store.ensure_binding(_workspace_record())

    assert result.plan.image == CURRENT_RUNTIME_IMAGE


@pytest.mark.asyncio
async def test_failed_binding_with_schema_intent_keeps_its_bound_image() -> None:
    binding = SimpleNamespace(
        id=123,
        runtime_id="rt-123",
        knative_service="runtime-employee-7",
        hermes_home_sub_path="home",
        workspace_sub_path="workspace",
        runtime_root_pvc="runtime-employee-7-root",
        runtime_root_host_path="/data/hermes/runtimes/7/employee-7",
        runtime_config_source="pvc",
        runtime_root_pvc_uid=PVC_UID,
        pvc_storage_identity_hash=PVC_IDENTITY,
        image=OLD_RUNTIME_IMAGE,
        image_digest=None,
        status="failed",
        pending_reload=True,
        last_error="fresh init interrupted",
        endpoint_base_url=None,
        endpoint_host_header=None,
        api_key_encrypted=RuntimeApiKeyCodec().encrypt("runtime-key"),
    )
    store = SqlAlchemyRuntimeWorkspaceStore(
        SequencedAsyncSession(binding, 99),
        namespace="hermes-runtimes",
        runtime_image_config=StaticRuntimeImageConfigReader(image=CURRENT_RUNTIME_IMAGE),
        ingress_base_url="http://127.0.0.1:30364",
        hermes_home_mount_path="/opt/data",
        workspace_mount_path="/data/workspace",
        platform_control_key_store=FakePlatformControlKeyStore(),
    )

    result = await store.ensure_binding(_workspace_record())

    assert result.plan.image == OLD_RUNTIME_IMAGE


@pytest.mark.asyncio
async def test_existing_bound_image_is_not_mutated_by_global_default(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    binding = SimpleNamespace(
        id=123,
        runtime_id="rt-123",
        knative_service="runtime-employee-7",
        hermes_home_sub_path="home",
        workspace_sub_path="workspace",
        runtime_root_pvc="runtime-employee-7-root",
        runtime_root_host_path="/data/hermes/runtimes/7/employee-7",
        runtime_config_source="pvc",
        runtime_root_pvc_uid=PVC_UID,
        pvc_storage_identity_hash=PVC_IDENTITY,
        image=OLD_RUNTIME_IMAGE,
        status="ready",
        pending_reload=False,
        last_error="old error",
        endpoint_base_url="http://127.0.0.1:30364",
        endpoint_host_header="runtime-employee-7.hermes-runtimes.hermes.local",
        api_key_encrypted=RuntimeApiKeyCodec().encrypt("runtime-key"),
    )
    session = SequencedAsyncSession(binding)
    store = SqlAlchemyRuntimeWorkspaceStore(
        session,
        namespace="hermes-runtimes",
        runtime_image_config=StaticRuntimeImageConfigReader(
            image=CURRENT_RUNTIME_IMAGE,
        ),
        ingress_base_url="http://127.0.0.1:30364",
        hermes_home_mount_path="/opt/data",
        workspace_mount_path="/data/workspace",
        platform_control_key_store=FakePlatformControlKeyStore(),
    )

    result = await store.ensure_binding(_workspace_record())

    assert result.plan.image == OLD_RUNTIME_IMAGE
    assert result.reconcile_required is False
    assert binding.image == OLD_RUNTIME_IMAGE
    assert binding.status == "ready"
    assert binding.pending_reload is False
    assert binding.last_error == "old error"
    assert session.commits == 0


@pytest.mark.parametrize(
    ("pvc_name", "host_path"),
    [
        (None, "/data/hermes/runtimes/7/employee-7"),
        ("runtime-employee-7-root", None),
    ],
)
def test_existing_binding_requires_complete_persisted_storage_identity(
    pvc_name: str | None,
    host_path: str | None,
) -> None:
    binding = SimpleNamespace(
        pending_reload=False,
        runtime_id="rt-123",
        knative_service="runtime-employee-7",
        hermes_home_sub_path="home",
        workspace_sub_path="workspace",
        runtime_root_pvc=pvc_name,
        runtime_root_host_path=host_path,
        runtime_config_source="pvc",
    )

    with pytest.raises(RuntimeConfigPreflightError, match="storage identity"):
        SqlAlchemyRuntimeWorkspaceStore._plan_from_binding(
            _workspace_record(),
            binding,
        )


@pytest.mark.asyncio
async def test_new_binding_rejects_service_name_already_bound_in_namespace() -> None:
    occupied = SimpleNamespace(knative_service="runtime-employee-7")
    session = SequencedAsyncSession(None, occupied)
    store = SqlAlchemyRuntimeWorkspaceStore(
        session,
        namespace="hermes-runtimes",
        image=CURRENT_RUNTIME_IMAGE,
        ingress_base_url="http://127.0.0.1:30364",
        hermes_home_mount_path="/data/hermes/home",
        workspace_mount_path="/data/workspace",
    )

    with pytest.raises(
        RuntimeConfigPreflightError,
        match="runtime resource name is already bound",
    ):
        await store.ensure_binding(_workspace_record())

    assert session.commits == 0
    collision_sql = str(session.statements[1].compile(compile_kwargs={"literal_binds": True}))
    assert "k8s_namespace = 'hermes-runtimes'" in collision_sql
    assert "knative_service = 'runtime-employee-7'" in collision_sql


@pytest.mark.asyncio
async def test_new_binding_persists_dashboard_route_fields_without_gateway_url() -> None:
    session = SequencedAsyncSession(
        None,
        None,
        None,
        SimpleNamespace(id=123),
    )
    store = SqlAlchemyRuntimeWorkspaceStore(
        session,
        namespace="hermes-runtimes",
        image="kind.local/hermes-agent:v0.19.0-f4df260f2-hermes",
        ingress_base_url="http://127.0.0.1:30364",
        hermes_home_mount_path="/opt/data",
        workspace_mount_path="/data/workspace",
        platform_control_key_store=FakePlatformControlKeyStore(created=True),
    )

    result = await store.ensure_binding(_workspace_record())

    insert_statement = next(statement for statement in session.statements if statement.is_insert)
    insert_values = insert_statement.compile().params
    assert "gateway_base_url" not in insert_values
    assert "endpoint_base_url" not in insert_values
    assert "endpoint_host_header" not in insert_values
    assert result.created is True
    assert result.reconcile_required is True


@pytest.mark.asyncio
async def test_new_personal_runtime_uses_env_config_without_release_queries() -> None:
    session = SqlAlchemySequencedAsyncSession(
        None,
        None,
        None,
        SimpleNamespace(id=123),
    )
    store = SqlAlchemyRuntimeWorkspaceStore(
        session,
        namespace="hermes-runtimes",
        runtime_image_config=StaticRuntimeImageConfigReader(
            image=CURRENT_RUNTIME_IMAGE,
            manifest_digest="sha256:" + "a" * 64,
            config_id="sha256:" + "b" * 64,
        ),
        ingress_base_url="http://127.0.0.1:30364",
        hermes_home_mount_path="/opt/data",
        workspace_mount_path="/data/workspace",
        platform_control_key_store=FakePlatformControlKeyStore(created=True),
    )

    result = await store.ensure_binding(_workspace_record())

    insert_statement = next(statement for statement in session.statements if statement.is_insert)
    insert_values = insert_statement.compile().params
    assert result.plan.image == CURRENT_RUNTIME_IMAGE
    assert result.plan.upstream_commit == ("f80f453ae0679347e38abc917c7f94f717bf96c5")
    assert result.plan.state_schema == 25
    assert result.plan.config_schema == 34
    assert result.plan.config_revision == "generation-1"
    assert insert_values["image"] == CURRENT_RUNTIME_IMAGE
    assert result.plan.image_digest == "sha256:" + "b" * 64
    assert insert_values["image_digest"] == "sha256:" + "b" * 64
    assert len(session.statements) == 4
    assert session.commits == 2


def test_default_binding_plan_uses_platform_owner_paths_and_knative_names() -> None:
    record = RuntimeWorkspaceRecord(
        runtime_workspace_id=123,
        cloud_profile_id=123,
        org_id=7,
        user_id=9,
        user_name="测试用户",
        user_code="employee-7",
        name="默认数字人",
        status="active",
    )

    plan = _control_plan(RuntimeWorkspaceService.build_default_binding_plan(record))

    assert plan.org_id == 7
    assert plan.user_id == 9
    assert plan.runtime_workspace_id == 123
    assert plan.runtime_id == "rt-123"
    assert plan.service_name == "runtime-employee-7"
    assert plan.secret_name == "runtime-employee-7-api-key"
    assert plan.hermes_home_sub_path == "home"
    assert plan.workspace_sub_path == "workspace"
    assert plan.runtime_root_pvc_name == "runtime-employee-7-root"


async def test_runtime_ready_service_persists_route_and_marks_ready_after_health() -> None:
    events: list[str] = []
    kservice = {
        "status": {
            "url": "http://hermes-runtime-123.hermes-runtimes.hermes.local",
            "latestCreatedRevisionName": "hermes-runtime-123-00001",
            "latestReadyRevisionName": "hermes-runtime-123-00001",
            "conditions": [{"type": "Ready", "status": "True"}],
        }
    }
    k8s = FakeKnativeClient(kservice, events=events)
    store = FakeBindingStatusStore(events)
    protocol = FakeProtocolAdapter(events=events)
    record = RuntimeWorkspaceRecord(
        runtime_workspace_id=123,
        cloud_profile_id=123,
        org_id=7,
        user_id=9,
        user_name="测试用户",
        user_code="employee-7",
        name="默认数字人",
        status="active",
    )
    plan = _control_plan(RuntimeWorkspaceService.build_default_binding_plan(record))

    result = await RuntimeBindingReadyService(
        k8s_client=k8s,
        binding_store=store,
        protocol_adapter=protocol,
    ).ensure_kservice_ready(
        spec=RuntimeBindingReadyService.build_knative_spec(
            plan=plan,
            namespace="hermes-runtimes",
            image="kind.local/hermes-agent:v2026.07.02",
            service_account="hermes-runtime-workload",
            startup_mode="steady_state",
            default_model_entry_id=9,
            enterprise_knowledge_host_path="",
        ),
        api_server_key="runtime-api-key",
        platform_control_key=_control_material(),
        ingress_base_url="http://127.0.0.1:30364",
    )

    assert result.status == "ready"
    assert result.route_host == "hermes-runtime-123.hermes-runtimes.hermes.local"
    assert result.endpoint_base_url == "http://127.0.0.1:30364"
    assert result.endpoint_host_header == "hermes-runtime-123.hermes-runtimes.hermes.local"
    assert not hasattr(result, "gateway_base_url")
    assert protocol.health_endpoint.endpoint_base_url == "http://127.0.0.1:30364"
    assert protocol.health_endpoint.endpoint_host_header == (
        "hermes-runtime-123.hermes-runtimes.hermes.local"
    )
    assert not hasattr(protocol.health_endpoint, "gateway_api_base_url")
    assert protocol.health_endpoint.api_key == "runtime-api-key"
    assert protocol.capabilities_endpoint is None
    assert [item["kind"] for item in k8s.applied] == [
        "PersistentVolume",
        "PersistentVolumeClaim",
        "Secret",
        "Secret",
        "Service",
    ]
    container = k8s.applied[4]["spec"]["template"]["spec"]["containers"][0]
    assert {
        "name": "NEWAPI_API_KEY",
        "valueFrom": {
            "secretKeyRef": {
                "name": "hermes-newapi-runtime",
                "key": "NEWAPI_API_KEY",
            }
        },
    } in container["env"]
    assert container["ports"] == [{"name": "http1", "containerPort": 9120}]
    assert all(mount["name"] != "runtime-config" for mount in container["volumeMounts"])
    assert k8s.requested == [
        {"namespace": "hermes-runtimes", "name": "runtime-employee-7"},
    ]
    assert k8s.requested_pvcs == [
        {
            "namespace": "hermes-runtimes",
            "name": "runtime-employee-7-root",
        }
    ]
    assert store.pvc_identities == [(123, PVC_UID, PVC_IDENTITY)]
    assert k8s.deleted_services == [
        {"namespace": "hermes-runtimes", "name": "runtime-employee-7-api"}
    ]
    assert [patch.status for patch in store.patches] == ["starting", "ready"]
    assert store.patches[-1].last_error is None
    assert events == [
        "apply:PersistentVolume/runtime-employee-7-root-pv",
        "apply:PersistentVolumeClaim/runtime-employee-7-root",
        "get:PVC/runtime-employee-7-root",
        "binding:pvc-identity",
        "apply:Secret/runtime-employee-7-platform-control",
        "apply:Secret/runtime-employee-7-api-key",
        "apply:KService/runtime-employee-7",
        "get:KService/runtime-employee-7",
        "binding:starting:http://127.0.0.1:30364",
        "dashboard:health",
        "delete:Service/runtime-employee-7-api",
        "binding:ready:http://127.0.0.1:30364",
    ]


async def test_runtime_ready_service_runs_startup_reconciler_before_full_health() -> None:
    events: list[str] = []
    kservice = {
        "status": {
            "url": "http://hermes-runtime-123.hermes-runtimes.hermes.local",
            "latestCreatedRevisionName": "hermes-runtime-123-00001",
            "latestReadyRevisionName": "hermes-runtime-123-00001",
            "conditions": [{"type": "Ready", "status": "True"}],
        }
    }

    async def reconcile_startup(_spec) -> None:
        events.append("schema:intent")

    k8s = FakeKnativeClient(kservice, events=events)
    result = await RuntimeBindingReadyService(
        k8s_client=k8s,
        binding_store=FakeBindingStatusStore(events),
        protocol_adapter=FakeProtocolAdapter(events=events),
        startup_reconciler=reconcile_startup,
    ).ensure_kservice_ready(
        spec=RuntimeBindingReadyService.build_knative_spec(
            plan=_control_plan(
                RuntimeWorkspaceService.build_default_binding_plan(_workspace_record())
            ),
            namespace="hermes-runtimes",
            image="kind.local/hermes-agent:v2026.07.02",
            service_account="hermes-runtime-workload",
            startup_mode="steady_state",
        ),
        api_server_key="runtime-api-key",
        platform_control_key=_control_material(),
        ingress_base_url="http://127.0.0.1:30364",
    )

    assert result.status == "ready"
    assert events.index("schema:intent") < events.index("dashboard:health")


async def test_runtime_ready_service_completes_fresh_intent_after_health() -> None:
    events: list[str] = []
    kservice = {
        "status": {
            "url": "http://runtime-employee-7.hermes-runtimes.hermes.local",
            "conditions": [{"type": "Ready", "status": "True"}],
        }
    }

    async def prepare(spec, _ingress):
        return replace(
            spec,
            bootstrap_intent_id="bootstrap-intent-123",
            bootstrap_authorization_digest="sha256:" + "c" * 64,
        )

    async def complete(spec) -> None:
        assert spec.bootstrap_intent_id == "bootstrap-intent-123"
        events.append("bootstrap:completed")

    spec = RuntimeBindingReadyService.build_knative_spec(
        plan=_control_plan(RuntimeWorkspaceService.build_default_binding_plan(_workspace_record())),
        namespace="hermes-runtimes",
        image="kind.local/hermes-agent:v2026.07.02",
        service_account="hermes-runtime-workload",
        startup_mode="fresh_bootstrap",
    )
    spec = replace(
        spec,
        initial_config_download_url="http://backend/internal/runtime-config/123",
        initial_config_digest="b" * 64,
    )

    k8s = FakeKnativeClient(kservice, events=events)
    result = await RuntimeBindingReadyService(
        k8s_client=k8s,
        binding_store=FakeBindingStatusStore(events),
        protocol_adapter=FakeProtocolAdapter(events=events),
        schema_startup_preparer=prepare,
        bootstrap_completion=complete,
    ).ensure_kservice_ready(
        spec=spec,
        api_server_key="runtime-api-key",
        platform_control_key=_control_material(),
        ingress_base_url="http://127.0.0.1:30364",
    )

    assert result.status == "ready"
    assert events.index("dashboard:health") < events.index("bootstrap:completed")
    assert events.index("bootstrap:completed") < events.index(
        "binding:ready:http://127.0.0.1:30364"
    )
    kservices = [item for item in k8s.applied if item["kind"] == "Service"]
    modes = [
        next(
            env["value"]
            for env in item["spec"]["template"]["spec"]["containers"][0]["env"]
            if env["name"] == "HERMES_STARTUP_MODE"
        )
        for item in kservices
    ]
    assert modes == ["fresh_bootstrap", "steady_state"]
    assert k8s.deleted_kservices == [
        {"namespace": "hermes-runtimes", "name": "runtime-employee-7"}
    ]
    kservice_applies = [
        index
        for index, event in enumerate(events)
        if event == "apply:KService/runtime-employee-7"
    ]
    assert events.index("bootstrap:completed") < events.index(
        "delete:KService/runtime-employee-7"
    ) < kservice_applies[1]


async def test_runtime_ready_service_marks_failed_when_health_check_fails() -> None:
    kservice = {
        "status": {
            "url": "http://hermes-runtime-123.hermes-runtimes.hermes.local",
            "latestCreatedRevisionName": "hermes-runtime-123-00001",
            "latestReadyRevisionName": "hermes-runtime-123-00001",
            "conditions": [{"type": "Ready", "status": "True"}],
        }
    }
    store = FakeBindingStatusStore()

    result = await RuntimeBindingReadyService(
        k8s_client=FakeKnativeClient(kservice),
        binding_store=store,
        protocol_adapter=FakeProtocolAdapter(fail_health=True),
    ).ensure_kservice_ready(
        spec=RuntimeBindingReadyService.build_knative_spec(
            plan=_control_plan(
                RuntimeWorkspaceService.build_default_binding_plan(
                    RuntimeWorkspaceRecord(
                        runtime_workspace_id=123,
                        cloud_profile_id=123,
                        org_id=7,
                        user_id=9,
                        user_name="测试用户",
                        user_code="employee-7",
                        name="默认数字人",
                        status="active",
                    )
                )
            ),
            namespace="hermes-runtimes",
            image="kind.local/hermes-agent:v2026.07.02",
            service_account="hermes-runtime-workload",
            startup_mode="steady_state",
        ),
        api_server_key="runtime-api-key",
        platform_control_key=_control_material(),
        ingress_base_url="http://127.0.0.1:30364",
    )

    assert result.status == "failed"
    assert "health failed" in (result.last_error or "")
    assert store.patches[-1].status == "failed"
    assert "health failed" in (store.patches[-1].last_error or "")


async def test_runtime_ready_service_never_overwrites_changed_pvc_identity() -> None:
    store = FakeBindingStatusStore()
    k8s = FakeKnativeClient()
    old_uid = "00000000-0000-0000-0000-000000000099"
    plan = replace(
        _control_plan(RuntimeWorkspaceService.build_default_binding_plan(_workspace_record())),
        runtime_root_pvc_uid=old_uid,
        pvc_storage_identity_hash=runtime_pvc_storage_identity_hash(
            namespace="hermes-runtimes",
            pvc_uid=old_uid,
        ),
    )

    result = await RuntimeBindingReadyService(
        k8s_client=k8s,
        binding_store=store,
        protocol_adapter=FakeProtocolAdapter(),
    ).ensure_kservice_ready(
        spec=RuntimeBindingReadyService.build_knative_spec(
            plan=plan,
            namespace="hermes-runtimes",
            image="kind.local/hermes-agent:v2026.07.02",
            service_account="hermes-runtime-workload",
            startup_mode="steady_state",
        ),
        api_server_key="runtime-api-key",
        platform_control_key=_control_material(),
        ingress_base_url="http://127.0.0.1:30364",
    )

    assert result.status == "failed"
    assert "PVC identity changed" in (result.last_error or "")
    assert [item["kind"] for item in k8s.applied] == [
        "PersistentVolume",
        "PersistentVolumeClaim",
    ]
    assert store.pvc_identities == []


async def test_runtime_ready_service_waits_for_latest_revision_before_health_check() -> None:
    deploying = {
        "status": {
            "url": "http://hermes-runtime-123.hermes-runtimes.hermes.local",
            "latestCreatedRevisionName": "hermes-runtime-123-00002",
            "latestReadyRevisionName": "hermes-runtime-123-00001",
            "conditions": [{"type": "Ready", "status": "Unknown", "reason": "Deploying"}],
        }
    }
    ready = {
        "status": {
            "url": "http://hermes-runtime-123.hermes-runtimes.hermes.local",
            "latestCreatedRevisionName": "hermes-runtime-123-00002",
            "latestReadyRevisionName": "hermes-runtime-123-00002",
            "conditions": [{"type": "Ready", "status": "True"}],
        }
    }
    k8s = FakeKnativeClient([deploying, ready])

    result = await RuntimeBindingReadyService(
        k8s_client=k8s,
        binding_store=FakeBindingStatusStore(),
        protocol_adapter=FakeProtocolAdapter(),
    ).ensure_kservice_ready(
        spec=RuntimeBindingReadyService.build_knative_spec(
            plan=_control_plan(
                RuntimeWorkspaceService.build_default_binding_plan(
                    RuntimeWorkspaceRecord(
                        runtime_workspace_id=123,
                        cloud_profile_id=123,
                        org_id=7,
                        user_id=9,
                        user_name="测试用户",
                        user_code="employee-7",
                        name="默认数字人",
                        status="active",
                    )
                )
            ),
            namespace="hermes-runtimes",
            image="kind.local/hermes-agent:v2026.07.02",
            service_account="hermes-runtime-workload",
            startup_mode="steady_state",
        ),
        api_server_key="runtime-api-key",
        platform_control_key=_control_material(),
        ingress_base_url="http://127.0.0.1:30364",
    )

    assert result.status == "ready"
    assert [request["name"] for request in k8s.requested] == [
        "runtime-employee-7",
        "runtime-employee-7",
    ]
    assert k8s.deleted_services == [
        {"namespace": "hermes-runtimes", "name": "runtime-employee-7-api"}
    ]


async def test_runtime_ready_service_uses_configured_readiness_timeout() -> None:
    kservice = {
        "status": {
            "url": "http://runtime-employee-7.hermes-runtimes.hermes.local",
            "latestCreatedRevisionName": "runtime-employee-7-00004",
            "latestReadyRevisionName": "runtime-employee-7-00004",
            "conditions": [{"type": "Ready", "status": "True"}],
        }
    }

    class TimeoutRecordingService(RuntimeBindingReadyService):
        seen_timeout: float | None = None

        async def _wait_for_kservice_ready(
            self,
            *,
            namespace: str,
            name: str,
            timeout_seconds: float,
            poll_interval_seconds: float = 2.0,
        ) -> dict:
            del namespace, name, poll_interval_seconds
            self.seen_timeout = timeout_seconds
            return kservice

    service = TimeoutRecordingService(
        k8s_client=FakeKnativeClient(kservice),
        binding_store=FakeBindingStatusStore(),
        protocol_adapter=FakeProtocolAdapter(),
        ready_timeout_seconds=37,
    )

    await service.ensure_kservice_ready(
        spec=RuntimeBindingReadyService.build_knative_spec(
            plan=_control_plan(
                RuntimeWorkspaceService.build_default_binding_plan(_workspace_record())
            ),
            namespace="hermes-runtimes",
            image="kind.local/hermes-agent:v0.19.0-f4df260f2-hermes",
            service_account="hermes-runtime-workload",
            startup_mode="steady_state",
            enterprise_knowledge_host_path="",
        ),
        api_server_key="runtime-api-key",
        platform_control_key=_control_material(),
        ingress_base_url="http://127.0.0.1:30364",
    )

    assert service.seen_timeout == 37


@pytest.mark.asyncio
async def test_runtime_ready_wait_retries_transient_kservice_not_found() -> None:
    ready = {
        "status": {
            "conditions": [{"type": "Ready", "status": "True"}],
        }
    }

    class EventuallyVisibleClient(FakeKnativeClient):
        async def get_kservice(self, *, namespace: str, name: str) -> dict:
            if not self.requested:
                self.requested.append({"namespace": namespace, "name": name})
                raise RuntimeConfigPreflightError(
                    'kubectl get ksvc failed: Error from server (NotFound): '
                    'services.serving.knative.dev "runtime-employee-7" not found'
                )
            return await super().get_kservice(namespace=namespace, name=name)

    client = EventuallyVisibleClient(ready)
    service = RuntimeBindingReadyService(
        k8s_client=client,
        binding_store=FakeBindingStatusStore(),
        protocol_adapter=FakeProtocolAdapter(),
    )

    observed = await service._wait_for_kservice_ready(
        namespace="hermes-runtimes",
        name="runtime-employee-7",
        timeout_seconds=1,
        poll_interval_seconds=0,
    )

    assert observed == ready
    assert len(client.requested) == 2


async def test_sqlalchemy_binding_status_store_updates_endpoint_without_secret() -> None:
    session = FakeAsyncSession()

    await SqlAlchemyRuntimeBindingStatusStore(session).update_binding_status(
        RuntimeBindingStatusPatch(
            runtime_workspace_id=123,
            knative_status_url="http://hermes-runtime-123.hermes-runtimes.hermes.local",
            route_host="hermes-runtime-123.hermes-runtimes.hermes.local",
            endpoint_base_url="http://127.0.0.1:30364",
            endpoint_host_header="hermes-runtime-123.hermes-runtimes.hermes.local",
            status="ready",
        )
    )

    assert session.commits == 1
    sql = str(session.statements[0].compile(compile_kwargs={"literal_binds": True}))
    assert "endpoint_host_header=" in sql
    assert "gateway_base_url" not in sql
    assert "status='ready'" in sql
    assert "pending_reload=false" in sql
    assert "api_key_encrypted" not in sql
