from __future__ import annotations

import asyncio
import json
from pathlib import Path
import time

import pytest
from fastapi.testclient import TestClient

from app.api.v1.admin.audit_logs import get_admin_audit_log_service
from app.api.v1.admin.deps import get_admin_principal
from app.api.v1.admin.knowledge_governance import _provider
from app.main import app
from app.services.admin_auth import AdminPrincipal
from app.services.knowledge_governance import (
    GovernanceSnapshotResult,
    KnowledgeGovernanceError,
    KnowledgeGovernanceProvider,
    governance_asset,
    governance_impact,
    governance_summary,
    list_governance_assets,
)


def snapshot_payload() -> dict:
    node = {
        "id": "model:project",
        "type": "semantic_model",
        "name": "项目",
        "properties": {"table": "crm_project_info_t"},
        "sources": ["semantic_models/project.mdl.yml"],
    }
    contract = {
        "id": "metric_contract:PROJECT_DEFAULT_AMOUNT_FLOOR",
        "type": "metric_contract",
        "name": "PROJECT_DEFAULT_AMOUNT_FLOOR",
        "properties": {"severity": "warning"},
        "sources": ["contracts/metric_contracts.yml"],
    }
    edge = {
        "id": "edge:1",
        "type": "CONSTRAINED_BY",
        "from": "model:project",
        "to": "metric_contract:PROJECT_DEFAULT_AMOUNT_FLOOR",
        "properties": {},
        "sources": ["contracts/metric_contracts.yml"],
    }
    metadata = {
        "schema_version": "1.0",
        "generated_at": "2026-07-30T00:00:00+00:00",
        "source_hash": "a" * 64,
        "read_only": True,
    }
    summary = {
        "node_count": 2,
        "edge_count": 1,
        "model_count": 1,
        "field_count": 0,
        "department_count": 0,
        "permission_count": 0,
        "relationship_count": 0,
        "metric_contract_count": 1,
        "node_types": {"metric_contract": 1, "semantic_model": 1},
        "edge_types": {"CONSTRAINED_BY": 1},
    }
    dependency = {
        "assets": {
            "model:project": {
                "name": "项目",
                "type": "semantic_model",
                "sources": ["semantic_models/project.mdl.yml"],
                "upstream": [],
                "downstream": ["metric_contract:PROJECT_DEFAULT_AMOUNT_FLOOR"],
                "downstream_two_hops": [],
            },
            "metric_contract:PROJECT_DEFAULT_AMOUNT_FLOOR": {
                "name": "PROJECT_DEFAULT_AMOUNT_FLOOR",
                "type": "metric_contract",
                "sources": ["contracts/metric_contracts.yml"],
                "upstream": ["model:project"],
                "downstream": [],
                "downstream_two_hops": [],
            },
        }
    }
    validation = {
        **metadata,
        "summary": {"errors": 0, "warnings": 0, "info": 0, "status": "passed"},
        "findings": [],
    }
    catalogue = {
        **metadata,
        "summary": summary,
        "assets": {"semantic_model": [node], "metric_contract": [contract]},
    }
    graph = {**metadata, "nodes": [node, contract], "edges": [edge]}
    dependency_index = {**metadata, **dependency}
    return {
        "schema_version": "1.0",
        "generated_at": metadata["generated_at"],
        "source_hash": metadata["source_hash"],
        "summary": summary,
        "validation": validation,
        "catalogue": catalogue,
        "graph": graph,
        "dependency_index": dependency_index,
        "metric_contracts": {
            "schema_version": 1,
            "summary": {
                "total": 1,
                "errors": 0,
                "warnings": 1,
                "covered_models": 1,
                "total_models": 1,
            },
            "rules": [
                {
                    "id": "PROJECT_DEFAULT_AMOUNT_FLOOR",
                    "severity": "warning",
                    "section": "advisory_rules",
                    "source": "semantic_models/project.mdl.yml",
                    "related_sources": [],
                    "applies_to_tables": ["crm_project_info_t"],
                    "messages": ["项目统计需要默认金额门槛。"],
                }
            ],
            "reviewed_exemptions": [],
        },
    }


def write_compiler(root: Path, payload: dict) -> None:
    script = (
        "import json, os, pathlib, sys\n"
        "assert sys.argv[1:] == ['snapshot', '--stdout']\n"
        "assert pathlib.Path.cwd() == pathlib.Path(__file__).resolve().parent\n"
        "assert 'SHOULD_NOT_LEAK' not in os.environ\n"
        f"print(json.dumps({payload!r}, ensure_ascii=False))\n"
    )
    (root / "ontology_compiler.py").write_text(script, encoding="utf-8")


async def test_provider_compiles_once_then_hits_cache(tmp_path: Path, monkeypatch) -> None:
    write_compiler(tmp_path, snapshot_payload())
    (tmp_path / "PRIMER.md").write_text("primer", encoding="utf-8")
    monkeypatch.setenv("SHOULD_NOT_LEAK", "secret")
    provider = KnowledgeGovernanceProvider(tmp_path)

    first = await provider.get_snapshot()
    second = await provider.get_snapshot()

    assert first.cache["state"] == "refreshed"
    assert second.cache["state"] == "hit"
    assert first.snapshot["source_hash"] == "a" * 64
    assert governance_summary(second)["validation_summary"]["status"] == "passed"


async def test_concurrent_force_refreshes_are_single_flight(tmp_path: Path) -> None:
    (tmp_path / "ontology_compiler.py").write_text("# trusted compiler\n", encoding="utf-8")

    class _CountingProvider(KnowledgeGovernanceProvider):
        def __init__(self, source_root: Path) -> None:
            super().__init__(source_root)
            self.compile_count = 0

        async def _compile_snapshot(self) -> dict:
            self.compile_count += 1
            await asyncio.sleep(0.05)
            return snapshot_payload()

    provider = _CountingProvider(tmp_path)
    first, second = await asyncio.gather(
        provider.get_snapshot(force=True),
        provider.get_snapshot(force=True),
    )

    assert provider.compile_count == 1
    assert first.snapshot is second.snapshot
    assert {first.cache["state"], second.cache["state"]} == {"refreshed", "hit"}


async def test_failed_refresh_keeps_last_successful_snapshot(tmp_path: Path) -> None:
    write_compiler(tmp_path, snapshot_payload())
    provider = KnowledgeGovernanceProvider(tmp_path)
    first = await provider.get_snapshot()
    (tmp_path / "ontology_compiler.py").write_text(
        "raise SystemExit(2)\n",
        encoding="utf-8",
    )

    stale = await provider.get_snapshot(force=True)
    repeated = await provider.get_snapshot()

    assert stale.snapshot is first.snapshot
    assert stale.cache["state"] == "stale"
    assert stale.latest_attempt["status"] == "failed"
    assert repeated.cache["state"] == "stale"


async def test_provider_rejects_unconfigured_source() -> None:
    provider = KnowledgeGovernanceProvider(None)

    with pytest.raises(KnowledgeGovernanceError) as raised:
        await provider.get_snapshot()

    assert raised.value.code == "knowledge_governance_source_not_configured"


async def test_provider_rejects_forbidden_snapshot_keys(tmp_path: Path) -> None:
    payload = snapshot_payload()
    payload["graph"]["nodes"][0]["properties"]["user_field"] = "sales_user_code"
    write_compiler(tmp_path, payload)
    provider = KnowledgeGovernanceProvider(tmp_path)

    with pytest.raises(KnowledgeGovernanceError) as raised:
        await provider.get_snapshot()

    assert raised.value.code == "knowledge_governance_unavailable"


async def test_provider_timeout_terminates_the_compiler_without_hanging(
    tmp_path: Path,
) -> None:
    (tmp_path / "ontology_compiler.py").write_text(
        "import time\ntime.sleep(30)\n",
        encoding="utf-8",
    )
    provider = KnowledgeGovernanceProvider(tmp_path, timeout_seconds=0.05)
    started = time.monotonic()

    with pytest.raises(KnowledgeGovernanceError) as raised:
        await provider.get_snapshot()

    assert raised.value.code == "knowledge_governance_unavailable"
    assert time.monotonic() - started < 2


async def test_provider_rejects_oversized_stdout_without_hanging(
    tmp_path: Path,
) -> None:
    (tmp_path / "ontology_compiler.py").write_text(
        "import sys\nsys.stdout.write('x' * 100_000)\n",
        encoding="utf-8",
    )
    provider = KnowledgeGovernanceProvider(
        tmp_path,
        timeout_seconds=2,
        max_snapshot_bytes=1024,
    )
    started = time.monotonic()

    with pytest.raises(KnowledgeGovernanceError) as raised:
        await provider.get_snapshot()

    assert raised.value.code == "knowledge_governance_unavailable"
    assert time.monotonic() - started < 2


async def test_asset_catalogue_detail_and_impact_are_bounded(tmp_path: Path) -> None:
    write_compiler(tmp_path, snapshot_payload())
    provider = KnowledgeGovernanceProvider(tmp_path)
    result = await provider.get_snapshot()

    assets = list_governance_assets(
        result,
        query="项目",
        asset_type="semantic_model",
        page=1,
        page_size=20,
    )
    detail = governance_asset(result, "model:project")
    impact = governance_impact(result, "model:project")

    assert assets["total"] == 1
    assert detail is not None
    assert detail["relationships"][0]["type"] == "CONSTRAINED_BY"
    assert impact is not None
    assert impact["affected"][0]["type"] == "metric_contract"


def test_admin_knowledge_governance_routes_are_registered() -> None:
    paths = {route.path for route in app.routes}
    assert "/api/v1/admin/knowledge-governance/summary" in paths
    assert "/api/v1/admin/knowledge-governance/validation" in paths
    assert "/api/v1/admin/knowledge-governance/assets" in paths
    assert "/api/v1/admin/knowledge-governance/assets/{asset_id:path}" in paths
    assert "/api/v1/admin/knowledge-governance/impact/{asset_id:path}" in paths
    assert "/api/v1/admin/knowledge-governance/metric-contracts" in paths
    assert "/api/v1/admin/knowledge-governance/refresh" in paths


def test_admin_knowledge_governance_endpoints_return_bounded_views_and_audit_refresh() -> None:
    class _Provider:
        def __init__(self) -> None:
            self.force_calls: list[bool] = []

        async def get_snapshot(self, *, force: bool = False) -> GovernanceSnapshotResult:
            self.force_calls.append(force)
            return GovernanceSnapshotResult(
                snapshot=snapshot_payload(),
                cache={
                    "state": "refreshed" if force else "hit",
                    "source_fingerprint": "fingerprint",
                    "last_success_at": "2026-07-30T00:00:00Z",
                    "last_attempt_at": "2026-07-30T00:00:00Z",
                },
                latest_attempt={
                    "status": "succeeded",
                    "attempted_at": "2026-07-30T00:00:00Z",
                    "source_hash": "a" * 64,
                },
            )

    class _Audit:
        def __init__(self) -> None:
            self.records: list[dict] = []

        async def record(self, **kwargs) -> None:
            self.records.append(kwargs)

    provider = _Provider()
    audit = _Audit()
    app.dependency_overrides[get_admin_principal] = lambda: AdminPrincipal(
        org_id=1,
        user_id=7,
        roles=("admin",),
    )
    app.dependency_overrides[_provider] = lambda: provider
    app.dependency_overrides[get_admin_audit_log_service] = lambda: audit
    try:
        client = TestClient(app)
        summary = client.get("/api/v1/admin/knowledge-governance/summary")
        validation = client.get("/api/v1/admin/knowledge-governance/validation")
        assets = client.get(
            "/api/v1/admin/knowledge-governance/assets",
            params={"q": "项目", "asset_type": "semantic_model", "page_size": 1},
        )
        detail = client.get(
            "/api/v1/admin/knowledge-governance/assets/model%3Aproject"
        )
        impact = client.get(
            "/api/v1/admin/knowledge-governance/impact/model%3Aproject"
        )
        contracts = client.get(
            "/api/v1/admin/knowledge-governance/metric-contracts"
        )
        refreshed = client.post("/api/v1/admin/knowledge-governance/refresh")
    finally:
        app.dependency_overrides.pop(get_admin_principal, None)
        app.dependency_overrides.pop(_provider, None)
        app.dependency_overrides.pop(get_admin_audit_log_service, None)

    assert summary.status_code == validation.status_code == 200
    assert summary.json()["summary"]["node_count"] == 2
    assert assets.json()["total"] == 1
    assert assets.json()["items"][0]["id"] == "model:project"
    assert detail.json()["asset"]["id"] == "model:project"
    assert impact.json()["affected"][0]["type"] == "metric_contract"
    assert contracts.json()["metric_contracts"]["summary"]["total"] == 1
    assert refreshed.json()["cache"]["state"] == "refreshed"
    assert provider.force_calls[-1] is True
    assert audit.records[0]["action"] == "knowledge_governance.refresh"
    assert audit.records[0]["detail"]["status"] == "succeeded"
