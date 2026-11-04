import os
import subprocess
from pathlib import Path


ROOT = Path(__file__).resolve().parents[3]


def _run_legacy_sso_config(
    *,
    login_base_url: str | None = None,
    app_name: str | None = None,
) -> subprocess.CompletedProcess[str]:
    command = (
        f"source {ROOT / 'scripts/lib/public-frontend-auth-config.sh'}; "
        "public_frontend_require_legacy_sso_config"
    )
    env = {
        key: value
        for key, value in os.environ.items()
        if not key.startswith("VITE_LEGACY_SSO_")
    }
    if login_base_url is not None:
        env["VITE_LEGACY_SSO_LOGIN_BASE_URL"] = login_base_url
    if app_name is not None:
        env["VITE_LEGACY_SSO_APP_NAME"] = app_name
    return subprocess.run(
        ["bash", "-c", command],
        cwd=ROOT,
        env=env,
        capture_output=True,
        text=True,
        check=False,
    )


def test_legacy_sso_frontend_config_requires_explicit_valid_environment_values() -> None:
    for app_name in ("agent-uat", "agent-uat-v2"):
        result = _run_legacy_sso_config(
            login_base_url="https://login.example.com/sd/index.php/api/clue/login_mobile",
            app_name=app_name,
        )
        assert result.returncode == 0, result.stderr

    missing = _run_legacy_sso_config(
        login_base_url="https://login.example.com/sd/index.php/api/clue/login_mobile",
    )
    assert missing.returncode != 0
    assert "VITE_LEGACY_SSO_APP_NAME" in missing.stderr

    invalid = _run_legacy_sso_config(
        login_base_url="http://login.example.com/login",
        app_name="agent uat",
    )
    assert invalid.returncode != 0
    assert "must be an absolute HTTPS URL" in invalid.stderr


def test_frontend_overlay_owns_only_vite_and_reuses_formal_backend() -> None:
    start = (ROOT / "scripts" / "start-public-frontends-dev.sh").read_text()
    stop = (ROOT / "scripts" / "stop-public-frontends-dev.sh").read_text()
    lifecycle = (ROOT / "scripts" / "lib" / "public-frontend-dev-lifecycle.sh").read_text()

    assert "public_prod_load_backend_env" in lifecycle
    assert "VITE_BACKEND_PROXY_TARGET=\"http://${BACKEND_LOCAL_HOST}:${BACKEND_PORT}\"" in lifecycle
    assert "public_frontend_require_legacy_sso_config" in lifecycle
    assert "VITE_LEGACY_SSO_LOGIN_BASE_URL=\"$VITE_LEGACY_SSO_LOGIN_BASE_URL\"" in lifecycle
    assert "VITE_LEGACY_SSO_APP_NAME=\"$VITE_LEGACY_SSO_APP_NAME\"" in lifecycle
    assert 'hmr_protocol="${VITE_HMR_PROTOCOL:-wss}"' in lifecycle
    assert "public_frontend_dev_listener_pids" in lifecycle
    assert "stopped orphan listener" in lifecycle
    assert "uvicorn" not in start
    assert "stop-public-dev.sh" not in start
    assert "status-public-prod.sh" in start
    assert start.index("status-public-prod.sh") < start.index("/api/v1/readyz")
    assert "switch-public-nginx-mode.sh dev" in start
    assert "switch-public-nginx-mode.sh prod" in start
    assert "Nginx 仍在 dev 模式" in stop


def test_nginx_mode_scripts_keep_dev_and_prod_configs_mutually_exclusive() -> None:
    install = (ROOT / "scripts" / "install-public-nginx-dev-config.sh").read_text()
    switch = (ROOT / "scripts" / "switch-public-nginx-mode.sh").read_text()
    query = (ROOT / "scripts" / "public-nginx-mode.sh").read_text()

    assert 'PUBLIC_NGINX_DEV_CONFIG' in install
    assert 'PUBLIC_NGINX_DEV_TEMPLATE' in install
    assert "dev|prod" in switch
    assert "nginx -t" in switch
    assert "nginx -s reload" in switch
    assert "restore_previous_mode" in switch
    assert "PUBLIC_NGINX_PROD_TEMPLATE" in switch
    assert "PUBLIC_NGINX_DEV_TEMPLATE" in switch
    assert "PUBLIC_NGINX_DEV_TEMPLATE" in install
    assert 'echo prod' in query
    assert 'echo dev' in query
