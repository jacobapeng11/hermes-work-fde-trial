from __future__ import annotations

from pathlib import Path
import subprocess


ROOT = Path(__file__).resolve().parents[3]


def test_runtime_bundle_keeps_answering_assets_and_excludes_governance_internals(
    tmp_path: Path,
) -> None:
    source = tmp_path / "source"
    destination = tmp_path / "bundle"
    destination.mkdir()
    (source / "workflows").mkdir(parents=True)
    (source / "semantic_models").mkdir()
    (source / "departments" / "sales").mkdir(parents=True)
    (source / "wiki" / "semantic").mkdir(parents=True)
    (source / "PRIMER.md").write_text("# Enterprise knowledge\n", encoding="utf-8")
    (source / "workflows" / "query.md").write_text("# Query\n", encoding="utf-8")
    (source / "semantic_models" / "project.mdl.yml").write_text(
        "name: project\n",
        encoding="utf-8",
    )
    (source / "departments" / "sales" / "glossary.md").write_text(
        "# Sales glossary\n",
        encoding="utf-8",
    )
    (source / "wiki" / "semantic" / "sales.mdl.yml").write_text(
        "name: sales\n",
        encoding="utf-8",
    )

    forbidden_files = [
        "ontology_compiler.py",
        "serve.py",
        "generated/enterprise-graph.json",
        "ui/index.html",
        "tests/test_compiler.py",
        "schemas/snapshot.schema.json",
        "contracts/metric_contracts.yml",
        "data_service/service.py",
        ".venv/pyvenv.cfg",
        ".memory/index.json",
        "var/query.log",
        ".env",
        "server.key",
    ]
    for relative in forbidden_files:
        path = source / relative
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text("must not ship\n", encoding="utf-8")

    completed = subprocess.run(
        [
            "bash",
            str(ROOT / "scripts" / "build-enterprise-knowledge-bundle.sh"),
            str(source),
            str(destination),
        ],
        check=False,
        capture_output=True,
        text=True,
    )

    assert completed.returncode == 0, completed.stderr
    assert (destination / "PRIMER.md").is_file()
    assert (destination / "workflows" / "query.md").is_file()
    assert (destination / "semantic_models" / "project.mdl.yml").is_file()
    assert (destination / "departments" / "sales" / "glossary.md").is_file()
    assert (destination / "wiki" / "semantic" / "sales.mdl.yml").is_file()
    for relative in forbidden_files:
        assert not (destination / relative).exists(), relative
