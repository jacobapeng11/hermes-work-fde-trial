from pathlib import Path


ROOT = Path(__file__).resolve().parents[3]


def test_admin_password_smoke_never_prints_credential_material() -> None:
    script = (ROOT / "scripts" / "smoke-admin-password-auth.sh").read_text()

    assert "HERMES_ADMIN_AUTH_MODE=password" in script
    assert "/api/v1/auth/admin/login" in script
    assert 'echo "$login_json"' not in script
    assert 'echo "$access_token"' not in script
    assert "ADMIN_INITIAL_PASSWORD is required" in script
