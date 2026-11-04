from __future__ import annotations

import pytest
from types import SimpleNamespace

from app.services.admin_auth import AdminPrincipal
from app.services.admin_resources import (
    AdminResourceViewService,
    AdminRuntimeWorkspaceResourceFilter,
    AdminRuntimeWorkspaceResourceRecord,
    InMemoryAdminResourceViewStore,
    SqlAlchemyAdminResourceViewStore,
)


def admin() -> AdminPrincipal:
    return AdminPrincipal(org_id=1, user_id=1, roles=("admin",))


@pytest.mark.asyncio
async def test_admin_resource_view_aggregates_runtime_workspace_state() -> None:
    result = await AdminResourceViewService(InMemoryAdminResourceViewStore()).list_runtime_workspaces(admin())

    assert len(result) == 1
    record = result[0]
    assert record.runtime_workspace_id == 1
    assert record.employee_no == "dev-user"
    assert record.user_name == "Admin User"
    assert record.runtime_name == "默认数字人"
    assert record.runtime_status == "ready"
    assert record.session_count == 2
    assert record.run_count == 5
    assert record.active_run_count == 1
    assert record.installed_expert_count == 1
    assert record.installed_skill_count == 2
    assert record.installed_mcp_count == 1
    assert record.installed_knowledge_base_count == 1
    assert record.default_model_display_name == "GPT-4.1 Mini"


def record(**overrides) -> AdminRuntimeWorkspaceResourceRecord:
    values = {
        "runtime_workspace_id": 1,
        "cloud_profile_id": 1,
        "org_id": 1,
        "user_id": 1,
        "employee_no": "E001",
        "user_name": "Alice Admin",
        "runtime_name": "Runtime 1",
        "profile_status": "active",
        "binding_status": "ready",
        "runtime_status": "ready",
        "default_model_entry_id": None,
        "default_model_display_name": None,
        "session_count": 0,
        "run_count": 0,
        "active_run_count": 0,
        "installed_expert_count": 0,
        "installed_skill_count": 0,
        "installed_mcp_count": 0,
        "installed_knowledge_base_count": 0,
        "pending_reload": False,
        "latest_error": None,
    }
    values.update(overrides)
    return AdminRuntimeWorkspaceResourceRecord(**values)


@pytest.mark.asyncio
async def test_admin_resource_view_filters_by_employee_user_name_and_runtime_status() -> None:
    service = AdminResourceViewService(
        InMemoryAdminResourceViewStore(
            [
                record(runtime_workspace_id=1, employee_no="E001", user_name="Alice Admin", runtime_status="ready"),
                record(runtime_workspace_id=2, employee_no="E002", user_name="Bob Builder", runtime_status="failed"),
                record(runtime_workspace_id=3, employee_no="E003", user_name="Alicia User", runtime_status="ready"),
            ]
        )
    )

    by_employee = await service.list_runtime_workspaces(admin(), filters=AdminRuntimeWorkspaceResourceFilter(employee_no="E002"))
    by_name = await service.list_runtime_workspaces(admin(), filters=AdminRuntimeWorkspaceResourceFilter(user_name="ali"))
    by_status = await service.list_runtime_workspaces(admin(), filters=AdminRuntimeWorkspaceResourceFilter(runtime_status="ready"))

    assert [item.runtime_workspace_id for item in by_employee] == [2]
    assert [item.runtime_workspace_id for item in by_name] == [1, 3]
    assert [item.runtime_workspace_id for item in by_status] == [1, 3]


@pytest.mark.asyncio
async def test_admin_resource_view_preserves_empty_and_duplicate_employee_numbers() -> None:
    service = AdminResourceViewService(
        InMemoryAdminResourceViewStore(
            [
                record(runtime_workspace_id=1, employee_no=None, user_name="No Code"),
                record(runtime_workspace_id=2, employee_no="DUP", user_name="Duplicate A"),
                record(runtime_workspace_id=3, employee_no="DUP", user_name="Duplicate B"),
            ]
        )
    )

    all_records = await service.list_runtime_workspaces(admin())
    duplicate_records = await service.list_runtime_workspaces(admin(), filters=AdminRuntimeWorkspaceResourceFilter(employee_no="DUP"))

    assert [item.employee_no for item in all_records] == [None, "DUP", "DUP"]
    assert [item.runtime_workspace_id for item in duplicate_records] == [2, 3]


class Result:
    def __init__(self, *, rows=None, scalar=None) -> None:
        self.rows = rows or []
        self.scalar = scalar

    def all(self):
        return self.rows

    def scalar_one(self):
        return self.scalar

    def scalar_one_or_none(self):
        return self.scalar


class FakeDb:
    def __init__(self, rows) -> None:
        self.results = [Result(rows=rows)]
        self.statements = []

    async def execute(self, statement):
        self.statements.append(statement)
        if self.results:
            return self.results.pop(0)
        return Result(scalar=0)


class ApplicationResourceDb:
    def __init__(self, agent, creator, binding, model_entry) -> None:
        self.agent = agent
        self.creator = creator
        self.binding = binding
        self.model_entry = model_entry

    async def execute(self, statement):
        sql = str(statement)
        if "FROM cloud_hermes_profile" in sql:
            return Result(rows=[])
        if (
            "FROM application_agent LEFT OUTER JOIN" in sql
            and "application_agent_runtime_binding" in sql
        ):
            return Result(
                rows=[(self.agent, self.creator, self.binding)]
            )
        if "FROM model_entry" in sql:
            return Result(scalar=self.model_entry)
        if "FROM application_agent_conversation" in sql:
            return Result(scalar=4)
        if "count(*)" in sql and "FROM application_agent_run" in sql:
            return Result(scalar=9)
        if "FROM application_agent_personal_knowledge_binding" in sql:
            return Result(scalar=2)
        if "application_agent_run.error_code" in sql:
            return Result(scalar=None)
        raise AssertionError(f"unexpected statement: {sql}")


@pytest.mark.asyncio
async def test_sqlalchemy_resource_view_keeps_workspace_when_user_row_is_missing() -> None:
    profile = SimpleNamespace(
        id=4,
        org_id=1,
        user_id=404,
        name="Orphan Runtime",
        status="active",
        default_model_id=None,
    )
    db = FakeDb(rows=[(profile, None, None, None)])

    records = await SqlAlchemyAdminResourceViewStore(db).list_runtime_workspaces(
        org_id=1,
        filters=AdminRuntimeWorkspaceResourceFilter(),
    )

    assert len(records) == 1
    assert records[0].user_id == 404
    assert records[0].employee_no is None
    assert records[0].user_name == "Unknown User"


@pytest.mark.asyncio
async def test_sqlalchemy_resource_view_applies_filter_conditions_to_query() -> None:
    db = FakeDb(rows=[])

    await SqlAlchemyAdminResourceViewStore(db).list_runtime_workspaces(
        org_id=1,
        filters=AdminRuntimeWorkspaceResourceFilter(
            employee_no="E001",
            user_name="Alice",
            runtime_status="ready",
        ),
    )

    statement = str(db.statements[0])
    assert "user_code" in statement
    assert "name LIKE" in statement
    assert "hermes_runtime_binding.status" in statement


@pytest.mark.asyncio
async def test_sqlalchemy_resource_view_includes_application_agent_runtime() -> None:
    agent = SimpleNamespace(
        id=19,
        org_id=1,
        created_by_user_id=7,
        name="test5",
        status="published",
        apply_status="applied",
        config_generation=5,
        last_apply_error_code=None,
        config={
            "model_entry_id": 3,
            "skill_asset_ids": [10, 10, 11],
            "mcp_asset_ids": [20],
        },
    )
    creator = SimpleNamespace(name="示例用户乙", user_code="10002")
    binding = SimpleNamespace(
        status="ready",
        active_run_count=2,
        config_generation=5,
        last_error_code=None,
        knative_service="application-agent-19",
    )
    model_entry = SimpleNamespace(id=3, display_name="Model 3")
    db = ApplicationResourceDb(agent, creator, binding, model_entry)

    records = await SqlAlchemyAdminResourceViewStore(
        db
    ).list_runtime_workspaces(
        org_id=1,
        filters=AdminRuntimeWorkspaceResourceFilter(),
    )

    assert len(records) == 1
    record = records[0]
    assert record.resource_key == "application:19"
    assert record.application_agent_id == 19
    assert record.runtime_name == "test5"
    assert record.user_name == "示例用户乙"
    assert record.employee_no == "10002"
    assert record.kservice_name == "application-agent-19"
    assert record.session_count == 4
    assert record.run_count == 9
    assert record.active_run_count == 2
    assert record.installed_skill_count == 2
    assert record.installed_mcp_count == 1
    assert record.installed_knowledge_base_count == 2
