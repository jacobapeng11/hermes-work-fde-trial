"""Shared data contracts and the smoke's sole checkpoint writer."""

from __future__ import annotations

from dataclasses import asdict, dataclass, field
import json
from pathlib import Path
import time
from typing import Any


class SmokeError(RuntimeError):
    """A deterministic smoke precondition or acceptance failure."""


@dataclass(frozen=True)
class Target:
    principal: Any
    user_code: str
    user_name: str
    workspace_id: int
    runtime_id: str
    endpoint: Any
    command_target: Any


@dataclass
class RunEvidence:
    label: str
    session_id: str
    run_id: str = ""
    provider_live_ids: list[str] = field(default_factory=list)
    events: list[dict[str, Any]] = field(default_factory=list)
    reasoning: str = ""
    final_text: str = ""
    tools: list[dict[str, Any]] = field(default_factory=list)
    error: str | None = None


@dataclass
class Checkpoint:
    suffix: str
    started_at: str
    sessions: list[str] = field(default_factory=list)
    experts: list[int] = field(default_factory=list)
    mcp_assets: list[dict[str, Any]] = field(default_factory=list)
    run_ids: list[str] = field(default_factory=list)
    control_methods: list[dict[str, Any]] = field(default_factory=list)
    evidence: dict[str, Any] = field(default_factory=dict)


class CheckpointStore:
    """Persist all evidence atomically; no other module writes checkpoint files."""

    def __init__(self, checkpoint: Checkpoint, path: Path) -> None:
        self.checkpoint = checkpoint
        self.path = path

    def save(self) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        temporary = self.path.with_suffix(".tmp")
        temporary.write_text(
            json.dumps(
                asdict(self.checkpoint),
                ensure_ascii=False,
                indent=2,
                sort_keys=True,
            ),
            encoding="utf-8",
        )
        temporary.replace(self.path)

    def record_control(self, method: str, **details: Any) -> None:
        self.checkpoint.control_methods.append(
            {"at": time.time(), "method": method, **details}
        )
        self.save()
