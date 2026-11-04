"""Retire application-owned knowledge through explicit, auditable phases.

The default ``dry-run`` phase is read-only. ``bind`` only restores the exact
historical personal library selected by ``sqlbot2``. ``cleanup`` permanently
deletes one exact application-owned library and its Provider dataset, and is
blocked unless a matching canary receipt and an exact confirmation token are
provided. The script never starts, stops, or reloads Runtime resources.
"""

from __future__ import annotations

import argparse
import asyncio
from datetime import datetime, timezone
import hashlib
import json
import os
from pathlib import Path
import sys
from typing import Any
from uuid import uuid4

from sqlalchemy import delete, func, select


BACKEND_ROOT = Path(__file__).resolve().parents[1]
if str(BACKEND_ROOT) not in sys.path:
    sys.path.insert(0, str(BACKEND_ROOT))

from app.database import AsyncSessionLocal, engine  # noqa: E402
from app.models import (  # noqa: E402
    ApplicationAgent,
    ApplicationAgentConfigRevision,
    ApplicationAgentPersonalKnowledgeBinding,
    KnowledgeBase,
    KnowledgeBaseFile,
    Permission,
    ProfileKnowledgeBinding,
)
from app.services.application_agent_personal_knowledge import (  # noqa: E402
    ApplicationAgentPersonalKnowledgeService,
)
from app.services.application_agent_asset_snapshot import (  # noqa: E402
    ApplicationAgentAssetSnapshotBuilder,
)
from app.services.personal_knowledge_provider import (  # noqa: E402
    FilebasePersonalKnowledgeClient,
    PersonalKnowledgeProviderError,
)
from app.services.personal_knowledge_acl import (  # noqa: E402
    PersonalKnowledgeAclService,
)
from app.services.runtime_workspace import Principal  # noqa: E402


RECEIPT_VERSION = "application-agent-knowledge-migration/v1"
CANARY_VERSION = "application-agent-knowledge-canary/v1"


class MigrationBlocked(RuntimeError):
    def __init__(self, code: str, detail: str) -> None:
        super().__init__(detail)
        self.code = code


def _checksum(payload: dict[str, Any]) -> str:
    unsigned = {key: value for key, value in payload.items() if key != "checksum"}
    encoded = json.dumps(
        unsigned,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")
    return f"sha256:{hashlib.sha256(encoded).hexdigest()}"


def _write_json(path: Path, payload: dict[str, Any]) -> None:
    target = path.resolve()
    target.parent.mkdir(parents=True, exist_ok=True)
    document = {**payload, "checksum": _checksum(payload)}
    temporary = target.with_name(f".{target.name}.{uuid4().hex}.tmp")
    try:
        temporary.write_text(
            json.dumps(document, ensure_ascii=False, sort_keys=True, indent=2)
            + "\n",
            encoding="utf-8",
        )
        os.chmod(temporary, 0o600)
        os.replace(temporary, target)
    finally:
        temporary.unlink(missing_ok=True)


def _load_json(path: Path, *, version: str) -> dict[str, Any]:
    try:
        document = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        raise MigrationBlocked(
            "MIGRATION_RECEIPT_INVALID",
            "receipt is missing or invalid JSON",
        ) from exc
    if not isinstance(document, dict) or document.get("receipt_version") != version:
        raise MigrationBlocked(
            "MIGRATION_RECEIPT_VERSION_INVALID",
            "receipt version is unsupported",
        )
    if document.get("checksum") != _checksum(document):
        raise MigrationBlocked(
            "MIGRATION_RECEIPT_CHECKSUM_INVALID",
            "receipt checksum does not match",
        )
    return document


def _dataset_digest(dataset_id: str | None) -> str | None:
    if not dataset_id:
        return None
    return "sha256:" + hashlib.sha256(dataset_id.encode("utf-8")).hexdigest()


async def _clean_legacy_agent_config(db: Any, agent: ApplicationAgent) -> None:
    config = dict(agent.config or {})
    config.pop("knowledge_bindings", None)
    config.pop("delegated_personal_knowledge_enabled", None)
    if config == (agent.config or {}):
        return
    agent.config = config
    agent.config_generation = int(agent.config_generation) + 1
    agent.updated_by_user_id = int(agent.created_by_user_id)
    if str(agent.status) == "published":
        agent.apply_status = "applying"
    snapshot = await ApplicationAgentAssetSnapshotBuilder(db).build(
        org_id=int(agent.org_id),
        application_agent_id=int(agent.id),
        config=config,
    )
    digest = hashlib.sha256(json.dumps(
        snapshot,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")).hexdigest()
    db.add(ApplicationAgentConfigRevision(
        org_id=int(agent.org_id),
        application_agent_id=int(agent.id),
        generation=int(agent.config_generation),
        config_snapshot=snapshot,
        config_hash=digest,
        created_by_user_id=int(agent.created_by_user_id),
        apply_status=str(agent.apply_status),
    ))


def _legacy_personal_ids(revisions: list[ApplicationAgentConfigRevision]) -> list[int]:
    ids: list[int] = []
    for revision in revisions:
        snapshot = revision.config_snapshot or {}
        bindings = snapshot.get("knowledge_bindings") or []
        if not isinstance(bindings, list):
            continue
        for binding in bindings:
            if not isinstance(binding, dict):
                continue
            if binding.get("access_mode") != "delegated_user":
                continue
            try:
                knowledge_base_id = int(binding["knowledge_base_id"])
            except (KeyError, TypeError, ValueError):
                continue
            if knowledge_base_id > 0 and knowledge_base_id not in ids:
                ids.append(knowledge_base_id)
    return ids


def _sqlbot2_source_blockers(
    source: KnowledgeBase,
    *,
    creator_effective_role: str | None,
    ready_file_count: int,
) -> list[str]:
    blockers: list[str] = []
    if str(source.kind) != "personal":
        blockers.append("SQLBOT2_SOURCE_NOT_PERSONAL")
    if creator_effective_role is None:
        blockers.append("SQLBOT2_SOURCE_PERMISSION_MISSING")
    if str(source.status) != "active" or not source.dataset_id:
        blockers.append("SQLBOT2_SOURCE_NOT_READY")
    if ready_file_count <= 0:
        blockers.append("SQLBOT2_SOURCE_HAS_NO_READY_FILE")
    return blockers


async def _plan(agent_id: int) -> dict[str, Any]:
    async with AsyncSessionLocal() as db:
        # Preflight must remain usable before the new personal-binding schema
        # is installed. Selecting the ORM entity here would also select newly
        # added columns and make the read-only dry-run depend on the migration
        # it is supposed to plan.
        agent_row = (await db.execute(
            select(
                ApplicationAgent.id,
                ApplicationAgent.org_id,
                ApplicationAgent.slug,
                ApplicationAgent.created_by_user_id,
                ApplicationAgent.config_generation,
            ).where(ApplicationAgent.id == agent_id)
        )).one_or_none()
        if agent_row is None:
            raise MigrationBlocked(
                "APPLICATION_AGENT_NOT_FOUND",
                "the exact application Agent does not exist",
            )
        (
            resolved_agent_id,
            org_id,
            application_slug,
            creator_user_id,
            config_generation,
        ) = agent_row
        application_bases = list((await db.execute(
            select(KnowledgeBase)
            .where(
                KnowledgeBase.application_agent_id == agent_id,
                KnowledgeBase.kind == "application",
            )
            .order_by(KnowledgeBase.id)
        )).scalars())
        revisions = list((await db.execute(
            select(ApplicationAgentConfigRevision)
            .where(ApplicationAgentConfigRevision.application_agent_id == agent_id)
            .order_by(ApplicationAgentConfigRevision.generation.desc())
        )).scalars())
        legacy_ids = _legacy_personal_ids(revisions)
        source = (
            await db.get(KnowledgeBase, legacy_ids[0])
            if len(legacy_ids) == 1
            else None
        )
        source_counts = (0, 0)
        source_effective_role: str | None = None
        if source is not None:
            source_effective_role = await PersonalKnowledgeAclService(
                db
            ).effective_role(
                principal=Principal(
                    org_id=int(org_id),
                    user_id=int(creator_user_id),
                ),
                knowledge_base=source,
            )
            source_counts = tuple((await db.execute(
                select(
                    func.count(KnowledgeBaseFile.id),
                    func.sum(
                        (KnowledgeBaseFile.file_status == 1)
                        & (KnowledgeBaseFile.is_deleted == 0)
                    ),
                ).where(
                    KnowledgeBaseFile.knowledge_base_id == int(source.id),
                    KnowledgeBaseFile.is_deleted == 0,
                )
            )).one())
        application_items: list[dict[str, Any]] = []
        for knowledge_base in application_bases:
            total, ready = (await db.execute(
                select(
                    func.count(KnowledgeBaseFile.id),
                    func.sum(
                        (KnowledgeBaseFile.file_status == 1)
                        & (KnowledgeBaseFile.is_deleted == 0)
                    ),
                ).where(
                    KnowledgeBaseFile.knowledge_base_id == int(knowledge_base.id),
                    KnowledgeBaseFile.is_deleted == 0,
                )
            )).one()
            application_items.append({
                "knowledge_base_id": int(knowledge_base.id),
                "owner_user_id": int(knowledge_base.owner_user_id),
                "status": str(knowledge_base.status),
                "file_count": int(total or 0),
                "ready_file_count": int(ready or 0),
                "provider_dataset_digest": _dataset_digest(
                    knowledge_base.dataset_id
                ),
            })
        blockers: list[str] = []
        sqlbot2 = str(application_slug) == "sqlbot2"
        if sqlbot2:
            if len(legacy_ids) != 1:
                blockers.append("SQLBOT2_LEGACY_PERSONAL_ID_NOT_UNIQUE")
            elif source is None:
                blockers.append("SQLBOT2_PERSONAL_SOURCE_MISSING")
            else:
                blockers.extend(_sqlbot2_source_blockers(
                    source,
                    creator_effective_role=source_effective_role,
                    ready_file_count=int(source_counts[1] or 0),
                ))
        return {
            "receipt_version": RECEIPT_VERSION,
            "receipt_id": uuid4().hex,
            "phase": "planned",
            "planned_at": datetime.now(timezone.utc).isoformat(),
            "application_agent_id": int(resolved_agent_id),
            "application_slug": str(application_slug),
            "creator_user_id": int(creator_user_id),
            "config_generation": int(config_generation),
            "sqlbot2": sqlbot2,
            "legacy_personal_knowledge_ids": legacy_ids,
            "source_personal_knowledge": (
                {
                    "knowledge_base_id": int(source.id),
                    "owner_user_id": int(source.owner_user_id),
                    "creator_effective_role": source_effective_role,
                    "status": str(source.status),
                    "file_count": int(source_counts[0] or 0),
                    "ready_file_count": int(source_counts[1] or 0),
                    "provider_dataset_digest": _dataset_digest(source.dataset_id),
                }
                if source is not None
                else None
            ),
            "application_knowledge": application_items,
            "provider_probe_required": sqlbot2,
            "blockers": blockers,
        }


def _validate_target_receipt(
    receipt: dict[str, Any],
    *,
    agent_id: int,
    phases: set[str],
) -> None:
    if int(receipt.get("application_agent_id") or 0) != agent_id:
        raise MigrationBlocked(
            "MIGRATION_RECEIPT_TARGET_MISMATCH",
            "receipt belongs to another application Agent",
        )
    if str(receipt.get("phase")) not in phases:
        raise MigrationBlocked(
            "MIGRATION_RECEIPT_PHASE_INVALID",
            "receipt phase does not permit this operation",
        )


async def _bind(agent_id: int, receipt_path: Path, confirmation: str) -> dict[str, Any]:
    if confirmation != str(agent_id):
        raise MigrationBlocked(
            "BIND_CONFIRMATION_INVALID",
            "bind requires --confirm-agent with the exact numeric Agent ID",
        )
    receipt = _load_json(receipt_path, version=RECEIPT_VERSION)
    _validate_target_receipt(receipt, agent_id=agent_id, phases={"planned", "bound"})
    if receipt.get("blockers"):
        raise MigrationBlocked("MIGRATION_PREFLIGHT_BLOCKED", "preflight has blockers")
    if not receipt.get("sqlbot2"):
        raise MigrationBlocked(
            "AUTOMATIC_BIND_NOT_ALLOWED",
            "only sqlbot2 may restore its exact historical personal binding",
        )
    source = receipt.get("source_personal_knowledge") or {}
    knowledge_base_id = int(source.get("knowledge_base_id") or 0)
    async with AsyncSessionLocal() as db:
        agent = await db.get(ApplicationAgent, agent_id)
        if agent is None or int(agent.created_by_user_id) != int(
            receipt["creator_user_id"]
        ):
            raise MigrationBlocked(
                "APPLICATION_AGENT_CHANGED",
                "application Agent creator changed after preflight",
            )
        await ApplicationAgentPersonalKnowledgeService(db).select(
            principal=Principal(
                org_id=int(agent.org_id),
                user_id=int(agent.created_by_user_id),
            ),
            agent_id=agent_id,
            knowledge_base_id=knowledge_base_id,
        )
        await db.refresh(agent)
        await _clean_legacy_agent_config(db, agent)
        await db.commit()
        binding = await db.scalar(select(
            ApplicationAgentPersonalKnowledgeBinding
        ).where(
            ApplicationAgentPersonalKnowledgeBinding.application_agent_id
            == agent_id,
            ApplicationAgentPersonalKnowledgeBinding.knowledge_base_id
            == knowledge_base_id,
            ApplicationAgentPersonalKnowledgeBinding.status == "enabled",
        ))
        if binding is None:
            raise MigrationBlocked(
                "PERSONAL_BINDING_NOT_ENABLED",
                "the restored binding did not converge to enabled",
            )
        await db.refresh(agent)
        updated = {
            **{key: value for key, value in receipt.items() if key != "checksum"},
            "phase": "bound",
            "bound_at": datetime.now(timezone.utc).isoformat(),
            "bound_personal_knowledge_id": knowledge_base_id,
            "bound_generation": int(agent.config_generation),
        }
    _write_json(receipt_path, updated)
    return updated


def _validate_canary(
    canary: dict[str, Any],
    *,
    receipt: dict[str, Any],
) -> None:
    if int(canary.get("application_agent_id") or 0) != int(
        receipt["application_agent_id"]
    ):
        raise MigrationBlocked(
            "CANARY_TARGET_MISMATCH",
            "canary receipt belongs to another application Agent",
        )
    mode = canary.get("personal_knowledge_mode")
    if mode == "selected":
        if not canary.get("studio_verified") or not canary.get(
            "retrieval_verified"
        ):
            raise MigrationBlocked(
                "CANARY_INCOMPLETE",
                "selected mode requires Studio and retrieval verification",
            )
        if receipt.get("sqlbot2"):
            expected = int(receipt.get("bound_personal_knowledge_id") or 0)
            if (
                int(canary.get("personal_knowledge_base_id") or 0) != expected
                or not canary.get("delegated_verified")
            ):
                raise MigrationBlocked(
                    "SQLBOT2_CANARY_INCOMPLETE",
                    "sqlbot2 requires exact source, delegated, and retrieval verification",
                )
    elif mode == "disabled":
        if receipt.get("sqlbot2") or not canary.get("disabled_verified"):
            raise MigrationBlocked(
                "CANARY_INCOMPLETE",
                "disabled mode requires an explicit disabled verification",
            )
    else:
        raise MigrationBlocked(
            "CANARY_MODE_INVALID",
            "canary personal_knowledge_mode must be selected or disabled",
        )


def _record_canary(
    *,
    receipt: dict[str, Any],
    canary_path: Path,
    personal_mode: str | None,
    personal_knowledge_base_id: int | None,
    studio_verified: bool,
    delegated_verified: bool,
    retrieval_verified: bool,
    disabled_verified: bool,
) -> dict[str, Any]:
    canary = {
        "receipt_version": CANARY_VERSION,
        "application_agent_id": int(receipt["application_agent_id"]),
        "personal_knowledge_mode": personal_mode,
        "personal_knowledge_base_id": personal_knowledge_base_id,
        "studio_verified": studio_verified,
        "delegated_verified": delegated_verified,
        "retrieval_verified": retrieval_verified,
        "disabled_verified": disabled_verified,
        "verified_at": datetime.now(timezone.utc).isoformat(),
    }
    _validate_canary(canary, receipt=receipt)
    _write_json(canary_path, canary)
    return canary


async def _cleanup(
    agent_id: int,
    knowledge_base_id: int,
    receipt_path: Path,
    canary_path: Path,
    confirmation: str,
) -> dict[str, Any]:
    expected_confirmation = f"{agent_id}:{knowledge_base_id}"
    if confirmation != expected_confirmation:
        raise MigrationBlocked(
            "DELETE_CONFIRMATION_INVALID",
            "cleanup requires --confirm-delete AGENT_ID:KNOWLEDGE_BASE_ID",
        )
    receipt = _load_json(receipt_path, version=RECEIPT_VERSION)
    _validate_target_receipt(
        receipt,
        agent_id=agent_id,
        phases={"planned", "bound", "provider_deleted", "cleaned"},
    )
    canary = _load_json(canary_path, version=CANARY_VERSION)
    _validate_canary(canary, receipt=receipt)
    allowed_ids = {
        int(item["knowledge_base_id"])
        for item in receipt.get("application_knowledge") or []
    }
    if knowledge_base_id not in allowed_ids:
        raise MigrationBlocked(
            "CLEANUP_TARGET_NOT_IN_PREFLIGHT",
            "knowledge library was not in the preflight target set",
        )
    if receipt.get("phase") == "cleaned" and int(
        receipt.get("cleaned_knowledge_base_id") or 0
    ) == knowledge_base_id:
        return receipt
    if receipt.get("phase") == "provider_deleted" and int(
        receipt.get("provider_deleted_knowledge_base_id") or 0
    ) != knowledge_base_id:
        raise MigrationBlocked(
            "PROVIDER_DELETE_CHECKPOINT_TARGET_MISMATCH",
            "provider-deleted checkpoint belongs to another knowledge library",
        )

    async with AsyncSessionLocal() as db:
        knowledge_base = await db.scalar(select(KnowledgeBase).where(
            KnowledgeBase.id == knowledge_base_id,
            KnowledgeBase.application_agent_id == agent_id,
            KnowledgeBase.kind == "application",
        ))
        if knowledge_base is None:
            raise MigrationBlocked(
                "APPLICATION_KNOWLEDGE_NOT_FOUND",
                "the exact application-owned knowledge library no longer exists",
            )
        if receipt.get("phase") != "provider_deleted":
            if knowledge_base.dataset_id:
                try:
                    await FilebasePersonalKnowledgeClient().delete_dataset(
                        str(knowledge_base.dataset_id)
                    )
                except PersonalKnowledgeProviderError as exc:
                    raise MigrationBlocked(
                        "PROVIDER_DELETE_FAILED",
                        "Provider dataset deletion failed; database rows were preserved",
                    ) from exc
            provider_deleted = {
                **{key: value for key, value in receipt.items() if key != "checksum"},
                "phase": "provider_deleted",
                "provider_deleted_at": datetime.now(timezone.utc).isoformat(),
                "provider_deleted_knowledge_base_id": knowledge_base_id,
            }
            _write_json(receipt_path, provider_deleted)
            receipt = provider_deleted
        await db.execute(delete(ProfileKnowledgeBinding).where(
            ProfileKnowledgeBinding.knowledge_base_id == knowledge_base_id
        ))
        await db.execute(delete(Permission).where(
            Permission.asset_type == "knowledge_base",
            Permission.asset_id == knowledge_base_id,
        ))
        await db.execute(delete(KnowledgeBaseFile).where(
            KnowledgeBaseFile.knowledge_base_id == knowledge_base_id
        ))
        await db.delete(knowledge_base)
        agent = await db.get(ApplicationAgent, agent_id)
        if agent is None:
            raise MigrationBlocked(
                "APPLICATION_AGENT_NOT_FOUND",
                "application Agent disappeared during cleanup",
            )
        await _clean_legacy_agent_config(db, agent)
        await db.commit()

    cleaned = {
        **{key: value for key, value in receipt.items() if key != "checksum"},
        "phase": "cleaned",
        "cleaned_at": datetime.now(timezone.utc).isoformat(),
        "cleaned_knowledge_base_id": knowledge_base_id,
        "restore_requires_backup": True,
    }
    _write_json(receipt_path, cleaned)
    return cleaned


def _print(payload: dict[str, Any], *, error: bool = False) -> None:
    print(
        json.dumps(payload, ensure_ascii=False, sort_keys=True),
        file=sys.stderr if error else sys.stdout,
    )


async def run(args: argparse.Namespace) -> int:
    try:
        if args.mode == "dry-run":
            result = await _plan(args.agent_id)
            if args.receipt:
                _write_json(args.receipt, result)
            _print({
                "mode": "dry-run",
                **result,
                "receipt": str(args.receipt.resolve()) if args.receipt else None,
            })
            return 2 if result["blockers"] else 0
        if args.receipt is None:
            raise MigrationBlocked(
                "MIGRATION_RECEIPT_REQUIRED",
                "bind and cleanup require --receipt from dry-run",
            )
        if args.mode == "bind":
            result = await _bind(
                args.agent_id,
                args.receipt,
                args.confirm_agent or "",
            )
        elif args.mode == "canary":
            if args.canary_receipt is None:
                raise MigrationBlocked(
                    "CANARY_RECEIPT_REQUIRED",
                    "canary requires --canary-receipt",
                )
            receipt = _load_json(args.receipt, version=RECEIPT_VERSION)
            _validate_target_receipt(
                receipt,
                agent_id=args.agent_id,
                phases={"planned", "bound"},
            )
            result = _record_canary(
                receipt=receipt,
                canary_path=args.canary_receipt,
                personal_mode=args.personal_mode,
                personal_knowledge_base_id=args.personal_knowledge_base_id,
                studio_verified=args.studio_verified,
                delegated_verified=args.delegated_verified,
                retrieval_verified=args.retrieval_verified,
                disabled_verified=args.disabled_verified,
            )
            _print({
                "mode": "canary",
                "application_agent_id": result["application_agent_id"],
                "canary_receipt": str(args.canary_receipt.resolve()),
            })
            return 0
        else:
            if args.canary_receipt is None or args.knowledge_base_id is None:
                raise MigrationBlocked(
                    "CLEANUP_EVIDENCE_REQUIRED",
                    "cleanup requires --knowledge-base-id and --canary-receipt",
                )
            result = await _cleanup(
                args.agent_id,
                args.knowledge_base_id,
                args.receipt,
                args.canary_receipt,
                args.confirm_delete or "",
            )
        _print({"mode": args.mode, "receipt_id": result["receipt_id"], "phase": result["phase"]})
        return 0
    except MigrationBlocked as exc:
        _print({"mode": "blocked", "code": exc.code, "detail": str(exc)}, error=True)
        return 2


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Safely retire one Application Agent's application-owned knowledge"
    )
    parser.add_argument(
        "--mode",
        choices=("dry-run", "bind", "canary", "cleanup"),
        default="dry-run",
        help="dry-run is read-only; bind restores sqlbot2 only; cleanup is irreversible",
    )
    parser.add_argument("--agent-id", type=int, required=True)
    parser.add_argument("--receipt", type=Path)
    parser.add_argument("--confirm-agent")
    parser.add_argument("--knowledge-base-id", type=int)
    parser.add_argument("--canary-receipt", type=Path)
    parser.add_argument("--confirm-delete")
    parser.add_argument("--personal-mode", choices=("selected", "disabled"))
    parser.add_argument("--personal-knowledge-base-id", type=int)
    parser.add_argument("--studio-verified", action="store_true")
    parser.add_argument("--delegated-verified", action="store_true")
    parser.add_argument("--retrieval-verified", action="store_true")
    parser.add_argument("--disabled-verified", action="store_true")
    args = parser.parse_args()

    async def execute() -> int:
        try:
            return await run(args)
        finally:
            await engine.dispose()

    raise SystemExit(asyncio.run(execute()))


if __name__ == "__main__":
    main()
