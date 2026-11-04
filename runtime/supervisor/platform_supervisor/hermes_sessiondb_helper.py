from __future__ import annotations

import contextlib
import hashlib
import json
import os
from pathlib import Path
import re
import shutil
import stat
import sys
from typing import Any, NoReturn


PROTOCOL = "hermes-sessiondb-helper-f80f453-v1"
TARGET_HERMES_COMMIT = "f80f453ae0679347e38abc917c7f94f717bf96c5"
TARGET_STATE_SCHEMA = 25
MAX_REQUEST_BYTES = 1024 * 1024
_SHA256 = re.compile(r"^sha256:[a-f0-9]{64}$")
_REQUEST_KEYS = {
    "contract_version",
    "intent_digest",
    "db_key",
    "db_path",
    "open_ordinal",
    "target_hermes_commit",
    "binding_effective_cjk_fts",
    "cjk_extension_path",
    "cjk_extension_digest",
    "cjk_extension_abi",
}


class HelperProtocolError(RuntimeError):
    pass


def _canonical_json(value: object) -> bytes:
    return json.dumps(
        value,
        ensure_ascii=False,
        allow_nan=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")


def _digest(value: object) -> str:
    return f"sha256:{hashlib.sha256(_canonical_json(value)).hexdigest()}"


def _read_request() -> dict[str, Any]:
    encoded = sys.stdin.buffer.read(MAX_REQUEST_BYTES + 1)
    if not encoded or len(encoded) > MAX_REQUEST_BYTES:
        raise HelperProtocolError("helper request size is invalid")
    try:
        value = json.loads(encoded)
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise HelperProtocolError("helper request JSON is invalid") from exc
    if not isinstance(value, dict) or set(value) != _REQUEST_KEYS:
        raise HelperProtocolError("helper request shape is not closed")
    if (
        value["contract_version"] != PROTOCOL
        or value["target_hermes_commit"] != TARGET_HERMES_COMMIT
        or _SHA256.fullmatch(value.get("intent_digest", "")) is None
        or not isinstance(value.get("db_key"), str)
        or not value["db_key"]
        or isinstance(value.get("open_ordinal"), bool)
        or value.get("open_ordinal") not in {1, 2, 3}
        or not isinstance(value.get("binding_effective_cjk_fts"), bool)
        or _SHA256.fullmatch(value.get("cjk_extension_digest", "")) is None
        or not isinstance(value.get("cjk_extension_abi"), str)
        or not value["cjk_extension_abi"]
    ):
        raise HelperProtocolError("helper request fields are invalid")
    for key in ("db_path", "cjk_extension_path"):
        selected = value.get(key)
        if not isinstance(selected, str) or not Path(selected).is_absolute():
            raise HelperProtocolError(f"helper {key} is not absolute")
    return value


def _hash_regular_nofollow(path: Path) -> str:
    flags = os.O_RDONLY | os.O_CLOEXEC
    if hasattr(os, "O_NOFOLLOW"):
        flags |= os.O_NOFOLLOW
    fd = os.open(path, flags)
    try:
        metadata = os.fstat(fd)
        if not stat.S_ISREG(metadata.st_mode):
            raise HelperProtocolError("helper extension is not a regular file")
        digest = hashlib.sha256()
        while True:
            chunk = os.read(fd, 1024 * 1024)
            if not chunk:
                break
            digest.update(chunk)
        return f"sha256:{digest.hexdigest()}"
    finally:
        os.close(fd)


def _validate_environment(request: dict[str, Any]) -> None:
    expected = {
        "HERMES_SKIP_CONFIG_MIGRATION": "1",
        "HERMES_CJK_FTS": (
            "1" if request["binding_effective_cjk_fts"] else "0"
        ),
        "HERMES_FTS5_CJK_SO": request["cjk_extension_path"],
        "PYTHONNOUSERSITE": "1",
        "PYTHONDONTWRITEBYTECODE": "1",
    }
    if any(os.environ.get(key) != value for key, value in expected.items()):
        raise HelperProtocolError("helper environment does not match request")
    home = os.environ.get("HERMES_HOME")
    if home is None or not Path(home).is_absolute():
        raise HelperProtocolError("helper HERMES_HOME is invalid")
    forbidden = {
        "PLATFORM_CONTROL_ROOT_KEY",
        "PLATFORM_CONTROL_KEYS",
        "NEWAPI_API_KEY",
        "OPENAI_API_KEY",
        "ANTHROPIC_API_KEY",
    }
    if any(os.environ.get(key) for key in forbidden):
        raise HelperProtocolError("helper inherited a forbidden secret")
    extension = Path(request["cjk_extension_path"])
    if _hash_regular_nofollow(extension) != request["cjk_extension_digest"]:
        raise HelperProtocolError("helper CJK extension digest does not match")


def _validate_database_path(path: Path) -> None:
    parent = path.parent
    parent_stat = os.lstat(parent)
    if not stat.S_ISDIR(parent_stat.st_mode):
        raise HelperProtocolError("helper database parent is not a directory")
    try:
        metadata = os.lstat(path)
    except FileNotFoundError:
        return
    if not stat.S_ISREG(metadata.st_mode):
        raise HelperProtocolError("helper database is not a regular file")


def _cleanup_ephemeral_home() -> None:
    """Remove child-owned upstream caches before returning to UID 10000."""

    selected = os.environ.get("HERMES_HOME")
    if not selected:
        return
    home = Path(selected)
    try:
        metadata = os.lstat(home)
    except FileNotFoundError:
        return
    if (
        not home.is_absolute()
        or not home.name.startswith("hermes-schema-home-")
        or not stat.S_ISDIR(metadata.st_mode)
        or stat.S_ISLNK(metadata.st_mode)
    ):
        return
    for child in home.iterdir():
        if child.is_dir() and not child.is_symlink():
            shutil.rmtree(child)
        else:
            child.unlink()


def run() -> None:
    request = _read_request()
    _validate_environment(request)
    db_path = Path(request["db_path"])
    helper_home = Path(os.environ["HERMES_HOME"])
    if db_path.is_relative_to(helper_home):
        raise HelperProtocolError("helper database cannot be inside ephemeral home")
    _validate_database_path(db_path)

    # The only writable schema operation is the fixed upstream SessionDB
    # constructor/close pair.  This helper contains no DDL, patch, monkey
    # patch, executescript, repair fallback, or import of Supervisor storage.
    with contextlib.redirect_stdout(sys.stderr):
        from hermes_state import SessionDB
        from hermes_state_common import SCHEMA_VERSION

        if SCHEMA_VERSION != TARGET_STATE_SCHEMA:
            raise HelperProtocolError("target Hermes schema version is not 25")
        database = SessionDB(db_path=db_path)
        try:
            pass
        finally:
            database.close()

    unsigned = {
        "contract_version": PROTOCOL,
        "intent_digest": request["intent_digest"],
        "db_key": request["db_key"],
        "open_ordinal": request["open_ordinal"],
        "target_hermes_commit": TARGET_HERMES_COMMIT,
        "completed": True,
    }
    result = {**unsigned, "result_digest": _digest(unsigned)}
    sys.stdout.buffer.write(_canonical_json(result))
    sys.stdout.buffer.flush()


def main() -> NoReturn:
    try:
        run()
    except BaseException as exc:
        print(f"HERMES_SCHEMA_HELPER_FAILED:{type(exc).__name__}", file=sys.stderr)
        raise SystemExit(70) from None
    finally:
        _cleanup_ephemeral_home()
    raise SystemExit(0)


if __name__ == "__main__":
    main()
