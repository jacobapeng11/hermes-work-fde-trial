from backend.scripts import seed


def test_seed_defaults_include_required_roles_and_permissions() -> None:
    assert ("admin", "Administrator") in seed.DEFAULT_ROLES
    assert ("user", "User") in seed.DEFAULT_ROLES
    assert (
        "application_agent_admin",
        "Application Agent Administrator",
    ) in seed.DEFAULT_ROLES
    assert "model_entry" in seed.DEFAULT_PERMISSION_ASSETS
    assert "runtime_workspace" in seed.DEFAULT_PERMISSION_ASSETS
    assert "template_review" in seed.DEFAULT_PERMISSION_ASSETS


def test_seed_does_not_create_a_fixed_development_user() -> None:
    assert not hasattr(seed, "_get_or_create_local_dev_user")
    assert "local_dev_user_created" not in seed.SeedSummary.__dataclass_fields__


def test_seed_does_not_define_plaintext_provider_key_defaults() -> None:
    exported = " ".join(
        str(value)
        for name, value in vars(seed).items()
        if name.startswith("DEFAULT_") or name.startswith("SEED_")
    )

    assert "api_key" not in exported.lower()
    assert "secret" not in exported.lower()


def test_seed_exposes_existing_user_role_bootstrap_and_identity_audit() -> None:
    assert hasattr(seed, "_ensure_default_user_roles")
    assert hasattr(seed, "_assert_identity_conflicts")


def test_seed_uses_customer_configured_admin_work_numbers() -> None:
    assert seed.parse_initial_admin_work_nos("10001, 10002,10001") == ("10001", "10002")
    assert hasattr(seed, "_ensure_initial_admin_work_no_permissions")
