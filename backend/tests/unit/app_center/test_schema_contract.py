from pathlib import Path


ROOT = Path(__file__).resolve().parents[4]


def test_current_schema_creates_app_center_table() -> None:
    content = (
        ROOT / "backend" / "scripts" / "current_schema.sql"
    ).read_text(encoding="utf-8")

    assert "CREATE TABLE IF NOT EXISTS app_center_app" in content
    assert "description VARCHAR(64) NOT NULL DEFAULT 'HTML 应用'" in content
    assert "package_blob LONGBLOB NOT NULL" in content
    assert "html_content LONGTEXT NULL" in content
    assert "uq_app_center_idempotency" in content
    assert "fk_app_center_org" in content
    assert "fk_app_center_creator" in content
    assert "INDEX ix_app_center_org_updated (org_id, updated_at)" in content


def test_model_and_schema_share_app_center_columns() -> None:
    from app.models import Base

    columns = set(Base.metadata.tables["app_center_app"].columns.keys())
    expected = {
        "id",
        "org_id",
        "name",
        "description",
        "source_type",
        "share_mode",
        "html_content",
        "package_blob",
        "access_url",
        "demo_id",
        "demo_code",
        "publish_version_id",
        "business_domain",
        "creator_user_id",
        "copied_from_app_id",
        "copied_from_creator_name",
        "source_session_id",
        "source_run_id",
        "idempotency_key",
        "idempotency_fingerprint",
        "created_at",
        "updated_at",
    }

    assert columns == expected
