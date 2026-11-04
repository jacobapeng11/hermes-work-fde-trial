from pathlib import Path


ROOT = Path(__file__).resolve().parents[3]


def test_real_smoke_is_strict_through_cleanup() -> None:
    content = (ROOT / "scripts" / "smoke-personal-knowledge.sh").read_text()

    assert (
        "runtime-personal-knowledge/"
        "$PERSONAL_KNOWLEDGE_SMOKE_RUNTIME_ID/state"
    ) in content
    assert "--data '{\"enabled\":false}'" in content
    assert "expected deleted knowledge base to return 404" in content
    assert "cleanup residue: knowledge_base_id=" in content
    assert "SMOKE_COMPLETE=1" in content
    assert 'PERSONAL_KNOWLEDGE_SMOKE_PROVIDER:-filebase' in content
    assert "/wiki-search" in content
    assert "/wiki-read-page" in content
    assert "source_files" in content
    assert "tests/unit/personal_knowledge/" in content
    assert "knowledge_bases[0]" not in content
    assert "src/components/knowledge/KnowledgePage.test.tsx" in content
    assert "src/components/knowledge/PersonalKnowledgePanel.test.tsx" in content
    assert "personal_knowledge_capability_unavailable" not in content
    assert "|| true" not in content


def test_fresh_database_smoke_is_explicit_and_isolated() -> None:
    content = (ROOT / "scripts" / "smoke-fresh-db.sh").read_text()

    assert 'HERMES_FRESH_DB_SMOKE:-0' in content
    assert 'HERMES_FRESH_DB_SMOKE must be 1' in content
    assert 'TEMP_DB="${MYSQL_DB}_fresh_smoke_' in content
    assert 'if [[ "$TEMP_DB" == "$MYSQL_DB" ]]' in content
    assert "scripts.schema_bootstrap create" in content
    assert "current_schema.sql" in content
    assert "alembic" not in content.lower()
    assert "scripts.schema_bootstrap verify" in content
    assert "DROP DATABASE IF EXISTS" in content


def test_startup_has_no_personal_knowledge_release_gate() -> None:
    gate = (ROOT / "scripts" / "check-gates.sh").read_text()
    start = (ROOT / "scripts" / "start-public-dev.sh").read_text()

    assert "PERSONAL_KNOWLEDGE_RELEASE_GATE" not in gate + start
    assert "PERSONAL_KNOWLEDGE_ASYNC_INGEST_CONFIRMED" not in gate + start
    assert 'require_nonempty "database" "MYSQL_DB"' in gate
    assert (
        'require_nonempty "k8s-knative" "HERMES_RUNTIME_NAMESPACE"'
        in gate
    )
