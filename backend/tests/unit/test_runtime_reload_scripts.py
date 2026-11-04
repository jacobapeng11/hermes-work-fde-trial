from pathlib import Path
import stat
import subprocess


REPOSITORY_ROOT = Path(__file__).resolve().parents[3]


def test_reload_shell_wrappers_are_strict_and_syntax_valid() -> None:
    scripts = [
        REPOSITORY_ROOT / "scripts/build-runtime-code-image-dev.sh",
        REPOSITORY_ROOT / "scripts/build-runtime-v3-image-dev.sh",
        REPOSITORY_ROOT / "scripts/cleanup-one-runtime-config-residue.sh",
        REPOSITORY_ROOT / "scripts/reload-one-runtime-from-env-dev.sh",
        REPOSITORY_ROOT / "scripts/reload-all-runtimes-from-env-dev.sh",
        REPOSITORY_ROOT / "scripts/restart-project-from-env.sh",
        REPOSITORY_ROOT / "scripts/rebuild-runtime-image-and-restart.sh",
        REPOSITORY_ROOT / "scripts/rebuild-runtime-test.sh",
        REPOSITORY_ROOT / "scripts/rebuild-runtime-v3-image-and-restart.sh",
        REPOSITORY_ROOT / "scripts/smoke-fresh-runtime-edge-session.sh",
        REPOSITORY_ROOT / "scripts/smoke-one-runtime-edge-session.sh",
    ]
    for script in scripts:
        source = script.read_text(encoding="utf-8")
        assert "set -euo pipefail" in source
        assert script.stat().st_mode & stat.S_IXUSR
        completed = subprocess.run(
            ["bash", "-n", str(script)],
            text=True,
            capture_output=True,
            check=False,
        )
        assert completed.returncode == 0, completed.stderr


def test_project_restart_entries_keep_mode_as_the_only_public_argument() -> None:
    restart = (REPOSITORY_ROOT / "scripts/restart-project-from-env.sh").read_text(
        encoding="utf-8"
    )
    rebuild = (
        REPOSITORY_ROOT / "scripts/rebuild-runtime-image-and-restart.sh"
    ).read_text(encoding="utf-8")

    assert 'case "$mode" in' in restart
    assert 'dev|prod)' in restart
    assert 'build-public-prod.sh' in restart
    assert '"--concurrency 4' not in restart
    assert '"--stop-concurrency 8' not in restart
    assert 'build-runtime-code-image-dev.sh' in rebuild
    assert 'restart-project-from-env.sh' in rebuild
    assert "--reexecute-completed" in restart
    assert "project-lifecycle.sh" in restart
    assert "switch-public-nginx-mode.sh\" dev" in restart
    assert "status-public-prod.sh" in restart
    assert restart.index("status-public-prod.sh") < restart.index("reload-all-runtimes-from-env-dev.sh")
    assert "last-rebuild.json" in rebuild


def test_v3_rebuild_uses_complete_overlay_and_shared_restart_entrypoint() -> None:
    builder = (REPOSITORY_ROOT / "scripts/build-publish-runtime-v3-image-dev.py").read_text(
        encoding="utf-8"
    )
    rebuild = (REPOSITORY_ROOT / "scripts/rebuild-runtime-v3-image-and-restart.sh").read_text(
        encoding="utf-8"
    )

    assert "Dockerfile.runtime-supervisor-overlay" in builder
    assert '"hermes.platform.build-strategy": "complete-runtime-overlay"' in builder
    assert "base-image-immutable-reference" in builder
    assert "hermes_source_unchanged" in builder
    assert "build-runtime-v3-image-dev.sh" in rebuild
    assert "restart-project-from-env.sh" in rebuild
    assert "last-v3-rebuild.json" in rebuild
    assert "build-runtime-code-image-dev.sh" not in rebuild


def test_project_restart_records_failure_safe_receipt_and_serializes_runs() -> None:
    restart = (REPOSITORY_ROOT / "scripts/restart-project-from-env.sh").read_text(
        encoding="utf-8"
    )
    rebuild = (
        REPOSITORY_ROOT / "scripts/rebuild-runtime-image-and-restart.sh"
    ).read_text(encoding="utf-8")
    lifecycle = (REPOSITORY_ROOT / "scripts/lib/project-lifecycle.sh").read_text(
        encoding="utf-8"
    )

    assert "project_lifecycle_acquire_lock" in restart
    assert "project_lifecycle_acquire_lock" in rebuild
    assert "project_lifecycle_write_receipt" in restart
    assert "project_lifecycle_write_receipt" in rebuild
    assert "flock -w" in lifecycle
    assert '"elapsed_seconds"' in lifecycle


def test_single_reload_uses_common_backend_projection_checkpoint_and_live_verify() -> None:
    source = (REPOSITORY_ROOT / "scripts/reload-one-runtime-from-env-dev.py").read_text(
        encoding="utf-8"
    )

    assert "runtime-image-lifecycle.lock" in source
    assert "runtime_image_preflight.py" in source
    assert '["isolate", *selector]' in source
    assert '["stop", *selector]' in source
    assert '["reload", *selector]' in source
    assert '["verify", *selector]' in source
    assert '["mark-failed", *selector]' in source
    assert "RuntimeCutoverCheckpointStore" in source
    assert '"--force"' in source
    assert '"--env-file",\n            str(env_file),' in source
    assert "_control(command, env_file=env_file)" in source
    assert "_control(reload_command, env_file=env_file)" in source
    assert "deepcopy" not in source


def test_fleet_reload_keeps_freeze_until_all_live_verification_passes() -> None:
    source = (REPOSITORY_ROOT / "scripts/reload-all-runtimes-from-env-dev.py").read_text(
        encoding="utf-8"
    )

    assert "runtime_image_preflight.py" in source

    audit = source.index("_control(_fleet_audit_command(args, checkpoint=True))")
    freeze = source.index('"freeze",')
    stop = source.index("pending_stop,")
    switch = source.index('"switch-fleet"')
    deferred_reload = source.index("deferred_pending,")
    eager_reload = source.index("eager_pending,")
    verify = source.index("key: json.loads(output)")
    unfreeze = source.index('["unfreeze", "--maintenance-epoch"')
    assert audit < freeze < stop < switch < deferred_reload < eager_reload < verify < unfreeze
    assert 'stage="failed"' in source
    assert 'status="failed"' in source
    assert "--supersede-failed-target" in source
    assert "--force" in source
    assert '"--delete-pods",' in source
    assert '"--fast-test-mode",' in source
    assert '"--timeout"' in source
    assert '"30"' in source
    assert "task-kill-timeout" not in source
    assert "delete-grace" not in source
    assert "stop-concurrency" in source
    assert "activation-concurrency" in source
    assert "--deferred" in source
    assert "--allow-absent" in source
    assert "activation_plan" in source
    assert "reexecute-completed" in source
    assert "--from-prod" in source
    assert '"assert-frozen"' in source
    assert '"maintenance-status"' in source
    assert '"--allow-unavailable"' in source
    assert "stale failed checkpoint" in source
    assert '"failed-stale"' in source
    assert "another Runtime maintenance epoch is active" in source
    assert "rollback" not in source.lower()


def test_exact_task_stop_falls_back_to_verified_pid_when_ctr_stalls() -> None:
    source = (REPOSITORY_ROOT / "backend/scripts/runtime_code_reload.py").read_text(
        encoding="utf-8"
    )

    assert 'signal_method = "ctr-task" if task_present else "verified-pid"' in source
    assert "containerd can transiently stall" in source


def test_fleet_session_permission_repair_runs_only_after_all_pods_are_zero() -> None:
    source = (REPOSITORY_ROOT / "scripts/reload-all-runtimes-from-env-dev.py").read_text(
        encoding="utf-8"
    )

    stop = source.index('_parallel(bindings, "stop", args.concurrency, extra=stop_extra)')
    repair = source.index('"repair-session-permissions"')
    restart = source.index("_backend_restart_command(args.backend_lifecycle)")
    reload_runtime = source.index('"reload",\n            args.concurrency')
    unfreeze = source.index('["unfreeze", "--maintenance-epoch"')

    assert stop < repair < restart < reload_runtime < unfreeze
    assert "--repair-session-permissions" in source
    assert '"permissions_repaired"' in source


def test_backend_reload_verifier_checks_live_image_id_not_only_manifest() -> None:
    source = (REPOSITORY_ROOT / "backend/scripts/runtime_code_reload.py").read_text(
        encoding="utf-8"
    )

    assert "containerStatuses" in source
    assert 'status.get("imageID")' in source
    assert 'status.get("name") == "user-container"' in source
    assert "require_live_image_matches_target" in source
    assert "platform-control Secret identity does not match Binding" in source
    assert 'env.get("HERMES_STARTUP_MODE") != "steady_state"' in source


def test_legacy_config_cleanup_is_one_time_and_fails_closed_on_pvc_paths() -> None:
    source = (
        REPOSITORY_ROOT / "backend/scripts/runtime_legacy_config_cleanup.py"
    ).read_text(encoding="utf-8")

    assert "runtime-image-lifecycle.lock" in source
    assert "--apply" in source
    assert "set(old_files) - set(current_files)" in source
    assert "Runtime PVC did not reach strict Pod/KService zero" in source
    assert "legacy cleanup refuses symbolic links" in source
    assert "state.db" not in source


def test_runtime_edge_interface_smoke_uses_live_edge_without_platform_login_token() -> None:
    source = (
        REPOSITORY_ROOT / "backend/scripts/smoke_runtime_edge_session.py"
    ).read_text(encoding="utf-8")

    assert "app.dependency_overrides[get_principal]" in source
    assert '"/api/v1/user/runtime-edge/prepare"' in source
    assert '"session.prepare"' in source
    assert '"run.submit"' in source
    assert '"chat.message.completed"' in source
    assert '"chat.done"' in source
    assert "create_access_token" not in source
    assert "Authorization" not in source
    assert "--runtime-edge-base" in source
    assert "proxy=None" in source
    assert "asyncio.run(engine.dispose())" not in source


def test_fresh_runtime_interface_smoke_uses_sso_provisioning_and_published_bootstrap() -> None:
    source = (
        REPOSITORY_ROOT / "backend/scripts/smoke_fresh_runtime_edge_session.py"
    ).read_text(encoding="utf-8")

    assert "SsoIdentityService" in source
    assert '"/api/v1/user/runtime-workspace/bootstrap"' in source
    assert "RuntimeBootstrapIntent.status == \"completed\"" in source
    assert "run_websocket_conversation" in source
    assert "create_access_token" not in source
    assert "Authorization" not in source
    assert "asyncio.run(engine.dispose())" not in source


def test_fresh_runtime_interface_smoke_forces_fail_closed_cleanup() -> None:
    smoke_source = (
        REPOSITORY_ROOT / "backend/scripts/smoke_fresh_runtime_edge_session.py"
    ).read_text(encoding="utf-8")
    cleanup_source = (
        REPOSITORY_ROOT / "backend/scripts/fresh_runtime_smoke_cleanup.py"
    ).read_text(encoding="utf-8")

    assert "cleanup_fresh_runtime" in smoke_source
    assert "finally:" in smoke_source
    assert "--retain" not in smoke_source
    assert "fresh_runtime_smoke" in smoke_source
    assert "cleanup_completed" in cleanup_source
    assert "cleanup_failed" in cleanup_source
    assert "delete_persistent_volume_claim" in cleanup_source
    assert "delete_persistent_volume" in cleanup_source
    assert "delete_kservice" in cleanup_source
    assert "RuntimePlatformControlKey" in cleanup_source
    assert "RuntimeBootstrapIntent" in cleanup_source
    assert "PersonalConfigBundleArtifact" in cleanup_source
    assert "ExpertPackage" in cleanup_source
    assert "origin_runtime_workspace_id" in cleanup_source
    assert "fresh smoke expert package has external references" in cleanup_source
    assert "legacy" not in cleanup_source.lower()


def test_fresh_runtime_cleanup_does_not_expand_model_usage_by_legacy_run_id() -> None:
    cleanup_source = (
        REPOSITORY_ROOT / "backend/scripts/fresh_runtime_smoke_cleanup.py"
    ).read_text(encoding="utf-8")

    assert 'if table.name == "model_usage_log":' in cleanup_source
    assert 'if column_name != "run_id"' in cleanup_source
    assert 'ModelUsageLog.runtime_workspace_id == profile_id' in cleanup_source
