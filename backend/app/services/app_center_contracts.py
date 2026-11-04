from __future__ import annotations

from dataclasses import dataclass


class AppCenterError(RuntimeError):
    def __init__(
        self,
        code: str,
        message: str,
        *,
        status_code: int,
        error_code: int,
    ) -> None:
        super().__init__(message)
        self.code = code
        self.message = message
        self.status_code = status_code
        self.error_code = error_code


@dataclass(frozen=True)
class AppCenterPackage:
    content: bytes


__all__ = ["AppCenterError", "AppCenterPackage"]
