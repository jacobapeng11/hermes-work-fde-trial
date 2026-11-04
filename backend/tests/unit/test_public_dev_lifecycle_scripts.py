import os
import subprocess
from pathlib import Path


ROOT = Path(__file__).resolve().parents[3]


def _run_private_env_check(path: Path) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        [
            "bash",
            "-c",
            'source "$1"; hermes_require_private_env_file "$2"',
            "_",
            str(ROOT / "scripts" / "lib" / "private-env-file.sh"),
            str(path),
        ],
        cwd=ROOT,
        capture_output=True,
        text=True,
    )


def test_backend_env_permission_gate_accepts_owner_only_modes(tmp_path: Path) -> None:
    env_file = tmp_path / ".env"
    env_file.write_text("TOP_SECRET=must-not-leak\n")

    for mode in (0o400, 0o600):
        env_file.chmod(mode)
        result = _run_private_env_check(env_file)
        assert result.returncode == 0, result.stderr


def test_backend_env_permission_gate_rejects_group_or_other_access(tmp_path: Path) -> None:
    env_file = tmp_path / ".env"
    secret = "TOP_SECRET=must-not-leak"
    env_file.write_text(f"{secret}\n")

    for mode in (0o644, 0o660):
        env_file.chmod(mode)
        result = _run_private_env_check(env_file)
        assert result.returncode != 0
        assert "chmod 600" in result.stderr
        assert secret not in result.stderr
        assert secret not in result.stdout


def test_lifecycle_library_validates_ownership_and_releases_ports() -> None:
    content = (ROOT / "scripts" / "lib" / "public-dev-lifecycle.sh").read_text()

    assert "command -v flock" in content
    assert "command -v ss" in content
    assert '/proc/$pid/cwd' in content
    assert '/proc/$pid/cmdline' in content
    assert 'ps -o pgid=' in content
    assert 'kill -TERM -- "-$pgid"' in content
    assert 'kill -KILL -- "-$pgid"' in content
    assert "public_dev_process_belongs" in content
    assert "public_dev_load_backend_env" in content
    assert "public_dev_require_real_backend_modes" in content
    assert "public_dev_require_public_web_bases" in content
    assert "public_dev_require_enterprise_knowledge_bundle" in content
    assert "public_dev_stop_service" in content
    assert "public_dev_resolve_enterprise_knowledge" not in content
    assert "public_dev_require_enterprise_knowledge_config" not in content
    assert "public_dev_listener_pids" in content
    assert "The lifecycle PID file is the" in content
    assert 'for orphan_pid in $(public_dev_background_pids' not in content
    assert "return 0" in content
    assert "public_dev_wait_port_free" in content
    assert "backend/.venv/bin/uvicorn" in content
    assert '".venv/bin/uvicorn"' in content
    assert 'case "$cwd" in' in content
    assert '"$service_dir"|"$service_dir"/*' in content
    assert "lsof -nP" not in content
    assert "fuser -k" not in content
    assert "pkill" not in content


def test_public_dev_port_probe_distinguishes_local_listener_addresses(
    tmp_path: Path,
) -> None:
    fake_bin = tmp_path / "bin"
    fake_bin.mkdir()
    fake_ss = fake_bin / "ss"
    fake_ss.write_text("#!/usr/bin/env bash\nprintf '%s\\n' \"${SS_OUTPUT:-}\"\n")
    fake_ss.chmod(0o755)
    command = f"""
set -euo pipefail
source {ROOT / 'scripts' / 'lib' / 'public-dev-lifecycle.sh'}
public_dev_host_port_is_free 127.0.0.1 8011
"""
    base_env = {**os.environ, "PATH": f"{fake_bin}:{os.environ['PATH']}"}

    external = subprocess.run(
        ["bash", "-c", command],
        env={
            **base_env,
            "SS_OUTPUT": "LISTEN 0 511 192.0.2.15:8011 0.0.0.0:*",
        },
        capture_output=True,
        text=True,
        check=False,
    )
    loopback = subprocess.run(
        ["bash", "-c", command],
        env={
            **base_env,
            "SS_OUTPUT": "LISTEN 0 511 127.0.0.1:8011 0.0.0.0:*",
        },
        capture_output=True,
        text=True,
        check=False,
    )
    wildcard = subprocess.run(
        ["bash", "-c", command],
        env={
            **base_env,
            "SS_OUTPUT": "LISTEN 0 511 0.0.0.0:8011 0.0.0.0:*",
        },
        capture_output=True,
        text=True,
        check=False,
    )

    assert external.returncode == 0, external.stderr
    assert loopback.returncode != 0
    assert wildcard.returncode != 0


def test_public_prod_topology_is_derived_from_environment_from_customer_config(tmp_path: Path) -> None:
    env_file = tmp_path / "env"
    env_file.write_text(
        "BACKEND_HOST=127.0.0.1\nBACKEND_LOCAL_HOST=127.0.0.1\n"
        "BACKEND_PORT=8011\nPORT_BACKEND=8011\n"
        "PUBLIC_API_BASE=http://192.0.2.84:28011\n"
        "PUBLIC_WEB_BASE=https://agent-84.example.test:28443\n"
        "PUBLIC_ADMIN_BASE=https://agent-84.example.test:28443/admin\n"
    )
    env_file.chmod(0o600)
    command = f"source {ROOT / 'scripts/lib/public-prod-lifecycle.sh'}; public_prod_load_backend_env {env_file}; public_prod_require_topology"
    result = subprocess.run(["bash", "-c", command], cwd=ROOT, capture_output=True, text=True)
    assert result.returncode == 0, result.stderr


def test_public_prod_topology_rejects_loopback_runtime_api(tmp_path: Path) -> None:
    env_file = tmp_path / "env"
    env_file.write_text(
        "BACKEND_HOST=127.0.0.1\nBACKEND_LOCAL_HOST=127.0.0.1\n"
        "BACKEND_PORT=8011\nPORT_BACKEND=8011\n"
        "PUBLIC_API_BASE=http://127.0.0.1:8011\n"
        "PUBLIC_WEB_BASE=https://agent-84.example.test\n"
        "PUBLIC_ADMIN_BASE=https://agent-84.example.test/admin\n"
    )
    env_file.chmod(0o600)
    command = f"source {ROOT / 'scripts/lib/public-prod-lifecycle.sh'}; public_prod_load_backend_env {env_file}; public_prod_require_topology"
    result = subprocess.run(["bash", "-c", command], cwd=ROOT, capture_output=True, text=True)
    assert result.returncode != 0
    assert "reachable from Runtime Pods" in result.stderr


def test_public_prod_nginx_gate_matches_configured_listener_and_server_name(tmp_path: Path) -> None:
    env_file = tmp_path / "env"
    env_file.write_text(
        "BACKEND_HOST=127.0.0.1\nBACKEND_LOCAL_HOST=127.0.0.1\n"
        "BACKEND_PORT=8011\nPORT_BACKEND=8011\n"
        "PUBLIC_API_BASE=http://192.0.2.84:28011\n"
        "PUBLIC_WEB_BASE=https://agent-84.example.test:28443\n"
        "PUBLIC_ADMIN_BASE=https://agent-84.example.test:28443/admin\n"
        "PUBLIC_NGINX_LISTEN_HOST=agent-84.example.test\n"
        "PUBLIC_NGINX_API_LISTEN_HOST=192.0.2.84\n"
        f"PUBLIC_NGINX_CONFIG={tmp_path / 'nginx.conf'}\n"
    )
    env_file.chmod(0o600)
    (tmp_path / "nginx.conf").write_text(
        "listen 192.0.2.84:28011;\nlisten agent-84.example.test:28443;\n"
        "server_name agent-84.example.test;\n"
    )
    fake_bin = tmp_path / "bin"
    fake_bin.mkdir()
    nginx = fake_bin / "nginx"
    nginx.write_text("#!/usr/bin/env bash\nexit 0\n")
    nginx.chmod(0o755)
    command = f"source {ROOT / 'scripts/lib/public-prod-lifecycle.sh'}; public_prod_load_backend_env {env_file}; public_prod_require_topology; public_prod_require_nginx_config"
    result = subprocess.run(
        ["bash", "-c", command],
        cwd=ROOT,
        env={**os.environ, "PATH": f"{fake_bin}:{os.environ['PATH']}"},
        capture_output=True,
        text=True,
    )
    assert result.returncode == 0, result.stderr


def test_public_dev_scripts_share_one_lifecycle_lock_and_cleanup_contract() -> None:
    start = (ROOT / "scripts" / "start-public-dev.sh").read_text()
    stop = (ROOT / "scripts" / "stop-public-dev.sh").read_text()
    restart = (ROOT / "scripts" / "restart-public-dev.sh").read_text()
    restart_backend = (ROOT / "scripts" / "restart-public-backend-dev.sh").read_text()

    for content in (start, stop, restart):
        assert "scripts/lib/public-dev-lifecycle.sh" in content
        assert "public_dev_load_backend_env" in content
        assert "scripts/lib/public-mode-handoff.sh" in content
    assert "public_dev_acquire_lock" in start
    assert 'echo "$$"' in start
    assert 'echo "$!" > "$RUNTIME_DIR/backend.pid"' not in start
    assert 'echo "$!" > "$RUNTIME_DIR/user-web.pid"' not in start
    assert 'echo "$!" > "$RUNTIME_DIR/admin-web.pid"' not in start
    assert "public_dev_acquire_lock" in stop
    assert "public_dev_acquire_lock" in restart
    assert "PUBLIC_DEV_LOCK_HELD=1" in restart
    assert "public_mode_stop_registered_full_dev" in stop
    assert 'public_dev_stop_service "enterprise-knowledge-data"' not in stop
    assert 'public_dev_stop_service "backend"' not in stop
    assert "public_dev_assert_ports_free" not in stop
    assert "public_dev_force_runtime_zero" not in stop
    assert "public_mode_switch_nginx prod" in stop
    assert "stop_status=1" in stop
    assert restart.index("stop-public-dev.sh") < restart.index("start-public-dev.sh")
    assert "INIT_DB=0 WARMUP_RUNTIME=0" in restart
    assert "public_dev_resolve_ports" in restart
    assert "delete pod" not in restart
    assert "rollback_partial_start" in start
    assert "public_dev_require_real_backend_modes" in start
    assert "runtime_infra_probe.py" in start
    assert start.index("public_mode_prepare_dev") < start.index(
        "runtime_infra_probe.py"
    )
    assert "--timeout-graceful-shutdown 10" in start
    assert "--timeout-graceful-shutdown 10 --reload" in restart_backend
    assert 'http://${ADMIN_WEB_HOST}:${ADMIN_WEB_PORT}/admin/' in start
    assert start.index("runtime_infra_probe.py") < start.index(
        "startup_transaction_active=1"
    )
    assert "/api/v1/readyz" in start
    assert "runtime_infra_probe.py" not in stop
    assert "REAL_BACKEND_MODE" not in start
    assert "MODEL_PROXY_FORWARD_MODE" not in start
    assert "stop-public-dev.sh" in start
    assert 'INIT_DB="${INIT_DB:-0}"' in start
    assert 'if [[ "$INIT_DB" == "1" ]]' in start
    assert "bash scripts/init-db.sh" in start
    assert "scripts/publish-enterprise-knowledge-bundle.sh" in start
    assert "enterprise-knowledge-data.pid" not in start
    assert "check-enterprise-knowledge-data-service.sh" in start
    assert "init-db.sh" not in stop
    assert "alembic" not in stop.lower()
    assert "scripts.seed" not in stop


def test_detached_public_services_do_not_inherit_lifecycle_locks() -> None:
    scripts = (
        ROOT / "scripts" / "start-public-dev.sh",
        ROOT / "scripts" / "restart-public-backend-dev.sh",
        ROOT / "scripts" / "restart-public-prod.sh",
    )

    for path in scripts:
        content = path.read_text()
        for descriptor in (7, 8, 9):
            assert f"exec {descriptor}>&-" in content, path.name


def test_public_mode_handoff_switches_only_verified_services_without_database_or_runtime_changes() -> None:
    handoff = (ROOT / "scripts" / "lib" / "public-mode-handoff.sh").read_text()
    prod = (ROOT / "scripts" / "restart-public-prod.sh").read_text()

    assert "public_mode_prepare_dev" in handoff
    assert "public_mode_prepare_prod" in handoff
    assert "public_prod_stop_backend" in handoff
    assert "public_mode_stop_registered_dev_service" in handoff
    assert "public_dev_wait_port_free" in handoff
    assert "public_dev_listener_pids" in handoff
    assert "public_frontend_dev_stop" in handoff
    assert "switch-public-nginx-mode.sh" in handoff
    assert "init-db.sh" not in handoff
    assert "public_dev_force_runtime_zero" not in handoff
    assert "pkill" not in handoff
    stop_full_dev = handoff.split("public_mode_stop_full_dev()", 1)[1].split(
        "public_mode_stop_prod_backend()", 1
    )[0]
    assert stop_full_dev.index("public_dev_wait_port_free") < stop_full_dev.index(
        "public_dev_assert_ports_free"
    )
    assert "public_mode_prepare_prod" in prod
    assert prod.index("public_prod_stop_backend") < prod.index(
        "public_mode_prepare_prod"
    )


def test_public_dev_has_no_retired_upgrade_worker_process() -> None:
    start = (ROOT / "scripts" / "start-public-dev.sh").read_text()
    lifecycle = (ROOT / "scripts" / "lib" / "public-dev-lifecycle.sh").read_text()

    assert "hermes_upgrade_worker" not in start
    assert "upgrade-worker" not in lifecycle


def test_public_dev_prod_backend_guard_fails_closed_before_lifecycle_actions(
    tmp_path: Path,
) -> None:
    scripts_dir = tmp_path / "scripts"
    scripts_dir.mkdir()
    status_script = scripts_dir / "status-public-prod.sh"
    status_script.write_text("#!/usr/bin/env bash\nexit 0\n")
    command = f"""
set -euo pipefail
ROOT_DIR="$1"
source {ROOT / 'scripts' / 'lib' / 'public-dev-lifecycle.sh'}
public_dev_require_prod_backend_stopped
echo lifecycle-action-reached
"""

    result = subprocess.run(
        ["bash", "-c", command, "_", str(tmp_path)],
        capture_output=True,
        text=True,
        check=False,
    )

    assert result.returncode != 0
    assert "formal public-prod Backend is running" in result.stderr
    assert "lifecycle-action-reached" not in result.stdout


def test_public_dev_prod_backend_guard_allows_when_formal_backend_is_stopped(
    tmp_path: Path,
) -> None:
    scripts_dir = tmp_path / "scripts"
    scripts_dir.mkdir()
    status_script = scripts_dir / "status-public-prod.sh"
    status_script.write_text("#!/usr/bin/env bash\nexit 1\n")
    command = f"""
set -euo pipefail
ROOT_DIR="$1"
source {ROOT / 'scripts' / 'lib' / 'public-dev-lifecycle.sh'}
public_dev_require_prod_backend_stopped
echo lifecycle-action-reached
"""

    result = subprocess.run(
        ["bash", "-c", command, "_", str(tmp_path)],
        capture_output=True,
        text=True,
        check=False,
    )

    assert result.returncode == 0, result.stderr
    assert "lifecycle-action-reached" in result.stdout


def test_public_dev_prod_backend_guard_blocks_unverified_prod_pid_record(
    tmp_path: Path,
) -> None:
    scripts_dir = tmp_path / "scripts"
    scripts_dir.mkdir()
    status_script = scripts_dir / "status-public-prod.sh"
    status_script.write_text("#!/usr/bin/env bash\nexit 1\n")
    runtime_dir = tmp_path / ".runtime" / "public-prod"
    runtime_dir.mkdir(parents=True)
    (runtime_dir / "backend.pid").write_text("99999999")
    command = f"""
set -euo pipefail
ROOT_DIR="$1"
source {ROOT / 'scripts' / 'lib' / 'public-dev-lifecycle.sh'}
public_dev_require_prod_backend_stopped
echo lifecycle-action-reached
"""

    result = subprocess.run(
        ["bash", "-c", command, "_", str(tmp_path)],
        capture_output=True,
        text=True,
        check=False,
    )

    assert result.returncode != 0
    assert "PID record exists but cannot be verified" in result.stderr
    assert "lifecycle-action-reached" not in result.stdout


def test_public_dev_frontend_overlay_guard_blocks_live_owned_pid(
    tmp_path: Path,
) -> None:
    runtime_dir = tmp_path / ".runtime" / "public-frontends-dev"
    runtime_dir.mkdir(parents=True)
    (runtime_dir / "user-web.pid").write_text(str(os.getpid()))
    command = f"""
set -euo pipefail
ROOT_DIR="$1"
USER_WEB_HOST=127.0.0.1
ADMIN_WEB_HOST=127.0.0.1
USER_WEB_PORT=15175
ADMIN_WEB_PORT=15174
source {ROOT / 'scripts' / 'lib' / 'public-dev-lifecycle.sh'}
public_dev_frontend_overlay_process_belongs() {{ return 0; }}
public_dev_require_frontend_overlay_stopped
echo lifecycle-action-reached
"""

    result = subprocess.run(
        ["bash", "-c", command, "_", str(tmp_path)],
        capture_output=True,
        text=True,
        check=False,
    )

    assert result.returncode != 0
    assert "public frontend dev overlay is running" in result.stderr
    assert "lifecycle-action-reached" not in result.stdout


def test_public_dev_frontend_overlay_guard_ignores_dead_stale_pid(
    tmp_path: Path,
) -> None:
    runtime_dir = tmp_path / ".runtime" / "public-frontends-dev"
    runtime_dir.mkdir(parents=True)
    (runtime_dir / "user-web.pid").write_text("99999999")
    command = f"""
set -euo pipefail
ROOT_DIR="$1"
USER_WEB_HOST=127.0.0.1
ADMIN_WEB_HOST=127.0.0.1
USER_WEB_PORT=15175
ADMIN_WEB_PORT=15174
source {ROOT / 'scripts' / 'lib' / 'public-dev-lifecycle.sh'}
public_dev_listener_pids() {{ return 0; }}
public_dev_require_frontend_overlay_stopped
echo lifecycle-action-reached
"""

    result = subprocess.run(
        ["bash", "-c", command, "_", str(tmp_path)],
        capture_output=True,
        text=True,
        check=False,
    )

    assert result.returncode == 0, result.stderr
    assert "lifecycle-action-reached" in result.stdout


def test_public_dev_frontend_overlay_guard_blocks_owned_orphan_listener(
    tmp_path: Path,
) -> None:
    command = f"""
set -euo pipefail
ROOT_DIR="$1"
USER_WEB_HOST=127.0.0.1
ADMIN_WEB_HOST=127.0.0.1
USER_WEB_PORT=15175
ADMIN_WEB_PORT=15174
source {ROOT / 'scripts' / 'lib' / 'public-dev-lifecycle.sh'}
public_dev_listener_pids() {{ echo {os.getpid()}; }}
public_dev_frontend_overlay_process_belongs() {{ return 0; }}
public_dev_require_frontend_overlay_stopped
echo lifecycle-action-reached
"""

    result = subprocess.run(
        ["bash", "-c", command, "_", str(tmp_path)],
        capture_output=True,
        text=True,
        check=False,
    )

    assert result.returncode != 0
    assert "orphan listener" in result.stderr
    assert "lifecycle-action-reached" not in result.stdout


def test_public_prod_scripts_are_static_only_and_use_environment_contract() -> None:
    build = (ROOT / "scripts" / "build-public-prod.sh").read_text()
    restart = (ROOT / "scripts" / "restart-public-prod.sh").read_text()
    stop = (ROOT / "scripts" / "stop-public-prod.sh").read_text()
    status = (ROOT / "scripts" / "status-public-prod.sh").read_text()
    lifecycle = (ROOT / "scripts" / "lib" / "public-prod-lifecycle.sh").read_text()
    env_template = (ROOT / "backend" / "env.sample").read_text()

    assert 'PUBLIC_PROD_ENV_FILE' not in lifecycle
    assert 'env_file="${1:-$ROOT_DIR/backend/.env}"' in lifecycle
    assert "private-env-file.sh" in lifecycle
    assert 'hermes_require_private_env_file "$env_file"' in lifecycle
    assert 'export INIT_DB=0' in lifecycle
    assert 'must be an absolute HTTP(S) URL' in lifecycle
    assert 'must be reachable from Runtime Pods' in lifecycle
    assert 'PUBLIC_RELEASE_ROOT="${PUBLIC_RELEASE_ROOT:-}"' in lifecycle
    assert "public_prod_require_enterprise_knowledge_data_service" in lifecycle
    assert "public_prod_require_enterprise_knowledge_data_service" in restart
    assert "public_prod_stop_backend" in stop
    assert "public_prod_backend_status" in status
    assert "public_mode_prepare_prod" in restart
    assert "public_prod_dev_backend_is_running" not in restart
    assert "Nginx, static files, database, and dev services were not changed" in stop
    assert 'npm run build' in build
    assert 'public-frontend-auth-config.sh' in build
    assert 'public_frontend_require_legacy_sso_config' in build
    assert 'VITE_LEGACY_SSO_LOGIN_BASE_URL="$VITE_LEGACY_SSO_LOGIN_BASE_URL"' in build
    assert 'VITE_LEGACY_SSO_APP_NAME="$VITE_LEGACY_SSO_APP_NAME"' in build
    assert 'rev-parse HEAD' in build
    assert '.hermes-release-revision' in build
    assert 'PUBLIC_RELEASE_REVISION' in lifecycle
    assert 'npm run dev' not in build
    assert 'init-db.sh' not in build
    assert 'npm run dev' not in restart
    assert 'npm run build' not in restart
    assert 'init-db.sh' not in restart
    assert 'nginx -t' in lifecycle
    assert 'nginx -s reload' in restart
    assert 'BACKEND_PORT=' in env_template
    assert 'PUBLIC_API_BASE=' in env_template
    assert 'VITE_LEGACY_SSO_LOGIN_BASE_URL=' in env_template
    assert 'VITE_LEGACY_SSO_APP_NAME=' in env_template
    assert 'HERMES_RUNTIME_STORAGE_SOURCE=' in env_template


def test_public_prod_lifecycle_discards_stale_pid_records_without_signalling() -> None:
    command = f"""
set -euo pipefail
ROOT_DIR=$(mktemp -d)
trap 'rm -rf "$ROOT_DIR"' EXIT
mkdir -p "$ROOT_DIR/.runtime/public-dev" "$ROOT_DIR/.runtime/public-prod"
printf '%s' 99999999 > "$ROOT_DIR/.runtime/public-dev/backend.pid"
printf '%s' invalid > "$ROOT_DIR/.runtime/public-prod/backend.pid"
BACKEND_PORT=8011
source {ROOT / 'scripts' / 'lib' / 'public-prod-lifecycle.sh'}
public_prod_remove_stale_dev_backend_pid
public_prod_stop_backend
test ! -e "$ROOT_DIR/.runtime/public-dev/backend.pid"
test ! -e "$ROOT_DIR/.runtime/public-prod/backend.pid"
"""

    subprocess.run(["bash", "-c", command], check=True, capture_output=True, text=True)


def test_knative_long_run_config_uses_current_runtime_timeout_setting() -> None:
    script = (ROOT / "scripts" / "configure-knative-long-runs.sh").read_text()

    assert "HERMES_RUNTIME_LONG_REQUEST_TIMEOUT_SECONDS" in script
    assert "HERMES_DASHBOARD_STREAM_TIMEOUT_SECONDS" not in script
    assert "kubernetes.podspec-securitycontext" in script
    assert "kubernetes.podspec-persistent-volume-claim" in script
    assert "kubernetes.podspec-persistent-volume-write" in script
    assert "kubernetes.podspec-volumes-emptydir" in script


def test_public_dev_loads_backend_env_only_after_permission_gate() -> None:
    lifecycle = (ROOT / "scripts" / "lib" / "public-dev-lifecycle.sh").read_text()

    assert "private-env-file.sh" in lifecycle
    check = 'hermes_require_private_env_file "$env_file"'
    assert check in lifecycle
    assert lifecycle.index(check) < lifecycle.index('source "$env_file"')


def test_start_rejects_invalid_init_db_before_lifecycle_actions() -> None:
    result = subprocess.run(
        ["bash", "scripts/start-public-dev.sh"],
        cwd=ROOT,
        env={
            **os.environ,
            "INIT_DB": "yes",
            "PUBLIC_HOST": "127.0.0.1",
        },
        capture_output=True,
        text=True,
        check=False,
    )

    assert result.returncode == 2
    assert "INIT_DB must be 0 or 1" in result.stderr
    assert "Checking Hermes Cloud gate configuration" not in result.stdout
