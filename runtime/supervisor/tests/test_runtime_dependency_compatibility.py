from __future__ import annotations

import importlib.util
from pathlib import Path

import pytest


ROOT = Path(__file__).resolve().parents[3]
SCRIPT = ROOT / "runtime/hermes-runtime-overlay/verify_dependency_compatibility.py"
SPEC = importlib.util.spec_from_file_location("verify_dependency_compatibility", SCRIPT)
assert SPEC is not None and SPEC.loader is not None
MODULE = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(MODULE)


def test_uv_check_accepts_only_the_fixed_upstream_override() -> None:
    MODULE._verify_uv_output(
        returncode=1,
        output=(
            "Checked 138 packages in 3ms\n"
            "Found 1 incompatibility\n"
            f"{MODULE.EXPECTED_CONFLICT}\n"
        ),
    )


@pytest.mark.parametrize(
    "returncode,output",
    [
        (0, "Checked 138 packages in 3ms\n"),
        (1, "Found 2 incompatibilities\n" + MODULE.EXPECTED_CONFLICT),
        (
            1,
            "Found 1 incompatibility\n"
            "The package `other` requires `dependency<1`, but `2` is installed\n",
        ),
        (2, "network or interpreter failure\n"),
    ],
)
def test_uv_check_rejects_any_other_result(returncode: int, output: str) -> None:
    with pytest.raises(MODULE.DependencyCompatibilityError):
        MODULE._verify_uv_output(returncode=returncode, output=output)


def test_fixed_upstream_project_contains_the_verified_override(tmp_path: Path) -> None:
    (tmp_path / "pyproject.toml").write_text(
        """
[project]
dependencies = ["cryptography==50.0.0"]
[tool.uv]
override-dependencies = ["cryptography>=50,<51"]
""".strip(),
        encoding="utf-8",
    )

    MODULE._verify_upstream_override(tmp_path)
