from __future__ import annotations

import io
import json
import sys
import types

from platform_supervisor import hermes_session_metadata_helper as helper


def test_helper_derives_profile_database_and_updates_only_model_metadata(
    monkeypatch,
    tmp_path,
) -> None:
    profile = tmp_path / "profiles" / "expert-73"
    profile.mkdir(parents=True)
    database = profile / "state.db"
    database.touch()
    updates: list[tuple[str, dict, str | None]] = []
    reopened: list[str] = []

    class FakeSessionDB:
        def __init__(self, *, db_path) -> None:
            assert db_path == database

        @staticmethod
        def get_session(_session_id: str) -> dict:
            return {
                "model": "19",
                "model_config": json.dumps(
                    {
                        "model": "19",
                        "provider": "custom:runtime-old",
                        "base_url": "http://127.0.0.1:9121/model-relay/old/v1",
                        "reasoning_config": {"enabled": True},
                    }
                ),
            }

        @staticmethod
        def reopen_session(session_id: str) -> None:
            reopened.append(session_id)

        @staticmethod
        def close() -> None:
            return None

        @staticmethod
        def update_session_meta(session_id, model_config_json, model=None) -> None:
            updates.append((session_id, json.loads(model_config_json), model))

    request = {
        "contract_version": "hermes-session-metadata-repair-v1",
        "runtime_session_id": "rs-legacy-runtime-route",
        "model": "deepseek-v4-flash",
        "provider": "custom:runtime-current",
        "hermes_profile_name": "expert-73",
    }
    output = io.BytesIO()
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    monkeypatch.setattr(sys, "stdin", types.SimpleNamespace(buffer=io.BytesIO(json.dumps(request).encode())))
    monkeypatch.setattr(sys, "stdout", types.SimpleNamespace(buffer=output, flush=lambda: None))
    monkeypatch.setitem(sys.modules, "hermes_state", types.SimpleNamespace(SessionDB=FakeSessionDB))

    helper.run()

    assert reopened == ["rs-legacy-runtime-route"]
    assert updates == [
        (
            "rs-legacy-runtime-route",
            {
                "model": "deepseek-v4-flash",
                "provider": "custom:runtime-current",
                "reasoning_config": {"enabled": True},
            },
            "deepseek-v4-flash",
        )
    ]
    assert output.getvalue() == b'{"ok":true}'


def test_helper_clears_stored_prompt_for_a_profile_revision_refresh(
    monkeypatch,
    tmp_path,
) -> None:
    profile = tmp_path / "profiles" / "expert-73"
    profile.mkdir(parents=True)
    (profile / "state.db").touch()
    prompts: list[tuple[str, str | None]] = []

    class FakeSessionDB:
        def __init__(self, *, db_path) -> None:
            assert db_path == profile / "state.db"

        @staticmethod
        def get_session(_session_id: str) -> dict:
            return {
                "model": "deepseek-v4-flash",
                "model_config": {
                    "model": "deepseek-v4-flash",
                    "provider": "custom:runtime-current",
                },
            }

        @staticmethod
        def reopen_session(_session_id: str) -> None:
            return None

        @staticmethod
        def update_system_prompt(session_id: str, prompt: str | None) -> None:
            prompts.append((session_id, prompt))

        @staticmethod
        def close() -> None:
            return None

    request = {
        "contract_version": "hermes-session-metadata-repair-v2",
        "runtime_session_id": "rs-profile-refresh",
        "model": "deepseek-v4-flash",
        "provider": "custom:runtime-current",
        "hermes_profile_name": "expert-73",
        "refresh_system_prompt": True,
    }
    output = io.BytesIO()
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    monkeypatch.setattr(
        sys,
        "stdin",
        types.SimpleNamespace(buffer=io.BytesIO(json.dumps(request).encode())),
    )
    monkeypatch.setattr(
        sys,
        "stdout",
        types.SimpleNamespace(buffer=output, flush=lambda: None),
    )
    monkeypatch.setitem(
        sys.modules,
        "hermes_state",
        types.SimpleNamespace(SessionDB=FakeSessionDB),
    )

    helper.run()

    assert prompts == [("rs-profile-refresh", None)]
    assert output.getvalue() == b'{"ok":true}'
