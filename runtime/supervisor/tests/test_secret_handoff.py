from __future__ import annotations

import os
from pathlib import Path

import pytest

from platform_supervisor.secret_handoff import (
    PLATFORM_CONTROL_KEY_FD_ENV,
    RootSecretHandoffError,
    consume_root_secret_handoff,
    prepare_root_secret_handoffs,
)


def _root_only_document(path: Path, content: bytes = b'{"safe":true}') -> None:
    path.write_bytes(content)
    path.chmod(0o400)


def test_root_entry_hands_off_fd_without_secret_bytes_in_environment(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    document = tmp_path / "control-keys.json"
    _root_only_document(document)

    environment, descriptors = prepare_root_secret_handoffs(
        {"VISIBLE": "safe"},
        documents=((PLATFORM_CONTROL_KEY_FD_ENV, document),),
    )
    descriptor = descriptors[0]
    assert environment == {
        "VISIBLE": "safe",
        PLATFORM_CONTROL_KEY_FD_ENV: str(descriptor),
    }
    assert os.get_inheritable(descriptor) is True

    monkeypatch.setenv(PLATFORM_CONTROL_KEY_FD_ENV, str(descriptor))
    assert consume_root_secret_handoff(PLATFORM_CONTROL_KEY_FD_ENV) == b'{"safe":true}'
    assert PLATFORM_CONTROL_KEY_FD_ENV not in os.environ
    with pytest.raises(OSError):
        os.fstat(descriptor)


def test_root_entry_rejects_group_readable_secret(tmp_path: Path) -> None:
    document = tmp_path / "control-keys.json"
    _root_only_document(document)
    document.chmod(0o440)

    with pytest.raises(RootSecretHandoffError, match="permissions are unsafe"):
        prepare_root_secret_handoffs(
            {},
            documents=((PLATFORM_CONTROL_KEY_FD_ENV, document),),
        )


def test_missing_optional_document_does_not_create_or_export_fd(
    tmp_path: Path,
) -> None:
    missing = tmp_path / "missing.json"

    environment, descriptors = prepare_root_secret_handoffs(
        {PLATFORM_CONTROL_KEY_FD_ENV: "99"},
        documents=((PLATFORM_CONTROL_KEY_FD_ENV, missing),),
    )

    assert PLATFORM_CONTROL_KEY_FD_ENV not in environment
    assert descriptors == ()
    assert not missing.exists()
