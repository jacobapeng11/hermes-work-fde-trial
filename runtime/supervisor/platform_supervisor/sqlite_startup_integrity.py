from __future__ import annotations

import hashlib
import json
import logging
import os
from pathlib import Path
from typing import Any


logger = logging.getLogger(__name__)
_RECEIPT_FORMAT_VERSION = 1


class SQLiteStartupIntegrityCache:
    """Reuse integrity evidence only for the same cleanly closed database."""

    def __init__(self, database_path: Path, receipt_path: Path) -> None:
        self.database_path = database_path
        self.receipt_path = receipt_path

    def can_reuse(self) -> bool:
        receipt = self._read_receipt()
        fingerprint = self._fingerprint()
        return bool(
            fingerprint
            and receipt
            and receipt.get("format_version") == _RECEIPT_FORMAT_VERSION
            and receipt.get("result") == "passed"
            and receipt.get("clean_shutdown") is True
            and receipt.get("fingerprint") == fingerprint
        )

    def record_passed(self) -> None:
        fingerprint = self._fingerprint()
        if fingerprint is None:
            return
        self._write_receipt(
            {
                "format_version": _RECEIPT_FORMAT_VERSION,
                "fingerprint": fingerprint,
                "result": "passed",
                "clean_shutdown": False,
            }
        )

    def record_clean_shutdown(self) -> None:
        receipt = self._read_receipt()
        fingerprint = self._fingerprint()
        if not receipt or receipt.get("result") != "passed" or fingerprint is None:
            return
        self._write_receipt(
            {
                "format_version": _RECEIPT_FORMAT_VERSION,
                "fingerprint": fingerprint,
                "result": "passed",
                "clean_shutdown": True,
            }
        )

    def _fingerprint(self) -> str | None:
        try:
            stat_result = self.database_path.stat()
            with self.database_path.open("rb") as database_file:
                header = database_file.read(100)
        except OSError:
            return None
        if len(header) != 100:
            return None
        digest = hashlib.sha256()
        digest.update(header)
        digest.update(f"\0{stat_result.st_size}\0{stat_result.st_mtime_ns}".encode("utf-8"))
        return digest.hexdigest()

    def _read_receipt(self) -> dict[str, Any] | None:
        try:
            payload = json.loads(self.receipt_path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return None
        return payload if isinstance(payload, dict) else None

    def _write_receipt(self, payload: dict[str, Any]) -> None:
        temporary_path = self.receipt_path.with_name(f"{self.receipt_path.name}.tmp-{os.getpid()}")
        try:
            self.receipt_path.parent.mkdir(parents=True, exist_ok=True)
            with temporary_path.open("w", encoding="utf-8") as receipt_file:
                json.dump(payload, receipt_file, sort_keys=True, separators=(",", ":"))
                receipt_file.write("\n")
                receipt_file.flush()
                os.fsync(receipt_file.fileno())
            os.replace(temporary_path, self.receipt_path)
        except OSError:
            logger.warning(
                "SQLite startup integrity receipt write failed",
                extra={"component": "supervisor", "event": "sqlite_receipt_write_failed"},
            )
        finally:
            try:
                temporary_path.unlink()
            except FileNotFoundError:
                pass
            except OSError:
                logger.warning(
                    "SQLite startup integrity receipt cleanup failed",
                    extra={
                        "component": "supervisor",
                        "event": "sqlite_receipt_cleanup_failed",
                    },
                )
