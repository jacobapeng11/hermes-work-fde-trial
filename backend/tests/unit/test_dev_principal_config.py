from __future__ import annotations

import pytest
from fastapi import HTTPException

from app.api.v1.user.runtime_workspace import configured_dev_principal
from app.config import settings


def test_contract_mode_uses_virtual_dev_principal(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(settings, "RUNTIME_CHAT_MODE", "contract")
    monkeypatch.setattr(settings, "DEV_PRINCIPAL_USER_ID", -1)

    principal = configured_dev_principal()

    assert (principal.org_id, principal.user_id) == (settings.DEFAULT_ORG_ID, -1)


def test_real_mode_requires_explicit_existing_principal(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(settings, "RUNTIME_CHAT_MODE", "real")
    monkeypatch.setattr(settings, "DEV_PRINCIPAL_USER_ID", -1)

    with pytest.raises(HTTPException) as raised:
        configured_dev_principal()

    assert raised.value.status_code == 503


def test_real_mode_accepts_explicit_positive_principal(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(settings, "RUNTIME_CHAT_MODE", "real")
    monkeypatch.setattr(settings, "DEV_PRINCIPAL_USER_ID", 990177)

    principal = configured_dev_principal()

    assert (principal.org_id, principal.user_id) == (
        settings.DEFAULT_ORG_ID,
        990177,
    )
