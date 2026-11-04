#!/usr/bin/env python3
"""Provision one fresh personal Runtime and prove its Runtime Edge path.

This acceptance utility creates one ordinary platform user through the normal
SSO identity service, invokes the published workspace bootstrap endpoint, and
always removes that exact account/PVC/Run closure before returning. Browser
and EnterpriseIdp assertion handling are outside this approved interface simulation;
no platform login JWT is created or logged.
"""

from __future__ import annotations

import argparse
import asyncio
from dataclasses import dataclass
import json
from pathlib import Path
import sys

import httpx
from sqlalchemy import select


BACKEND_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(BACKEND_ROOT))

from app.api.v1.user.runtime_workspace import get_principal  # noqa: E402
from app.config import settings  # noqa: E402
from app.database import AsyncSessionLocal, engine  # noqa: E402
from app.main import app  # noqa: E402
from app.models import CloudHermesProfile, HermesRuntimeBinding, RuntimeBootstrapIntent  # noqa: E402
from app.services.runtime_image_config import RuntimeImageConfigStore  # noqa: E402
from app.services.runtime_workspace import Principal  # noqa: E402
from app.services.sso_identity import (  # noqa: E402
    SsoIdentityService,
    SqlAlchemySsoIdentityRepository,
)
from scripts.smoke_runtime_edge_session import (  # noqa: E402
    RuntimeEdgeSmokeError,
    SmokeTarget,
    create_bootstrap,
    run_websocket_conversation,
)
from scripts.fresh_runtime_smoke_cleanup import (  # noqa: E402
    FreshRuntimeSmokeAccount,
    FreshRuntimeSmokeCleanupError,
    acceptance_work_no,
    cleanup_fresh_runtime,
    new_acceptance_id,
    write_checkpoint,
)


class FreshRuntimeSmokeError(RuntimeError):
    """An operator-safe error; do not embed account codes, ticket, or reply."""


@dataclass(frozen=True)
class FreshRuntimeEvidence:
    principal: Principal
    runtime_workspace_id: int
    runtime_id: str
    binding_id: int
    bootstrap_intent_completed: bool
    image_matches_env: bool


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description=(
            "Create a dedicated user, bootstrap a fresh Runtime, then run one "
            "live Runtime Edge interface conversation. The exact test account "
            "and Runtime closure are always removed."
        )
    )
    parser.add_argument(
        "--runtime-edge-base",
        default=None,
        help=(
            "Optional direct Nginx Runtime Edge base when browser/EnterpriseIdp "
            "login is explicitly out of scope."
        ),
    )
    parser.add_argument("--timeout-seconds", type=float, default=600.0)
    return parser


async def create_acceptance_account() -> FreshRuntimeSmokeAccount:
    acceptance_id = new_acceptance_id()
    work_no = acceptance_work_no(acceptance_id=acceptance_id)
    async with AsyncSessionLocal() as db:
        identity = await SsoIdentityService(
            SqlAlchemySsoIdentityRepository(db)
        ).resolve_or_create(
            org_id=settings.DEFAULT_ORG_ID,
            work_no=work_no,
        )
    account = FreshRuntimeSmokeAccount(
        acceptance_id=acceptance_id,
        org_id=identity.org_id,
        user_id=identity.user_id,
        work_no=work_no,
    )
    write_checkpoint(account, status="identity_created")
    return account


async def bootstrap_workspace(
    *,
    principal: Principal,
    timeout_seconds: float,
) -> int:
    async def principal_override() -> Principal:
        return principal

    app.dependency_overrides[get_principal] = principal_override
    origin = settings.PUBLIC_WEB_BASE.rstrip("/")
    try:
        transport = httpx.ASGITransport(app=app)
        async with httpx.AsyncClient(
            transport=transport,
            base_url="http://fresh-runtime-interface-smoke.invalid",
            headers={"Origin": origin},
        ) as client:
            response = await client.post("/api/v1/user/runtime-workspace/bootstrap")
            if response.is_error:
                raise FreshRuntimeSmokeError(
                    f"Runtime workspace bootstrap returned HTTP {response.status_code}"
                )
            try:
                initial = response.json()
            except ValueError as exc:
                raise FreshRuntimeSmokeError(
                    "Runtime workspace bootstrap returned invalid JSON"
                ) from exc
            workspace_id = initial.get("runtime_workspace_id")
            if not isinstance(workspace_id, int) or isinstance(workspace_id, bool):
                raise FreshRuntimeSmokeError("Runtime workspace bootstrap response is invalid")

            deadline = asyncio.get_running_loop().time() + timeout_seconds
            while True:
                current = await client.get("/api/v1/user/runtime-workspace")
                if current.is_error:
                    raise FreshRuntimeSmokeError(
                        f"Runtime workspace status returned HTTP {current.status_code}"
                    )
                try:
                    payload = current.json()
                except ValueError as exc:
                    raise FreshRuntimeSmokeError(
                        "Runtime workspace status returned invalid JSON"
                    ) from exc
                if payload.get("runtime_workspace_id") != workspace_id:
                    raise FreshRuntimeSmokeError("Runtime workspace identity changed")
                if payload.get("runtime_status") == "ready":
                    return workspace_id
                if payload.get("runtime_status") == "failed":
                    raise FreshRuntimeSmokeError("fresh Runtime bootstrap failed")
                if asyncio.get_running_loop().time() >= deadline:
                    raise FreshRuntimeSmokeError("fresh Runtime bootstrap timed out")
                await asyncio.sleep(1)
    finally:
        app.dependency_overrides.pop(get_principal, None)


async def load_fresh_evidence(
    *,
    principal: Principal,
    runtime_workspace_id: int,
) -> FreshRuntimeEvidence:
    async with AsyncSessionLocal() as db:
        profile = await db.scalar(
            select(CloudHermesProfile).where(
                CloudHermesProfile.id == runtime_workspace_id,
                CloudHermesProfile.org_id == principal.org_id,
                CloudHermesProfile.user_id == principal.user_id,
                CloudHermesProfile.profile_type == "default_chat_agent",
                CloudHermesProfile.status == "active",
            )
        )
        binding = await db.scalar(
            select(HermesRuntimeBinding).where(
                HermesRuntimeBinding.runtime_workspace_id == runtime_workspace_id,
                HermesRuntimeBinding.org_id == principal.org_id,
                HermesRuntimeBinding.user_id == principal.user_id,
                HermesRuntimeBinding.status == "ready",
            )
        )
        if profile is None or binding is None:
            raise FreshRuntimeSmokeError("fresh Runtime identity is unavailable")
        intent = await db.scalar(
            select(RuntimeBootstrapIntent).where(
                RuntimeBootstrapIntent.runtime_binding_type == "personal_runtime",
                RuntimeBootstrapIntent.runtime_binding_id == int(binding.id),
                RuntimeBootstrapIntent.runtime_id == str(binding.runtime_id),
                RuntimeBootstrapIntent.status == "completed",
            )
        )
    target = RuntimeImageConfigStore(BACKEND_ROOT / ".env").read()
    return FreshRuntimeEvidence(
        principal=principal,
        runtime_workspace_id=runtime_workspace_id,
        runtime_id=str(binding.runtime_id),
        binding_id=int(binding.id),
        bootstrap_intent_completed=intent is not None,
        image_matches_env=(
            str(binding.image) == target.image
            and str(binding.image_digest or "") == target.config_id
        ),
    )


async def run_smoke(args: argparse.Namespace) -> dict[str, object]:
    if args.timeout_seconds <= 0:
        raise FreshRuntimeSmokeError("timeout must be positive")
    account = await create_acceptance_account()
    principal = Principal(user_id=account.user_id, org_id=account.org_id)
    operation_error: Exception | None = None
    result: dict[str, object] | None = None
    cleanup: dict[str, object] | None = None
    try:
        workspace_id = await bootstrap_workspace(
            principal=principal,
            timeout_seconds=args.timeout_seconds,
        )
        evidence = await load_fresh_evidence(
            principal=principal,
            runtime_workspace_id=workspace_id,
        )
        if not evidence.bootstrap_intent_completed or not evidence.image_matches_env:
            raise FreshRuntimeSmokeError("fresh Runtime contract verification failed")
        target = SmokeTarget(
            principal=principal,
            runtime_id=evidence.runtime_id,
            binding_id=evidence.binding_id,
        )
        created, bootstrap = await create_bootstrap(
            target=target,
            runtime_workspace_id=workspace_id,
        )
        try:
            conversation = await run_websocket_conversation(
                bootstrap=bootstrap,
                runtime_edge_base=args.runtime_edge_base or settings.PUBLIC_WEB_BASE,
                timeout_seconds=args.timeout_seconds,
            )
        except RuntimeEdgeSmokeError as exc:
            raise FreshRuntimeSmokeError(str(exc)) from exc
        runtime_session_id = created.get("runtime_session_id")
        if not isinstance(runtime_session_id, str) or not runtime_session_id:
            raise FreshRuntimeSmokeError("fresh Runtime Session response is invalid")
        result = {
            "success": True,
            "simulation": "fresh-user-api-principal-override-live-runtime-edge",
            "test_user_id": principal.user_id,
            "runtime_workspace_id": workspace_id,
            "runtime_id": evidence.runtime_id,
            "binding_id": evidence.binding_id,
            "bootstrap_intent_completed": evidence.bootstrap_intent_completed,
            "image_matches_env": evidence.image_matches_env,
            "runtime_session_id": runtime_session_id,
            **conversation,
        }
    except Exception as exc:
        operation_error = exc
        write_checkpoint(
            account,
            status="acceptance_failed",
            error_type=type(exc).__name__,
        )
    else:
        write_checkpoint(account, status="acceptance_succeeded")
    finally:
        try:
            cleanup = await cleanup_fresh_runtime(
                account,
                timeout_seconds=args.timeout_seconds,
            )
        except FreshRuntimeSmokeCleanupError as cleanup_error:
            if operation_error is not None:
                raise FreshRuntimeSmokeError(
                    "fresh Runtime smoke failed and automatic cleanup failed"
                ) from cleanup_error
            raise FreshRuntimeSmokeError("fresh Runtime automatic cleanup failed") from cleanup_error
    if operation_error is not None:
        if isinstance(operation_error, FreshRuntimeSmokeError):
            raise operation_error
        raise FreshRuntimeSmokeError(
            f"fresh Runtime smoke failed with {type(operation_error).__name__}"
        ) from operation_error
    if result is None or cleanup is None:
        raise FreshRuntimeSmokeError("fresh Runtime smoke result is unavailable")
    return {**result, **cleanup}


async def _run_and_dispose(args: argparse.Namespace) -> dict[str, object]:
    try:
        return await run_smoke(args)
    finally:
        await engine.dispose()


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    try:
        evidence = asyncio.run(_run_and_dispose(args))
    except (FreshRuntimeSmokeError, FreshRuntimeSmokeCleanupError, RuntimeEdgeSmokeError) as exc:
        print(json.dumps({"success": False, "error": str(exc)}, sort_keys=True))
        return 1
    except Exception as exc:  # Never render unknown exceptions containing tickets.
        print(
            json.dumps(
                {"success": False, "error": f"unexpected {type(exc).__name__}"},
                sort_keys=True,
            )
        )
        return 1
    print(json.dumps(evidence, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
