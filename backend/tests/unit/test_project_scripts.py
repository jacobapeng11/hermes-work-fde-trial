from pathlib import Path
import subprocess

import yaml


ROOT = Path(__file__).resolve().parents[3]


def test_supervisor_dependencies_do_not_downgrade_newer_hermes_images() -> None:
    pyproject = (ROOT / "runtime" / "supervisor" / "pyproject.toml").read_text()

    for dependency in (
        "fastapi",
        "httpx",
        "pydantic",
        "pydantic-settings",
        "PyYAML",
        "uvicorn[standard]",
        "websockets",
    ):
        assert f'"{dependency}==' not in pyproject
        assert f'"{dependency}>=' in pyproject
    assert '"fastapi>=0.115.6,<1"' in pyproject
    assert '"websockets>=15.0.1,<17"' in pyproject


def test_runtime_plugin_layer_installs_expert_roster_into_hermes_environment() -> None:
    dockerfile = (ROOT / "Dockerfile.hermes-enterprise-knowledge").read_text()
    verifier = (
        ROOT / "runtime" / "hermes-runtime-overlay" / "verify_runtime_overlay.py"
    ).read_text()

    assert "runtime/expert-roster-plugin" in dockerfile
    assert "/opt/hermes/.venv/bin/python" in dockerfile
    assert '"hermes-hermes-expert-roster": "0.1.0"' in verifier
    assert '"expert-roster": "hermes_expert_roster.plugin"' in verifier


def test_runtime_image_supervises_native_kanban_dispatcher() -> None:
    dockerfile = (ROOT / "Dockerfile.hermes-enterprise-knowledge").read_text()
    run_script = (
        ROOT
        / "runtime"
        / "kanban-dispatcher"
        / "s6-rc.d"
        / "kanban-dispatcher"
        / "run"
    )
    service_link = (
        ROOT
        / "runtime"
        / "kanban-dispatcher"
        / "s6-rc.d"
        / "user"
        / "contents.d"
        / "kanban-dispatcher"
    )

    assert run_script.exists()
    subprocess.run(
        ["sh", "-n", str(run_script)],
        check=True,
        capture_output=True,
        text=True,
    )
    content = run_script.read_text()
    assert "/opt/hermes/.venv/bin/hermes kanban daemon" in content
    assert "--force" in content
    assert 'HERMES_HOME="${HERMES_HOME:-/opt/data}"' in content
    assert service_link.exists()
    assert "runtime/kanban-dispatcher/s6-rc.d/kanban-dispatcher" in dockerfile
    assert "/etc/s6-overlay/s6-rc.d/user/contents.d/kanban-dispatcher" in dockerfile
    assert "chmod 0755 /etc/s6-overlay/s6-rc.d/kanban-dispatcher/run" in dockerfile


def test_project_scripts_exist_and_are_hermes_first() -> None:
    scripts = [
        ROOT / "scripts" / "check-env.sh",
        ROOT / "scripts" / "init-db.sh",
        ROOT / "scripts" / "smoke-backend.sh",
        ROOT / "scripts" / "smoke-runtime-network.sh",
        ROOT / "scripts" / "smoke-runtime.sh",
        ROOT / "scripts" / "smoke-runtime-supervisor.sh",
        ROOT / "scripts" / "smoke-provider.sh",
        ROOT / "scripts" / "smoke-user-web.sh",
        ROOT / "scripts" / "smoke-admin-web.sh",
        ROOT / "scripts" / "smoke-admin-usage.sh",
        ROOT / "scripts" / "smoke-auth.sh",
        ROOT / "scripts" / "status.sh",
        ROOT / "scripts" / "check-gates.sh",
    ]

    for script in scripts:
        assert script.exists(), script
        content = script.read_text()
        assert "COPAW" not in content
        assert "CoPaw" not in content
        assert "BusinessWorkspace" not in content


def test_check_env_audits_registry_before_legacy_alignment() -> None:
    content = (ROOT / "scripts" / "check-env.sh").read_text()

    registry_check = (
        'python3 "$ROOT_DIR/scripts/check-env-registry.py" '
        '--env-file "$ENV_FILE" --root "$ROOT_DIR"'
    )
    alignment_check = (
        'exec python3 "$ROOT_DIR/scripts/check-env-alignment.py" '
        '--env-file "$ENV_FILE" --mode "$mode" --root "$ROOT_DIR"'
    )

    assert registry_check in content
    assert alignment_check in content
    assert content.index(registry_check) < content.index(alignment_check)


def test_restart_public_dev_uses_complete_stop_and_leaves_runtimes_cold() -> None:
    script = ROOT / "scripts" / "restart-public-dev.sh"
    assert script.exists()

    content = script.read_text()
    assert "scripts/stop-public-dev.sh" in content
    assert "scripts/start-public-dev.sh" in content
    assert content.index("scripts/stop-public-dev.sh") < content.index("scripts/start-public-dev.sh")
    assert "WARMUP_RUNTIME=0" in content
    assert "delete pod" not in content
    assert "--force" not in content
    assert "delete kservice" not in content
    assert "delete pvc" not in content
    assert "knative-serving" not in content
    assert "kourier-system" not in content


def test_image_restart_paths_always_use_kservice_reset_and_frontend_only_paths_do_not() -> None:
    project_restart = (ROOT / "scripts" / "restart-project-from-env.sh").read_text()
    rebuild_code = (
        ROOT / "scripts" / "rebuild-runtime-image-and-restart.sh"
    ).read_text()
    rebuild_v3 = (
        ROOT / "scripts" / "rebuild-runtime-v3-image-and-restart.sh"
    ).read_text()
    rebuild_test = (ROOT / "scripts" / "rebuild-runtime-test.sh").read_text()
    canary = (ROOT / "scripts" / "reload-one-runtime-from-env-dev.py").read_text()

    assert "reload-all-runtimes-from-env-dev.sh" in project_restart
    assert "--reexecute-completed" in project_restart
    assert "restart-project-from-env.sh" in rebuild_code
    assert "restart-project-from-env.sh" in rebuild_v3
    assert "reload-all-runtimes-from-env-dev.sh" in rebuild_test
    assert 'stop_command = ["stop", *selector]' in canary
    assert 'reload_command = ["reload", *selector]' in canary
    assert 'checkpoint.advance(stage="pods_zero"' in canary

    frontend_only_paths = (
        "restart-public.sh",
        "restart-public-dev.sh",
        "restart-public-prod.sh",
        "restart-public-backend-dev.sh",
        "start-public-frontends-dev.sh",
        "stop-public-frontends-dev.sh",
    )
    runtime_reset_markers = (
        "reload-all-runtimes-from-env-dev.sh",
        "restart-project-from-env.sh",
        "reload-one-runtime-from-env-dev.sh",
    )
    for relative in frontend_only_paths:
        content = (ROOT / "scripts" / relative).read_text()
        assert not any(marker in content for marker in runtime_reset_markers), relative


def test_public_dev_lifecycle_has_no_upgrade_worker() -> None:
    lifecycle = (
        ROOT / "scripts" / "lib" / "public-dev-lifecycle.sh"
    ).read_text()
    stop = (ROOT / "scripts" / "stop-public-dev.sh").read_text()

    assert "Background workers have no TCP listener" in lifecycle
    assert "ownership boundary" in lifecycle
    assert "stopped orphan process" not in lifecycle
    start = (ROOT / "scripts" / "start-public-dev.sh").read_text()
    status = (ROOT / "scripts" / "status.sh").read_text()
    assert "upgrade-worker" not in stop
    assert "upgrade-worker" not in start
    assert "upgrade-worker" not in status


def test_nginx_runtime_auth_uses_fixed_subrequest_uris() -> None:
    for relative in (
        "deploy/nginx/hermes-enterprise_idp.conf.example",
        "deploy/nginx/hermes-enterprise_idp-uat-origin.conf",
    ):
        content = (ROOT / relative).read_text()

        assert "set $runtime_edge_auth_handle $runtime_handle;" in content
        assert "auth_request /_runtime-edge-auth;" in content
        assert "auth_request /_runtime-edge-auth/$runtime_handle;" not in content
        assert "location = /_runtime-edge-auth" in content
        assert (
            "proxy_pass http://127.0.0.1:8011/internal/v1/"
            "runtime-edge-auth/$runtime_edge_auth_handle;"
        ) in content

        assert (
            "set $runtime_file_auth_handle_saved $runtime_file_handle;"
            in content
        )
        assert (
            "set $runtime_file_auth_operation_saved $runtime_file_operation;"
            in content
        )
        assert "auth_request /_runtime-file-auth;" in content
        assert "auth_request /_runtime-file-auth/$runtime_file_handle" not in content
        assert "location = /_runtime-file-auth" in content
        assert (
            "proxy_pass http://127.0.0.1:8011/internal/v1/"
            "runtime-file-auth/$runtime_file_auth_handle_saved;"
        ) in content
        assert (
            "proxy_set_header X-Hermes-File-Operation "
            "$runtime_file_auth_operation_saved;"
        ) in content


def test_init_db_uses_safe_schema_bootstrap_and_seed() -> None:
    content = (ROOT / "scripts" / "init-db.sh").read_text()

    assert "CREATE DATABASE IF NOT EXISTS" in content
    assert "scripts.schema_bootstrap create" in content
    assert "scripts.schema_bootstrap verify" in content
    assert "current_schema.sql" in content
    assert "alembic" not in content.lower()
    assert "await conn.run_sync(Base.metadata.create_all)" not in content
    assert "scripts.seed" in content


def test_smoke_backend_runs_tests_and_health_checks() -> None:
    content = (ROOT / "scripts" / "smoke-backend.sh").read_text()

    assert "pytest backend/tests" in content
    assert '"/health"' in content
    assert '"/api/v1/health"' in content


def test_status_checks_mysql_authentication_not_only_ping() -> None:
    content = (ROOT / "scripts" / "status.sh").read_text()

    assert "SELECT 1" in content
    assert "mysqladmin ping" not in content
    assert ".runtime/public-frontends-dev" in content
    assert "/admin/" in content
    assert "public_dev_process_belongs" in content
    assert "check-enterprise-knowledge-data-service.sh" in content
    assert "public_dev_process_belongs" in content
    assert "enterprise knowledge bundle" in content
    assert "runtime_infra_probe.py" in content
    assert "overall: ready" in content
    assert "overall: not_ready" in content


def test_public_dev_start_is_strictly_real_and_waits_for_readiness() -> None:
    start = (ROOT / "scripts" / "start-public-dev.sh").read_text()
    readme = (ROOT / "README.md").read_text()

    assert "public_dev_require_real_backend_modes" in start
    assert "runtime_infra_probe.py" in start
    assert "/api/v1/readyz" in start
    assert "REAL_BACKEND_MODE" not in start
    assert "REAL_BACKEND_MODE" not in readme


def test_enterprise_knowledge_uses_monorepo_source_and_public_dev_lifecycle() -> None:
    build = (ROOT / "scripts" / "build-enterprise-knowledge-bundle.sh").read_text()
    publish = (
        ROOT / "scripts" / "publish-enterprise-knowledge-bundle.sh"
    ).read_text()
    start = (ROOT / "scripts" / "start-public-dev.sh").read_text()
    stop = (ROOT / "scripts" / "stop-public-dev.sh").read_text()
    data_requirements = (
        ROOT / "enterprise-knowledge" / "data_service" / "requirements.txt"
    ).read_text()

    assert '"$ROOT_DIR/enterprise-knowledge"' in build
    assert '"$ROOT_DIR/enterprise-knowledge"' in publish
    assert "/root/enterprise-knowledge" not in build
    assert "/root/enterprise-knowledge" not in publish
    assert "ENTERPRISE_KNOWLEDGE_HOST_PATH" in start
    assert "check-enterprise-knowledge-data-service.sh" in start
    assert "enterprise-knowledge-data" not in stop
    assert "KB-NEW" not in start
    assert "fastmcp" not in start.lower()
    assert "sentence-transformers" not in data_requirements


def test_runtime_smoke_scripts_are_gate_aware() -> None:
    network = (ROOT / "scripts" / "smoke-runtime-network.sh").read_text()
    runtime = (ROOT / "scripts" / "smoke-runtime.sh").read_text()

    assert "Gate 类型: k8s-knative" in network
    assert "HERMES_RUNTIME_TEST_ROUTE_HOST" in network
    assert "HERMES_RUNTIME_TEST_ROUTE_HOSTS" in network
    assert "runtime network smoke: ok host=" in network
    assert "svc.cluster.local" in network
    assert "不能作为集群外 NodePort + Host Header 验收" in network
    assert "API_SERVER_KEY" in network
    assert '"/api/v1/user/runtime-workspace"' in runtime
    assert "HERMES_RUNTIME_IMAGE" in runtime
    assert "docker image inspect" in runtime
    assert "projection_worker_job" not in runtime
    assert "hermes_cli.web_server" in runtime
    assert "gateway.run" in runtime
    assert "platform_supervisor.main" in runtime
    assert 'metadata.version("hermes-platform-supervisor")' in runtime
    assert "/opt/hermes/.venv/bin/platform-supervisor" in runtime
    assert "HERMES_PROJECTION_WORKER_IMAGE" not in runtime


def test_runtime_supervisor_smoke_checks_the_new_execution_plane() -> None:
    script = ROOT / "scripts" / "smoke-runtime-supervisor.sh"
    subprocess.run(
        ["bash", "-n", str(script)],
        check=True,
        capture_output=True,
        text=True,
    )
    content = script.read_text()
    assert 'container.get("command") != ["/opt/hermes/.venv/bin/platform-supervisor"]' in content
    assert 'ports != {9120}' in content
    assert 'readiness.get("path") != "/readyz"' in content
    assert 'liveness.get("path") != "/healthz"' in content
    assert '"supervisor_9120": True' in content
    assert '"dashboard_9119": True' in content
    assert '"model_relay_9121": True' in content
    assert '"legacy_gateway_8642": False' in content
    assert '"/data/workspace"' in content
    assert '"/workspace"' not in content
    assert "HERMES_APPLICATION_AGENT_PVC_ACCESS_MODE" in content
    assert 'kube_json("pvc", manifest["runtime_root_pvc"])' in content
    assert '"journal_mode": "wal"' in content
    assert '"synchronous": 2' in content
    assert "MYSQL_" in content
    assert "REDIS_" in content
    assert "WECOM_" in content


def test_provider_smoke_uses_openai_compatible_gate_without_printing_key() -> None:
    content = (ROOT / "scripts" / "smoke-provider.sh").read_text()

    assert "OPENAI_COMPATIBLE_BASE_URL" in content
    assert "OPENAI_COMPATIBLE_API_KEY" in content
    assert "/models" in content
    assert "--noproxy" in content
    assert "provider models smoke: ok" in content


def test_admin_web_smoke_uses_frontend_proxy_and_admin_audit_chain() -> None:
    content = (ROOT / "scripts" / "smoke-admin-web.sh").read_text()

    assert "VITE_BACKEND_PROXY_TARGET" in content
    assert "/api/v1/admin/me" in content
    assert "/api/v1/admin/models" in content
    assert "/api/v1/admin/resources/runtime-workspaces" in content
    assert "/api/v1/admin/usage/summary" in content
    assert "/api/v1/admin/experts" in content
    assert "/api/v1/admin/runtime-workspaces/" not in content
    assert "admin.runtime.reproject" not in content
    assert "provider_key" not in content
    assert "api_key" not in content
    assert "admin web smoke: ok" in content


def test_admin_usage_smoke_verifies_aggregation_and_sensitive_field_boundary() -> None:
    content = (ROOT / "scripts" / "smoke-admin-usage.sh").read_text()

    assert "/api/v1/admin/usage/summary" in content
    assert "model_call_count" in content
    assert "total_tokens" in content
    assert '"api" + "_key"' in content
    assert '"runtime" + "_token"' in content
    assert "admin usage smoke: ok" in content


def test_auth_smoke_enforces_jwt_mode_without_secrets() -> None:
    content = (ROOT / "scripts" / "smoke-auth.sh").read_text()

    assert "HERMES_AUTH_MODE=jwt" in content
    assert "/api/v1/auth/login" in content
    assert "/api/v1/auth/me" in content
    assert "/api/v1/user/runtime-workspace" in content
    assert "/api/v1/admin/me" in content
    assert "auth smoke: ok" in content
    assert "provider_key" not in content
    assert "api_key" not in content


def test_backend_env_is_the_single_runtime_configuration_source() -> None:
    start = (ROOT / "scripts" / "start-public-dev.sh").read_text()
    lifecycle = (ROOT / "scripts" / "lib" / "public-dev-lifecycle.sh").read_text()
    init = (ROOT / "scripts" / "check-env.sh").read_text()
    gitignore = (ROOT / ".gitignore").read_text()

    assert "public_dev_load_backend_env" in start
    assert 'source "$env_file"' in lifecycle
    assert "sync-local-env" not in start
    assert "hermes-local.env" not in start
    assert "check-env-alignment.py" in init
    assert not (ROOT / "scripts" / "sync-local-env.sh").exists()
    assert (ROOT / "backend" / "env.sample").exists()
    tracked_env = subprocess.run(
        ["git", "ls-files"], cwd=ROOT, capture_output=True, text=True, check=True
    ).stdout.splitlines()
    assert not any(
        path.rsplit("/", 1)[-1] == ".env"
        or path.rsplit("/", 1)[-1].startswith(".env.")
        for path in tracked_env
    )
    assert "**/.env" in gitignore


def test_dev_env_alignment_requires_both_managed_nginx_config_paths(tmp_path: Path) -> None:
    env_file = tmp_path / "env"
    checker = ROOT / "scripts" / "check-env-alignment.py"
    required = {
        "APP_ENV",
        "RUNTIME_CHAT_MODE",
        "HERMES_AUTH_MODE",
        "HERMES_ADMIN_AUTH_MODE",
        "BACKEND_HOST",
        "BACKEND_LOCAL_HOST",
        "BACKEND_PORT",
        "USER_WEB_HOST",
        "USER_WEB_PORT",
        "ADMIN_WEB_HOST",
        "ADMIN_WEB_PORT",
        "PUBLIC_WEB_BASE",
        "PUBLIC_ADMIN_BASE",
        "PUBLIC_API_BASE",
        "ENTERPRISE_KNOWLEDGE_HOST_PATH",
    }
    env_file.write_text("\n".join(f"{key}=test" for key in sorted(required)) + "\n")
    env_file.chmod(0o600)
    result = subprocess.run(
        ["python3", str(checker), "--env-file", str(env_file), "--mode", "dev", "--root", str(ROOT)],
        cwd=ROOT,
        capture_output=True,
        text=True,
        check=False,
    )
    assert result.returncode != 0
    assert "missing: PUBLIC_NGINX_CONFIG" in result.stdout
    assert "missing: PUBLIC_NGINX_DEV_CONFIG" in result.stdout


def test_gate_checker_reports_required_gate_fields_without_secret_values() -> None:
    content = (ROOT / "scripts" / "check-gates.sh").read_text()

    assert "check-config-registry.py" not in content
    assert "registry incomplete" not in content
    assert "MYSQL_PASSWORD" in content
    assert "HERMES_RUNTIME_INGRESS_BASE_URL" in content
    assert "HERMES_RUNTIME_" + "GENERATION" not in content
    assert "PUBLIC_API_BASE" in content
    assert "OPENAI_COMPATIBLE_BASE_URL" in content
    assert "OPENAI_COMPATIBLE_API_KEY" in content
    assert "Gate configuration: incomplete" in content
    assert "HERMES_RUNTIME_CONTAINER_PORT" not in content
    assert "HERMES_RUNTIME_GATEWAY_BASE_URL" not in content
    assert "MODEL_PROXY_BASE_URL" not in content
    assert 'require_nonempty "k8s-knative" "HERMES_RUNTIME_TEST_ROUTE_HOST"' not in content
    assert "backend/.env" in content


def test_public_dev_backend_port_is_not_derived_from_public_api_base() -> None:
    lifecycle = ROOT / "scripts" / "lib" / "public-dev-lifecycle.sh"
    command = f"""
set -euo pipefail
ROOT_DIR={ROOT!s}
source {lifecycle!s}
BACKEND_HOST=127.0.0.1
BACKEND_LOCAL_HOST=127.0.0.1
USER_WEB_HOST=127.0.0.1
ADMIN_WEB_HOST=127.0.0.1
USER_WEB_PORT=15175
ADMIN_WEB_PORT=15174
unset BACKEND_PORT
PUBLIC_API_BASE=https://app.example.com
PORT_BACKEND=18001
public_dev_resolve_ports
printf '%s' "$BACKEND_PORT"
"""
    result = subprocess.run(
        ["bash", "-c", command],
        check=True,
        capture_output=True,
        text=True,
    )

    assert result.stdout == "18001"
    assert "MODEL_PROXY_BASE_URL" not in lifecycle.read_text()


def test_operational_scripts_use_unified_runtime_setting_names() -> None:
    paths = [
        ROOT / "scripts" / "check-gates.sh",
        ROOT / "scripts" / "smoke-runtime-network.sh",
        ROOT / "backend" / "app" / "services" / "runtime_infrastructure.py",
    ]
    content = "\n".join(path.read_text() for path in paths)

    for removed in (
        "HERMES_RUNTIME_GATEWAY_BASE_URL",
        "HERMES_RUNTIME_CONFIG_SOURCE",
        "HERMES_RUNTIME_CONTAINER_PORT",
        "HERMES_HOME_MOUNT_PATH",
        "HERMES_WORKSPACE_MOUNT_PATH",
        "settings.MODEL_PROXY_BASE_URL",
    ):
        assert removed not in content
    assert "HERMES_RUNTIME_INGRESS_BASE_URL" in content
    assert not (ROOT / "backend" / "scripts" / "warmup_dashboard_streams.py").exists()


def test_admin_kubernetes_rbac_gate_requires_safe_permissions_and_denies_dangerous_ones() -> None:
    content = (ROOT / "scripts" / "check-admin-kubernetes-rbac.sh").read_text()

    assert "pods --subresource=log" in content
    assert '"delete pods"' in content
    assert 'require_no "runtime Service reads"' in content
    assert 'require_no "EndpointSlice reads"' in content
    assert "patch revisions.serving.knative.dev" in content
    assert "patch podautoscalers.autoscaling.internal.knative.dev" in content
    assert '"pods/exec"' in content
    assert '"secrets"' in content
    assert '"PVC deletion"' in content
    assert '"Service deletion"' in content
    assert '"EndpointSlice mutation"' in content
    assert '"Knative control-plane patch"' in content


def test_admin_kubernetes_reader_cannot_read_legacy_gateway_routing_resources() -> None:
    documents = list(
        yaml.safe_load_all(
            (
                ROOT
                / "deploy"
                / "kubernetes"
                / "admin-resource-reader-rbac.yaml"
            ).read_text()
        )
    )
    runtime_role = next(
        document
        for document in documents
        if document.get("kind") == "Role"
        and document.get("metadata", {}).get("name") == "hermes-admin-runtime-resources"
    )

    def verbs_for(api_group: str, resource: str) -> set[str]:
        return {
            verb
            for rule in runtime_role["rules"]
            if api_group in rule.get("apiGroups", []) and resource in rule.get("resources", [])
            for verb in rule.get("verbs", [])
        }

    for api_group, resource in (("", "services"), ("discovery.k8s.io", "endpointslices")):
        verbs = verbs_for(api_group, resource)
        assert verbs == set()


def test_admin_kubernetes_smoke_is_read_only_by_default_and_requires_one_workspace() -> None:
    content = (ROOT / "scripts" / "smoke-admin-kubernetes-resources.sh").read_text()

    assert "<runtime_workspace_id>" in content
    assert "/admin/resources/cluster-summary" in content
    assert "/kubernetes/yaml-summary" in content
    assert "curl -X POST" not in content
    assert "delete pod" not in content


def test_public_dev_uses_backend_env_for_dedicated_admin_kubeconfig_without_runtime_fallback() -> None:
    start = (ROOT / "scripts" / "start-public-dev.sh").read_text()
    lifecycle = (ROOT / "scripts" / "lib" / "public-dev-lifecycle.sh").read_text()
    api = (
        ROOT
        / "backend"
        / "app"
        / "api"
        / "v1"
        / "admin"
        / "kubernetes_resources.py"
    ).read_text()

    assert "public_dev_load_backend_env" in start
    assert "ADMIN_KUBECONFIG_DEFAULT" not in start
    assert "backend/.env" in lifecycle
    assert "settings.ADMIN_KUBECONFIG" in api
    assert "settings.ADMIN_KUBECONFIG or settings.KUBECONFIG" not in api
