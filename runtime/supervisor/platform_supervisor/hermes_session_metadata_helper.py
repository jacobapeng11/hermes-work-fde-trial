"""Bounded SessionDB metadata repair executed as the Hermes child identity."""

from __future__ import annotations

import contextlib
import json
import os
from pathlib import Path
import re
import stat
import sys
from typing import Any, NoReturn


_MAX_REQUEST_BYTES = 4096
_REQUEST_KEYS_V1 = {
    "contract_version",
    "runtime_session_id",
    "model",
    "provider",
    "hermes_profile_name",
}
_REQUEST_KEYS_V2 = {*_REQUEST_KEYS_V1, "refresh_system_prompt"}
_SESSION_ID = re.compile(r"^[A-Za-z0-9._-]{1,200}$")
_PROFILE = re.compile(r"^(?:default|expert-[1-9][0-9]*)$")


class SessionMetadataHelperError(RuntimeError):
    pass


def _request() -> dict[str, Any]:
    encoded = sys.stdin.buffer.read(_MAX_REQUEST_BYTES + 1)
    if not encoded or len(encoded) > _MAX_REQUEST_BYTES:
        raise SessionMetadataHelperError("request size is invalid")
    try:
        value = json.loads(encoded)
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise SessionMetadataHelperError("request JSON is invalid") from exc
    if not isinstance(value, dict):
        raise SessionMetadataHelperError("request shape is not closed")
    version = value.get("contract_version")
    if version == "hermes-session-metadata-repair-v1":
        expected_keys = _REQUEST_KEYS_V1
        refresh_system_prompt = False
    elif version == "hermes-session-metadata-repair-v2":
        expected_keys = _REQUEST_KEYS_V2
        refresh_system_prompt = value.get("refresh_system_prompt")
        if not isinstance(refresh_system_prompt, bool):
            raise SessionMetadataHelperError("prompt refresh flag is invalid")
    else:
        raise SessionMetadataHelperError("request contract is invalid")
    if set(value) != expected_keys:
        raise SessionMetadataHelperError("request shape is not closed")
    for key in ("runtime_session_id", "model", "provider", "hermes_profile_name"):
        if not isinstance(value.get(key), str) or not value[key].strip():
            raise SessionMetadataHelperError("request field is invalid")
    if not _SESSION_ID.fullmatch(value["runtime_session_id"]):
        raise SessionMetadataHelperError("session identity is invalid")
    if not _PROFILE.fullmatch(value["hermes_profile_name"]):
        raise SessionMetadataHelperError("profile identity is invalid")
    if len(value["model"]) > 256 or len(value["provider"]) > 256:
        raise SessionMetadataHelperError("model identity is too long")
    request: dict[str, Any] = {
        key: str(value[key]).strip() for key in _REQUEST_KEYS_V1
    }
    request["refresh_system_prompt"] = refresh_system_prompt
    return request


def _database_path(profile: str) -> Path:
    home_text = os.environ.get("HERMES_HOME")
    if not home_text or not Path(home_text).is_absolute():
        raise SessionMetadataHelperError("managed Hermes home is invalid")
    home = Path(home_text)
    home_stat = os.lstat(home)
    if not stat.S_ISDIR(home_stat.st_mode) or stat.S_ISLNK(home_stat.st_mode):
        raise SessionMetadataHelperError("managed Hermes home is unsafe")
    path = home / "state.db" if profile == "default" else home / "profiles" / profile / "state.db"
    parent_stat = os.lstat(path.parent)
    if not stat.S_ISDIR(parent_stat.st_mode) or stat.S_ISLNK(parent_stat.st_mode):
        raise SessionMetadataHelperError("profile directory is unsafe")
    try:
        database_stat = os.lstat(path)
    except FileNotFoundError:
        raise SessionMetadataHelperError("profile database is unavailable") from None
    if not stat.S_ISREG(database_stat.st_mode) or stat.S_ISLNK(database_stat.st_mode):
        raise SessionMetadataHelperError("profile database is unsafe")
    return path


def run() -> None:
    request = _request()
    database_path = _database_path(request["hermes_profile_name"])
    with contextlib.redirect_stdout(sys.stderr):
        from hermes_state import SessionDB

        database = SessionDB(db_path=database_path)
        try:
            # Hermes Dashboard's native session.close ends legacy
            # source=api_server rows.  A skipped id from the import endpoint is
            # still authoritative, so reopen it through the public SessionDB
            # API before reading/repairing its route metadata.
            database.reopen_session(request["runtime_session_id"])
            session = database.get_session(request["runtime_session_id"])
            if not isinstance(session, dict):
                raise SessionMetadataHelperError("execution session is unavailable")
            raw_config = session.get("model_config")
            config: dict[str, Any] = {}
            if isinstance(raw_config, dict):
                config = dict(raw_config)
            elif isinstance(raw_config, str) and raw_config.strip():
                try:
                    parsed = json.loads(raw_config)
                    if isinstance(parsed, dict):
                        config = parsed
                except json.JSONDecodeError:
                    config = {}
            model = request["model"]
            provider = request["provider"]
            if (
                str(session.get("model") or "").strip() != model
                or str(config.get("model") or "").strip() != model
                or str(config.get("provider") or "").strip() != provider
            ):
                config["model"] = model
                config["provider"] = provider
                config.pop("base_url", None)
                database.update_session_meta(
                    request["runtime_session_id"],
                    json.dumps(config, ensure_ascii=False, separators=(",", ":")),
                    model,
                )
            if request["refresh_system_prompt"]:
                database.update_system_prompt(request["runtime_session_id"], None)
        finally:
            database.close()
    sys.stdout.buffer.write(b'{"ok":true}')
    sys.stdout.buffer.flush()


def main() -> NoReturn:
    try:
        run()
    except BaseException as exc:
        print(f"HERMES_SESSION_METADATA_HELPER_FAILED:{type(exc).__name__}", file=sys.stderr)
        raise SystemExit(70) from None
    raise SystemExit(0)


if __name__ == "__main__":
    main()
