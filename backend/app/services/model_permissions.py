from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class PermissionGrant:
    grantee_type: str
    grantee_id: str


def model_permission_allows(
    grants: list[PermissionGrant],
    *,
    user_id: int,
    department_ids: set[str] | None = None,
) -> bool:
    if not grants:
        return True

    departments = department_ids or set()
    user_id_text = str(user_id)
    for grant in grants:
        if grant.grantee_type == "all":
            return True
        if grant.grantee_type == "user" and grant.grantee_id == user_id_text:
            return True
        if grant.grantee_type == "department" and grant.grantee_id in departments:
            return True
    return False
