#!/usr/bin/env python3
"""Dry-run or explicitly apply failed expert cleanup for one exact user."""

from __future__ import annotations

import argparse
import asyncio
from datetime import datetime, timedelta, timezone
import json
from pathlib import Path
import sys
from typing import Sequence

if __package__ in {None, ""}:
    sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app.database import AsyncSessionLocal
from app.hermes_runtime.profile_adapter import HermesProfileAdapter
from app.services.failed_expert_cleanup import (
    FailedExpertCleanupIdentity,
    FailedExpertCleanupRejected,
    FailedExpertCleanupService,
)
from app.services.runtime_infrastructure import build_runtime_protocol_adapters


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Safely inspect or clean explicitly confirmed failed experts.",
    )
    parser.add_argument("--user-name", required=True)
    parser.add_argument("--stale-hours", type=int, default=24)
    parser.add_argument("--apply", action="store_true")
    parser.add_argument("--org-id", type=int)
    parser.add_argument("--user-id", type=int)
    parser.add_argument("--runtime-workspace-id", type=int)
    parser.add_argument("--profile-id", type=int, action="append", default=[])
    return parser


def _apply_identity(args: argparse.Namespace) -> FailedExpertCleanupIdentity:
    values = (args.org_id, args.user_id, args.runtime_workspace_id)
    if not args.apply or any(value is None or value <= 0 for value in values):
        raise FailedExpertCleanupRejected(
            "apply requires positive --org-id, --user-id, and --runtime-workspace-id"
        )
    if not args.profile_id or any(value <= 0 for value in args.profile_id):
        raise FailedExpertCleanupRejected(
            "apply requires one or more positive --profile-id values"
        )
    return FailedExpertCleanupIdentity(
        org_id=args.org_id,
        user_id=args.user_id,
        runtime_workspace_id=args.runtime_workspace_id,
    )


async def run(args: argparse.Namespace) -> dict:
    if args.stale_hours <= 0:
        raise FailedExpertCleanupRejected("--stale-hours must be positive")
    stale_before = datetime.now(timezone.utc) - timedelta(hours=args.stale_hours)
    dashboard = build_runtime_protocol_adapters().dashboard_adapter
    async with AsyncSessionLocal() as db:
        service = FailedExpertCleanupService(
            db,
            hermes_profiles=HermesProfileAdapter(dashboard),
        )
        if not args.apply:
            return (
                await service.dry_run(
                    user_name=args.user_name,
                    stale_before=stale_before,
                )
            ).to_dict()
        identity = _apply_identity(args)
        return (
            await service.apply(
                user_name=args.user_name,
                identity=identity,
                profile_ids=args.profile_id,
                stale_before=stale_before,
            )
        ).to_dict()


def main(argv: Sequence[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    try:
        report = asyncio.run(run(args))
    except FailedExpertCleanupRejected as exc:
        print(json.dumps({"status": "rejected", "reason": str(exc)}, ensure_ascii=False))
        return 2
    print(json.dumps(report, ensure_ascii=False, sort_keys=True))
    if any(item.get("status") == "failed" for item in report.get("results", [])):
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
