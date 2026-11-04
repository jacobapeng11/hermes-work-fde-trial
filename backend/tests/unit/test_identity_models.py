from app.models import (
    Base,
    Permission,
    Role,
    SysDept,
    SysUser,
    User,
    UserRole,
)


def test_identity_tables_are_registered() -> None:
    table_names = set(Base.metadata.tables)

    assert {
        "organization",
        "user",
        "role",
        "user_role",
        "permission",
        "sys_user",
        "sys_dept",
    }.issubset(table_names)


def test_platform_user_primary_key_is_runtime_owner_candidate() -> None:
    user_table = User.__table__

    assert user_table.c.id.primary_key
    assert not user_table.c.hrm_user_id.primary_key
    assert user_table.c.org_id.foreign_keys


def test_core_constraints_exist() -> None:
    constraint_names = {
        constraint.name
        for table in (
            User.__table__,
            Role.__table__,
            UserRole.__table__,
            Permission.__table__,
        )
        for constraint in table.constraints
    }

    assert "uq_user_org_code" in constraint_names
    assert "uq_user_org_sso_subject" in constraint_names
    assert "uq_role_org_code" in constraint_names
    assert "uq_user_role" in constraint_names
    assert "uq_permission_grantee" in constraint_names


def test_hrm_tables_keep_external_identity_separate() -> None:
    assert SysUser.__table__.c.user_id.primary_key
    assert SysDept.__table__.c.dept_id.primary_key
    assert "org_id" not in SysUser.__table__.c


def test_platform_user_keeps_work_number_as_stable_external_mapping() -> None:
    user_table = User.__table__

    assert "user_code" in user_table.c
    assert "sso_subject" in user_table.c
    assert "qw_token" in user_table.c
    assert user_table.c.qw_token.type.length == 100
    assert "last_login_at" in user_table.c
    assert "last_daily_presence_at" in user_table.c
    assert "ix_user_org_qw_token" in {
        index.name for index in user_table.indexes
    }
