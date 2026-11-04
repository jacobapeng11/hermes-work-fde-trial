from __future__ import annotations

from datetime import datetime, timezone
import json
import os
from pathlib import Path
import tempfile
from typing import Any

from app.services.runtime_image_config import RuntimeImageConfig


class RuntimeCutoverCheckpointError(RuntimeError):
    pass


class RuntimeCutoverCheckpointStore:
    """Atomic, secret-free progress hint; never a replacement for live facts."""

    def __init__(self, path: Path) -> None:
        self.path = path.resolve(strict=False)

    def load(self) -> dict[str, Any] | None:
        try:
            document = json.loads(self.path.read_text(encoding="utf-8"))
        except FileNotFoundError:
            return None
        except (OSError, json.JSONDecodeError) as exc:
            raise RuntimeCutoverCheckpointError("cutover checkpoint is unreadable") from exc
        if not isinstance(document, dict) or document.get("contract_version") != 1:
            raise RuntimeCutoverCheckpointError("cutover checkpoint contract is invalid")
        self._reject_sensitive_fields(document)
        return document

    def initialize(
        self,
        *,
        flow: str,
        target: RuntimeImageConfig,
        bindings: list[dict[str, object]],
    ) -> dict[str, Any]:
        if flow not in {"single", "fleet"}:
            raise ValueError("cutover flow must be single or fleet")
        if not target.manifest_digest or not target.config_id:
            raise ValueError("cutover target identity is incomplete")
        existing = self.load()
        identity = {
            "image": target.image,
            "manifest_digest": target.manifest_digest,
            "config_id": target.config_id,
        }
        if existing is not None:
            if existing.get("target") != identity:
                raise RuntimeCutoverCheckpointError("checkpoint target conflicts with backend/.env")
            return existing
        document: dict[str, Any] = {
            "contract_version": 1,
            "flow": flow,
            "target": identity,
            "stage": "planned",
            "status": "running",
            "bindings": bindings,
            "checks": {},
            "error_summary": None,
            "updated_at": datetime.now(timezone.utc).isoformat(),
        }
        self._write(document)
        return document

    def advance(
        self,
        *,
        stage: str,
        status: str = "running",
        checks: dict[str, object] | None = None,
        error_summary: str | None = None,
    ) -> dict[str, Any]:
        document = self.load()
        if document is None:
            raise RuntimeCutoverCheckpointError("cutover checkpoint does not exist")
        if status not in {"running", "failed", "completed"}:
            raise ValueError("cutover checkpoint status is invalid")
        document["stage"] = str(stage)
        document["status"] = status
        if checks:
            document["checks"] = {**dict(document.get("checks") or {}), **checks}
        document["error_summary"] = (
            " ".join(str(error_summary).split())[:1000] if error_summary else None
        )
        document["updated_at"] = datetime.now(timezone.utc).isoformat()
        self._write(document)
        return document

    def _write(self, document: dict[str, Any]) -> None:
        self._reject_sensitive_fields(document)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        temporary: Path | None = None
        try:
            with tempfile.NamedTemporaryFile(
                mode="w",
                encoding="utf-8",
                dir=self.path.parent,
                prefix=f".{self.path.name}.",
                suffix=".tmp",
                delete=False,
            ) as handle:
                temporary = Path(handle.name)
                os.fchmod(handle.fileno(), 0o600)
                json.dump(document, handle, ensure_ascii=False, sort_keys=True, indent=2)
                handle.write("\n")
                handle.flush()
                os.fsync(handle.fileno())
            os.replace(temporary, self.path)
            temporary = None
        finally:
            if temporary is not None:
                temporary.unlink(missing_ok=True)

    @classmethod
    def _reject_sensitive_fields(cls, value: object, *, path: str = "root") -> None:
        if isinstance(value, dict):
            for key, item in value.items():
                normalized = str(key).lower()
                if any(
                    marker in normalized for marker in ("secret", "password", "token", "api_key")
                ):
                    raise RuntimeCutoverCheckpointError(
                        f"sensitive checkpoint field is forbidden: {path}.{key}"
                    )
                cls._reject_sensitive_fields(item, path=f"{path}.{key}")
        elif isinstance(value, list):
            for index, item in enumerate(value):
                cls._reject_sensitive_fields(item, path=f"{path}[{index}]")


__all__ = ["RuntimeCutoverCheckpointError", "RuntimeCutoverCheckpointStore"]
