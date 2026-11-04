"""Business assertions for the Skill/MCP Profile resume smoke."""

from __future__ import annotations

import json
from typing import Any


def acceptance_failures(
    *,
    suffix: str,
    evidence: dict[str, Any],
    markers: dict[str, bool],
) -> list[str]:
    failures = [
        f"marker_not_observed:{name}"
        for name, observed in markers.items()
        if not observed
    ]
    required_runs = {
        "registry-keeper": evidence["registry_keeper"]["run"],
        "E0-alpha": evidence["E0"]["alpha"],
        "E0-beta": evidence["E0"]["beta"],
        "E1-old": evidence["E1"]["old_run"],
        "E1-new": evidence["E1"]["new_run"],
        "E3-new": evidence["E3"]["new_run"],
        "E3-same": evidence["E3"]["same_run"],
        "E4-new": evidence["E4"]["new_run"],
        "E4-same": evidence["E4"]["same_run"],
        "E5-old": evidence["E5"]["old_run"],
        "E5-next": evidence["E5"]["next_run"],
        "E6-beta-during": evidence["E6"]["beta_during"],
        "E6-beta-after": evidence["E6"]["beta_after"],
        "E7-v1": evidence["E7"]["v1"],
        "E7-v2": evidence["E7"]["v2"],
    }
    failures.extend(
        f"run_error:{label}:{run['error']}"
        for label, run in required_runs.items()
        if run.get("error")
    )
    required_markers = {
        "registry-keeper-v1": (
            evidence["registry_keeper"]["run"],
            f"ALPHA_V1_{suffix}",
        ),
        "registry-keeper-v2": (
            evidence["registry_keeper"]["run"],
            f"ALPHA_V2_{suffix}",
        ),
        "E1-new-skill": (
            evidence["E1"]["new_run"],
            f"ALPHA_SKILL_V2_{suffix}",
        ),
        "E1-new-mcp": (
            evidence["E1"]["new_run"],
            f"ALPHA_V2_{suffix}",
        ),
        "E3-same-skill": (
            evidence["E3"]["same_run"],
            f"ALPHA_SKILL_V2_{suffix}",
        ),
        "E4-new-mcp": (
            evidence["E4"]["new_run"],
            f"ALPHA_V2_{suffix}",
        ),
        "E5-next-mcp": (
            evidence["E5"]["next_run"],
            f"ALPHA_V2_{suffix}",
        ),
        "E7-v2-mcp": (
            evidence["E7"]["v2"],
            f"ALPHA_V2_{suffix}",
        ),
    }
    failures.extend(
        f"run_marker_not_observed:{label}"
        for label, (run, marker) in required_markers.items()
        if marker not in json.dumps(run, ensure_ascii=False)
    )
    return failures
