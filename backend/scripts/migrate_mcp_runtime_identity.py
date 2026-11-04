"""Rebuild MCP-bearing Profile Desired snapshots with mcp_id-based keys.

Dry-run is the default.  Canary apply requires one explicit user id; fleet
apply requires both ``--all-users`` and ``--confirm-all-users``.  The command
only advances MySQL Desired/Registry revisions and their normal convergence
jobs.  It does not call Runtime, Kubernetes, or PVC APIs directly.
"""

from __future__ import annotations

import argparse
import asyncio
from dataclasses import asdict
import json
from pathlib import Path
import sys
from uuid import uuid4


BACKEND_ROOT = Path(__file__).resolve().parents[1]
if str(BACKEND_ROOT) not in sys.path:
    sys.path.insert(0, str(BACKEND_ROOT))

from app.database import AsyncSessionLocal, engine  # noqa: E402
from app.services.mcp_identity_migration import (  # noqa: E402
    McpIdentityMigrationService,
)


def _validate_scope(args: argparse.Namespace) -> int | None:
    if args.user_id is not None and args.all_users:
        raise ValueError("choose either --user-id or --all-users")
    if args.user_id is None and not args.all_users:
        raise ValueError("provide --user-id for canary or --all-users for fleet")
    if args.apply and args.user_id is not None and args.confirm_user_id != args.user_id:
        raise ValueError("canary apply requires matching --confirm-user-id")
    if args.apply and args.all_users and not args.confirm_all_users:
        raise ValueError("fleet apply requires --confirm-all-users")
    return args.user_id


async def run(args: argparse.Namespace) -> int:
    try:
        user_id = _validate_scope(args)
    except ValueError as exc:
        print(json.dumps({"mode": "blocked", "detail": str(exc)}), file=sys.stderr)
        return 2
    operation_id = f"mcp-runtime-identity-{uuid4().hex}"
    async with AsyncSessionLocal() as db:
        try:
            result = await McpIdentityMigrationService(db).run(
                operation_id=operation_id,
                created_by_user_id=args.created_by_user_id,
                user_id=user_id,
                apply=args.apply,
            )
            if args.apply:
                await db.commit()
            else:
                await db.rollback()
            print(
                json.dumps(
                    {
                        "mode": "apply" if args.apply else "dry-run",
                        "scope": "all-users" if args.all_users else "single-user",
                        "user_id": user_id,
                        "operation_id": operation_id,
                        **asdict(result),
                    },
                    ensure_ascii=False,
                    sort_keys=True,
                )
            )
            return 0
        except Exception as exc:
            await db.rollback()
            print(
                json.dumps(
                    {
                        "mode": "blocked",
                        "error_type": type(exc).__name__,
                        "detail": str(exc),
                    },
                    ensure_ascii=False,
                    sort_keys=True,
                ),
                file=sys.stderr,
            )
            return 2


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Canary then fleet migration to mcp_id-based Runtime MCP keys"
    )
    parser.add_argument("--user-id", type=int, help="stable user id for canary")
    parser.add_argument("--all-users", action="store_true", help="select every MCP user")
    parser.add_argument("--apply", action="store_true", help="commit Desired revisions")
    parser.add_argument("--confirm-user-id", type=int)
    parser.add_argument("--confirm-all-users", action="store_true")
    parser.add_argument("--created-by-user-id", type=int, required=True)
    args = parser.parse_args()

    async def execute() -> int:
        try:
            return await run(args)
        finally:
            await engine.dispose()

    raise SystemExit(asyncio.run(execute()))


if __name__ == "__main__":
    main()
