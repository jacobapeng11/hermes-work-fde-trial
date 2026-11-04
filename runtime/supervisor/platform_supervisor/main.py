from __future__ import annotations

from contextlib import asynccontextmanager
import asyncio
from datetime import datetime, timezone
import logging
import time
from uuid import uuid4

from fastapi import FastAPI
from fastapi.responses import JSONResponse
import uvicorn

from platform_supervisor.startup_timing import log_summary, mark
from platform_supervisor.health import (
    RuntimeHealth,
    evaluate_startup_resources,
    probe_startup_resources,
    raise_nofile_soft_limit,
)
from platform_supervisor.configuration import RuntimeConfigController
from platform_supervisor.edge import (
    ExternalConnectionGate,
    RuntimeEdgeDependencies,
    attach_runtime_routes,
)
from platform_supervisor.channel_fast_lane import (
    DurableChannelFastLaneV2Router,
    attach_channel_fast_lane,
)
from platform_supervisor.admission_audit import AdmissionAuditReporter
from platform_supervisor.execution import (
    CapacityGate,
    RedirectBoundaryRouteResolver,
    RedirectDurableGate,
    RunManager,
    SessionManager,
)
from platform_supervisor.hermes import (
    ActiveRunControlPeerRegistry,
    HermesExecutionAdapter,
    close_dashboard_http_sessions,
)
from platform_supervisor.model_relay import (
    KanbanWorkerModelRouter,
    MainBoundaryBoot,
    MainBoundaryCoordinator,
    ModelRelay,
    ModelRouteRegistry,
    RelayOutboxObservationSink,
    SessionModelRouter,
)
from platform_supervisor.model_relay.server import ModelRelayServer
from platform_supervisor.process_manager import (
    ProcessManager,
    harden_process_secret_boundary,
)
from platform_supervisor.platform_control_identity import (
    load_platform_control_identity,
)
from platform_supervisor.fresh_presence import assert_fresh_runtime_home
from platform_supervisor.projection import ProjectionSender
from platform_supervisor.provider_routes import (
    ProviderRouteBindingIdentity,
    ProviderRouteCapsuleCodec,
)
from platform_supervisor.profile_projection import (
    BackendProfileArtifactDownloader,
    ProfileProjectionController,
)
from platform_supervisor.profile_projection_state import (
    ProfileProjectionStateStore,
)
from platform_supervisor.mcp_registry import (
    McpRegistryExecutionGate,
    RuntimeMcpRegistryController,
)
from platform_supervisor.mcp_registry_state import McpRegistryStateStore
from platform_supervisor.settings import SupervisorSettings
from platform_supervisor.security import RuntimeTokenVerifier
from platform_supervisor.sqlite_startup_integrity import SQLiteStartupIntegrityCache
from platform_supervisor.storage import SQLiteStore
from platform_supervisor.storage import (
    RuntimeStorageLifecycle,
    RuntimeStorageLockError,
    StorageLevel,
)


logger = logging.getLogger(__name__)
SIGTERM_RUN_DRAIN_SECONDS = 870
IDLE_SHUTDOWN_CHILD_STOP_SECONDS = 5


def shutdown_child_stop_timeout(*, drained: bool, active_runs: int) -> float:
    """Bound child shutdown once no Run can still be executing.

    An idle Pod still held the PVC flock for up to 30 seconds while Hermes
    children ignored SIGTERM. The next scale-from-zero Pod then failed on
    PERSISTENT_STORAGE_LOCK_HELD and crash-looped. Once the capacity gate is
    empty and no Run context exists, no execution can be lost, so children
    only get a short graceful window before SIGKILL. Any active Run keeps the
    long drain semantics untouched.
    """
    if drained and active_runs == 0:
        return IDLE_SHUTDOWN_CHILD_STOP_SECONDS
    return 30.0


async def verify_sqlite_before_admission(
    store: SQLiteStore,
    integrity_cache: SQLiteStartupIntegrityCache,
) -> bool:
    if integrity_cache.can_reuse():
        mark("sqlite_quick_check_reused")
        return True
    mark("sqlite_quick_check_started")
    passed = await store.quick_check()
    if passed:
        integrity_cache.record_passed()
    mark("sqlite_quick_check_checked")
    return passed


async def start_hermes_until_dashboard_listens(
    process_manager: ProcessManager,
) -> None:
    await process_manager.start()
    mark("hermes_process_started")
    await process_manager.wait_until_dashboard_listens()
    mark("dashboard_listening")


def create_app(settings: SupervisorSettings) -> FastAPI:
    mark("app_create_begin")
    fresh_storage_probe = settings.HERMES_STARTUP_MODE == "fresh_probe"
    health = RuntimeHealth(require_admission_open=False)
    store = SQLiteStore(
        settings.supervisor_database_path,
        group_commit_window_ms=settings.DISPATCH_GROUP_COMMIT_WINDOW_MS,
        busy_timeout_ms=settings.SQLITE_BUSY_TIMEOUT_MS,
    )
    sqlite_integrity_cache = SQLiteStartupIntegrityCache(
        settings.supervisor_database_path,
        settings.RUNTIME_DATA_ROOT / "supervisor.sqlite-integrity.json",
    )
    supervisor_boot_id = f"supervisor-{uuid4()}"
    platform_control_identity = load_platform_control_identity(
        settings,
        supervisor_boot_id=supervisor_boot_id,
    )
    health.mark_control_ready()
    process_manager = ProcessManager(settings)
    main_boundary_boot = MainBoundaryBoot(
        dashboard_boot_id=process_manager.dashboard_boot_id,
        marker_key=process_manager.main_boundary_marker_key,
        boundary_epoch_nonce=process_manager.main_boundary_epoch_nonce,
    )
    control_peers = ActiveRunControlPeerRegistry()
    model_route_registry = ModelRouteRegistry()
    provider_route_kwargs = {}
    if platform_control_identity is not None:
        identity = platform_control_identity.identity
        provider_route_kwargs = {
            "provider_route_codec": ProviderRouteCapsuleCodec(),
            "provider_route_control_keys": (platform_control_identity.control_root_keys),
            "provider_route_binding": ProviderRouteBindingIdentity(
                runtime_id=identity.runtime_id,
                binding_type=identity.binding_type,
                binding_id=identity.binding_id,
                pvc_storage_identity_hash=identity.pvc_storage_identity_hash,
            ),
        }
    session_model_router = SessionModelRouter(
        settings,
        model_route_registry,
        **provider_route_kwargs,
    )
    kanban_model_router = KanbanWorkerModelRouter(
        settings,
        model_route_registry,
        **provider_route_kwargs,
    )
    hermes_adapter = HermesExecutionAdapter(
        settings,
        dashboard_session_token=process_manager.dashboard_session_token,
        model_router=session_model_router,
        control_peers=control_peers,
    )
    profile_projection_state = ProfileProjectionStateStore(
        settings.RUNTIME_DATA_ROOT / "profile-projections.db",
        busy_timeout_ms=settings.SQLITE_BUSY_TIMEOUT_MS,
    )
    mcp_registry_state = McpRegistryStateStore(
        settings.RUNTIME_DATA_ROOT / "mcp-registry.db",
        busy_timeout_ms=settings.SQLITE_BUSY_TIMEOUT_MS,
    )
    profile_artifact_downloader = BackendProfileArtifactDownloader(
        backend_base_url=settings.BACKEND_INTERNAL_BASE_URL,
        api_server_key=settings.API_SERVER_KEY.get_secret_value(),
    )
    asset_config_lock = asyncio.Lock()
    profile_projection = ProfileProjectionController(
        runtime_id=settings.RUNTIME_ID,
        binding_id=settings.RUNTIME_BINDING_ID,
        home=settings.HERMES_HOME,
        state=profile_projection_state,
        dashboard=hermes_adapter,
        downloader=profile_artifact_downloader,
        config_lock=asset_config_lock,
    )
    session_model_router.bind_profile_config_io(
        reader=hermes_adapter.read_profile_config,
        writer=hermes_adapter.write_profile_config,
    )
    kanban_model_router.bind_profile_config_io(
        reader=hermes_adapter.read_profile_config,
        writer=hermes_adapter.write_profile_config,
    )
    capacity = CapacityGate(
        settings.max_inflight_runs,
        busy_code=(
            "PERSONAL_ACTIVE_SESSION_LIMIT"
            if settings.RUNTIME_MODE == "personal"
            else "APPLICATION_CAPACITY_BUSY"
        ),
    )
    connections = ExternalConnectionGate(
        settings.max_external_websockets,
    )
    sessions = SessionManager(
        store,
        runtime_id=settings.RUNTIME_ID,
        max_inflight_runs=settings.max_inflight_runs,
        max_resident_sessions=settings.max_resident_sessions,
        post_run_warm_grace_seconds=(settings.APPLICATION_AGENT_POST_RUN_WARM_GRACE_SECONDS),
        hot_session_limit=(
            settings.APPLICATION_AGENT_HOT_SESSION_LIMIT
            if settings.RUNTIME_MODE == "application"
            else 0
        ),
        hot_session_ttl_seconds=(settings.APPLICATION_AGENT_HOT_SESSION_TTL_SECONDS),
        prepare_callback=hermes_adapter.prepare,
        close_callback=hermes_adapter.close_session,
        profile_refresh_callback=hermes_adapter.refresh_session_profile,
    )
    hermes_adapter.bind_agent_generation_advancer(sessions.advance_agent_generation)

    async def resolve_profile_identity(identity):
        return await profile_projection.resolve_identity(
            identity,
            allow_pending=True,
        )

    execution_identity_gate = McpRegistryExecutionGate(
        state=mcp_registry_state,
        resolver=resolve_profile_identity,
    )

    runs = RunManager(
        store,
        sessions,
        capacity,
        hermes_adapter,
        model_router=session_model_router,
        profile_identity_resolver=execution_identity_gate,
        artifact_workspace_dir=settings.WORKSPACE_DIR,
    )
    profile_projection.bind_applied_sink(runs.wake_queued_sessions)
    mcp_registry = RuntimeMcpRegistryController(
        runtime_id=settings.RUNTIME_ID,
        binding_id=settings.RUNTIME_BINDING_ID,
        state=mcp_registry_state,
        dashboard=hermes_adapter,
        active_run_ids=runs.active_run_ids,
        config_lock=asset_config_lock,
    )
    mcp_registry.bind_terminal_sink(runs.wake_queued_sessions)
    redirect_gate = RedirectDurableGate(
        store=store,
        control_peers=control_peers,
        history_source=hermes_adapter,
        event_fences=runs,
        settle_controller=hermes_adapter,
    )
    main_boundary = MainBoundaryCoordinator(
        gate=redirect_gate,
        route_resolver=RedirectBoundaryRouteResolver(
            store=store,
            control_peers=control_peers,
            boot=main_boundary_boot,
        ),
    )
    redirect_gate.bind_boundary(main_boundary)
    runs.set_redirect_lifecycle(redirect_gate)
    control_peers.set_boundary_observer(redirect_gate)
    channel_fast_lane_v2_router = DurableChannelFastLaneV2Router(store, runs)
    config_controller = RuntimeConfigController(
        home=settings.HERMES_HOME,
        state_path=settings.RUNTIME_DATA_ROOT / "current-config.json",
        initial_revision=settings.RUNTIME_CONFIG_REVISION,
        adapter=hermes_adapter,
        sessions=sessions,
        runtime_id=settings.RUNTIME_ID,
        binding_id=settings.RUNTIME_BINDING_ID,
        api_server_key=settings.API_SERVER_KEY.get_secret_value(),
        backend_base_url=settings.BACKEND_INTERNAL_BASE_URL,
        initial_download_url=(settings.RUNTIME_INITIAL_CONFIG_DOWNLOAD_URL),
        initial_digest=settings.RUNTIME_INITIAL_CONFIG_DIGEST,
    )
    model_relay = ModelRelay(
        settings,
        model_route_registry,
        observation_sink=RelayOutboxObservationSink(store),
        main_boundary=main_boundary,
    )
    session_model_router.bind_provider_route_warmup(model_relay.schedule_provider_warmup)
    model_relay_server = ModelRelayServer(
        settings,
        model_relay,
        model_route_registry,
    )
    projection_sender = ProjectionSender(settings, store)
    admission_audit = AdmissionAuditReporter(
        backend_base_url=settings.BACKEND_INTERNAL_BASE_URL,
        runtime_id=settings.RUNTIME_ID,
        binding_id=settings.RUNTIME_BINDING_ID,
        api_server_key=settings.API_SERVER_KEY.get_secret_value(),
    )
    storage_lifecycle = RuntimeStorageLifecycle(settings.RUNTIME_DATA_ROOT)

    @asynccontextmanager
    async def lifespan(_: FastAPI):
        mark("lifespan_begin")
        reaper_task: asyncio.Task[None] | None = None
        storage_task: asyncio.Task[None] | None = None
        if fresh_storage_probe:
            assert_fresh_runtime_home(settings.HERMES_HOME)
            startup_check = evaluate_startup_resources(
                probe_startup_resources(settings.HERMES_HOME, sqlite_ok=True),
                min_nofile=settings.MIN_NOFILE,
                min_pids=settings.MIN_PIDS,
            )
            if not startup_check.ready:
                health.mark_failed(startup_check.errors[0])
                raise RuntimeError("fresh probe startup failed: " + ",".join(startup_check.errors))
            health.mark_supervisor_ready(startup_check)
            health.mark_hermes_ready()
            mark("fresh_probe_ready")
            try:
                yield
            finally:
                health.mark_draining()
                await capacity.begin_drain("shutdown")
                await admission_audit.close()
                await kanban_model_router.close()
            return
        settings.RUNTIME_DATA_ROOT.mkdir(parents=True, exist_ok=True)
        try:
            storage_lifecycle.acquire()
            storage_lifecycle.ensure_reserve()
            storage_lifecycle.cleanup_stale_upload_parts(
                settings.WORKSPACE_DIR,
            )
            mark("storage_ready")
        except RuntimeStorageLockError as exc:
            health.mark_failed(str(exc))
            raise RuntimeError(f"Supervisor startup gate failed: {exc}") from exc
        except OSError as exc:
            health.mark_failed("PERSISTENT_STORAGE_RESERVE_FAILED")
            storage_lifecycle.close()
            raise RuntimeError(
                "Supervisor startup gate failed: PERSISTENT_STORAGE_RESERVE_FAILED"
            ) from exc
        await store.start()
        mark("sqlite_store_started")
        await profile_projection_state.start()
        mark("projection_state_started")
        await mcp_registry_state.start()
        mark("mcp_state_started")
        health.mark_store_state("migrated")
        await main_boundary.register_boot(main_boundary_boot)
        await config_controller.start()
        mark("config_controller_started")
        hermes_listening_task = asyncio.create_task(
            start_hermes_until_dashboard_listens(process_manager)
        )
        try:
            try:
                startup_check = evaluate_startup_resources(
                    probe_startup_resources(
                        settings.RUNTIME_DATA_ROOT,
                        sqlite_ok=await verify_sqlite_before_admission(
                            store,
                            sqlite_integrity_cache,
                        ),
                    ),
                    min_nofile=settings.MIN_NOFILE,
                    min_pids=settings.MIN_PIDS,
                )
                if not startup_check.ready:
                    health.mark_failed(startup_check.errors[0])
                    await store.close()
                    await profile_projection_state.close()
                    await mcp_registry_state.close()
                    storage_lifecycle.close()
                    raise RuntimeError(
                        "Supervisor startup gate failed: " + ",".join(startup_check.errors)
                    )
                health.mark_supervisor_ready(startup_check)
                mark("supervisor_ready")
                await runs.restore()
                mark("runs_restored")
                await model_relay_server.start()
                mark("model_relay_started")
                model_relay.start_transport_warmup()
                await hermes_listening_task
                await hermes_adapter.health()
                mark("hermes_health_checked")
                await redirect_gate.start()
                projection_sender.start()
                health.mark_hermes_ready()
                mark("hermes_ready")
                sessions.start_background_restore(
                    config_revision=config_controller.current_revision,
                )
                mark("lifespan_startup_complete")
                log_summary(mode=settings.HERMES_STARTUP_MODE)
            except BaseException:
                hermes_listening_task.cancel()
                await asyncio.gather(hermes_listening_task, return_exceptions=True)
                await process_manager.stop(timeout_seconds=IDLE_SHUTDOWN_CHILD_STOP_SECONDS)
                raise

            async def reap_sessions() -> None:
                while True:
                    await asyncio.sleep(30)
                    await sessions.reap()

            async def monitor_storage() -> None:
                while True:
                    snapshot = storage_lifecycle.snapshot()
                    health.update_storage(snapshot)
                    if snapshot.level == StorageLevel.FAILED:
                        await capacity.begin_drain("storage")
                        storage_lifecycle.release_reserve()
                        health.update_storage(storage_lifecycle.snapshot())
                        health.mark_failed("PERSISTENT_STORAGE_CRITICAL_WATERMARK")
                    elif not snapshot.admissions_allowed:
                        await capacity.begin_drain("storage")
                        health.mark_draining()
                    elif (
                        snapshot.level == StorageLevel.NORMAL
                        and "storage" in capacity.drain_reasons
                    ):
                        try:
                            storage_lifecycle.ensure_reserve()
                        except OSError:
                            health.mark_failed("PERSISTENT_STORAGE_RESERVE_FAILED")
                        else:
                            await capacity.cancel_drain("storage")
                            health.update_storage(storage_lifecycle.snapshot())
                            if capacity.draining:
                                health.mark_draining()
                            else:
                                health.mark_hermes_ready()
                    try:
                        now = datetime.now(timezone.utc)
                        session_metrics = await sessions.metrics_snapshot()
                        outbox_metrics = await store.outbox_snapshot()
                        health_snapshot = health.public_snapshot()
                        await store.enqueue_projection(
                            projection_id=(
                                f"health:{settings.RUNTIME_ID}:{int(now.timestamp()) // 30}"
                            ),
                            projection_type="runtime.health",
                            payload={
                                "observed_at": now.isoformat(),
                                "state": health_snapshot["state"],
                                "error_code": health_snapshot["error_code"],
                                "config_revision": (config_controller.current_revision),
                                "config_digest": (config_controller.current_digest),
                                "storage": health_snapshot.get("storage"),
                                "runs": {
                                    "active": capacity.inflight,
                                    "limit": capacity.limit,
                                    "rejected_total": (capacity.rejected_total),
                                },
                                "websockets": {
                                    "active": connections.active,
                                    "limit": connections.limit,
                                    "rejected_total": (connections.rejected_total),
                                },
                                "sessions": session_metrics,
                                "realtime": (runs.realtime_snapshot()),
                                "sqlite": (store.writer_metrics_snapshot()),
                                "outbox": outbox_metrics,
                                "model_relay": (model_relay.metrics_snapshot()),
                            },
                            created_at=now,
                        )
                    except Exception as exc:
                        logger.warning(
                            "Runtime health projection failed",
                            extra={
                                "component": "supervisor",
                                "error_type": type(exc).__name__,
                            },
                        )
                    await asyncio.sleep(30)

            reaper_task = asyncio.create_task(
                reap_sessions(),
                name="runtime-session-reaper",
            )
            storage_task = asyncio.create_task(
                monitor_storage(),
                name="runtime-storage-monitor",
            )
            logger.info(
                "Runtime execution plane is ready",
                extra={"component": "supervisor"},
            )
            yield
        finally:
            shutdown_started = time.monotonic()
            logger.warning(
                "runtime shutdown begin",
                extra={"component": "supervisor"},
            )
            health.mark_draining()
            await capacity.begin_drain("shutdown")
            await connections.close_all(
                code=1012,
                reason="runtime shutting down",
            )
            drained = await capacity.wait_until_empty(
                timeout_seconds=SIGTERM_RUN_DRAIN_SECONDS,
            )
            logger.warning(
                "runtime shutdown drained=%s active_runs=%s elapsed=%.3fs",
                drained,
                runs.active_count,
                time.monotonic() - shutdown_started,
                extra={"component": "supervisor"},
            )
            if drained and runs.active_count == 0:
                # Release the PVC single-writer gate before the graceful
                # close chain. An idle Pod's resident hot sessions each call
                # Dashboard release_live_session during sessions.close(), and
                # that chain held the flock for tens of seconds, making the
                # next scale-from-zero Pod fail on PERSISTENT_STORAGE_LOCK_HELD.
                # With no Run executing there is nothing left to drain:
                # sessions.close() already tolerates a dead Dashboard through
                # return_exceptions, and the later process_manager.stop() is
                # idempotent for an already-stopped process tree.
                await process_manager.stop(
                    timeout_seconds=IDLE_SHUTDOWN_CHILD_STOP_SECONDS,
                )
                logger.warning(
                    "runtime shutdown idle children stopped elapsed=%.3fs",
                    time.monotonic() - shutdown_started,
                    extra={"component": "supervisor"},
                )
                # Release the lock immediately as well: sessions.close() can
                # still spend tens of seconds in Dashboard connector retries
                # even after the children are dead. RuntimeStorageLifecycle
                # close() is idempotent, so the tail of this shutdown chain
                # simply skips the already-released lock.
                storage_lifecycle.close()
                logger.warning(
                    "runtime shutdown storage lock released elapsed=%.3fs",
                    time.monotonic() - shutdown_started,
                    extra={"component": "supervisor"},
                )
            if reaper_task is not None:
                reaper_task.cancel()
                await asyncio.gather(reaper_task, return_exceptions=True)
            if storage_task is not None:
                storage_task.cancel()
                await asyncio.gather(storage_task, return_exceptions=True)
            await runs.close()
            await redirect_gate.close()
            await kanban_model_router.close()
            await admission_audit.close()
            await projection_sender.close()
            await config_controller.close()
            await profile_artifact_downloader.close()
            await sessions.close()
            await hermes_adapter.close()
            await close_dashboard_http_sessions()
            await main_boundary.revoke_boot(main_boundary_boot.dashboard_boot_id)
            await process_manager.stop(
                timeout_seconds=shutdown_child_stop_timeout(
                    drained=drained,
                    active_runs=runs.active_count,
                ),
            )
            await model_relay_server.stop()
            await store.close()
            if drained and runs.active_count == 0:
                sqlite_integrity_cache.record_clean_shutdown()
            await profile_projection_state.close()
            await mcp_registry_state.close()
            storage_lifecycle.close()
            logger.warning(
                "runtime shutdown complete elapsed=%.3fs",
                time.monotonic() - shutdown_started,
                extra={"component": "supervisor"},
            )

    app = FastAPI(
        title="Platform Runtime Supervisor",
        docs_url=None,
        redoc_url=None,
        openapi_url=None,
        lifespan=lifespan,
    )
    app.state.health = health
    app.state.store = store
    app.state.process_manager = process_manager
    app.state.model_route_registry = model_route_registry
    app.state.model_relay = model_relay
    app.state.main_boundary = main_boundary
    app.state.main_boundary_boot = main_boundary_boot
    app.state.redirect_gate = redirect_gate
    app.state.control_peers = control_peers
    app.state.capacity = capacity
    app.state.connections = connections
    app.state.sessions = sessions
    app.state.runs = runs
    app.state.channel_fast_lane_v2_router = channel_fast_lane_v2_router
    app.state.hermes_adapter = hermes_adapter
    app.state.projection_sender = projection_sender
    app.state.admission_audit = admission_audit
    app.state.config_controller = config_controller
    app.state.profile_projection = profile_projection
    app.state.storage_lifecycle = storage_lifecycle
    app.state.supervisor_boot_id = supervisor_boot_id
    app.state.platform_control_identity = platform_control_identity

    @app.get("/control-readyz")
    async def control_ready() -> dict[str, object]:
        return {
            "control_ready": health.control_ready,
            "supervisor_boot_id": supervisor_boot_id,
            "store_state": health.store_state,
        }

    token_verifier = RuntimeTokenVerifier(
        runtime_id=settings.RUNTIME_ID,
        secret=settings.API_SERVER_KEY.get_secret_value(),
    )
    edge_dependencies = RuntimeEdgeDependencies(
        runtime_id=settings.RUNTIME_ID,
        binding_id=settings.RUNTIME_BINDING_ID,
        api_server_key=settings.API_SERVER_KEY.get_secret_value(),
        token_verifier=token_verifier,
        store=store,
        sessions=sessions,
        runs=runs,
        health=health,
        connections=connections,
        config=config_controller,
        session_control=hermes_adapter,
        workspace_dir=settings.WORKSPACE_DIR,
        storage_lifecycle=storage_lifecycle,
        admission_audit=admission_audit,
        kanban_model_router=kanban_model_router,
        channel_fast_lane_v2_router=channel_fast_lane_v2_router,
        profile_projection=profile_projection,
        mcp_registry=mcp_registry,
    )
    if not fresh_storage_probe:
        attach_runtime_routes(app, edge_dependencies)
        attach_channel_fast_lane(app, edge_dependencies)

    @app.get("/healthz")
    async def healthz() -> dict[str, object]:
        return health.public_snapshot()

    @app.get("/livez")
    async def livez() -> dict[str, object]:
        return {
            "live": True,
            "supervisor_boot_id": supervisor_boot_id,
        }

    @app.get("/readyz")
    async def readyz() -> JSONResponse:
        status_code = 200 if health.ready else 503
        return JSONResponse(health.public_snapshot(), status_code=status_code)

    mark("app_created")
    return app


def run() -> None:
    harden_process_secret_boundary()
    settings = SupervisorSettings()
    # containerd commonly supplies a conservative soft NOFILE value while
    # retaining a much larger hard limit. Raise only within that inherited
    # boundary; the startup gate still fails closed when the hard limit is low.
    raise_nofile_soft_limit(settings.MIN_NOFILE)
    uvicorn.run(
        create_app(settings),
        host=settings.RUNTIME_EDGE_HOST,
        port=settings.RUNTIME_EDGE_PORT,
        access_log=False,
        proxy_headers=True,
    )
