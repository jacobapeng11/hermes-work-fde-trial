from __future__ import annotations

import asyncio
from dataclasses import dataclass
from datetime import datetime, timezone
import hashlib
import json
from pathlib import Path
import re
import sys
from typing import Any, Callable

from app.config import settings


SNAPSHOT_SCHEMA_VERSION = "1.0"
SOURCE_PATTERNS = (
    "PRIMER.md",
    "SCHEMA.md",
    "enterprise.yml",
    "relationships.yml",
    "ontology_compiler.py",
    "contracts/metric_contracts.yml",
    "semantic_models/*.mdl.yml",
    "wren-projects/default/wren_project.yml",
    "wren-projects/default/instructions*.md",
    "departments/*/PRIMER.md",
    "departments/*/index.yml",
    "departments/*/glossary.md",
    "departments/*/faq.md",
    "departments/*/log.md",
    "departments/*/methodologies/*.md",
    "departments/*/policies/*.md",
    "departments/*/sops/*.md",
    "departments/*/roles/*.md",
    "departments/*/training/*.md",
    "wiki/semantic/*.mdl.yml",
    "wiki/instructions/instructions.md",
    "skills/*/SKILL.md",
    "workflows/*.md",
    "data_service/auth.py",
)
FORBIDDEN_SNAPSHOT_KEYS = frozenset(
    {
        "api_key",
        "api_key_encrypted",
        "password",
        "secret",
        "token",
        "runtime_token",
        "user_field",
        "dept_field",
        "parent_user_field",
        "parent_dept_field",
    }
)


class KnowledgeGovernanceError(RuntimeError):
    def __init__(self, code: str, message: str, *, status_code: int = 503) -> None:
        super().__init__(code)
        self.code = code
        self.message = message
        self.status_code = status_code

    def as_error(self) -> dict[str, Any]:
        return {
            "code": self.code,
            "message": self.message,
            "retryable": False,
        }


class _OutputTooLarge(RuntimeError):
    pass


@dataclass(frozen=True)
class GovernanceSnapshotResult:
    snapshot: dict[str, Any]
    cache: dict[str, Any]
    latest_attempt: dict[str, Any]


def _utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _safe_failure(code: str, message: str, attempted_at: str) -> dict[str, Any]:
    return {
        "status": "failed",
        "attempted_at": attempted_at,
        "error": {"code": code, "message": message},
    }


class KnowledgeGovernanceProvider:
    def __init__(
        self,
        source_root: str | Path | None,
        *,
        timeout_seconds: float = 30,
        max_snapshot_bytes: int = 8 * 1024 * 1024,
        max_nodes: int = 10_000,
        max_edges: int = 20_000,
    ) -> None:
        self.source_root = (
            Path(source_root).expanduser().resolve()
            if str(source_root or "").strip()
            else None
        )
        self.timeout_seconds = timeout_seconds
        self.max_snapshot_bytes = max_snapshot_bytes
        self.max_nodes = max_nodes
        self.max_edges = max_edges
        self._lock = asyncio.Lock()
        self._snapshot: dict[str, Any] | None = None
        self._source_fingerprint: str | None = None
        self._last_attempt_fingerprint: str | None = None
        self._last_success_at: str | None = None
        self._last_attempt_at: str | None = None
        self._attempt_generation = 0
        self._latest_attempt: dict[str, Any] = {
            "status": "never",
            "attempted_at": None,
        }

    def _require_source(self) -> Path:
        if self.source_root is None:
            raise KnowledgeGovernanceError(
                "knowledge_governance_source_not_configured",
                "知识治理源目录尚未配置。",
            )
        if not self.source_root.is_dir():
            raise KnowledgeGovernanceError(
                "knowledge_governance_source_not_configured",
                "知识治理源目录不存在或不可读。",
            )
        compiler = self.source_root / "ontology_compiler.py"
        if (
            not compiler.is_file()
            or compiler.is_symlink()
            or compiler.resolve().parent != self.source_root
        ):
            raise KnowledgeGovernanceError(
                "knowledge_governance_source_not_configured",
                "知识治理编译器不存在或不是可信源目录中的普通文件。",
            )
        return self.source_root

    def _fingerprint(self) -> str:
        root = self._require_source()
        paths: set[Path] = set()
        for pattern in SOURCE_PATTERNS:
            paths.update(path for path in root.glob(pattern) if path.is_file() and not path.is_symlink())
        digest = hashlib.sha256()
        for path in sorted(paths):
            stat = path.stat()
            relative = path.relative_to(root).as_posix()
            digest.update(relative.encode("utf-8"))
            digest.update(b"\0")
            digest.update(str(stat.st_size).encode("ascii"))
            digest.update(b"\0")
            digest.update(str(stat.st_mtime_ns).encode("ascii"))
            digest.update(b"\0")
        return digest.hexdigest()

    async def _read_limited(
        self,
        stream: asyncio.StreamReader,
        limit: int,
        on_limit: Callable[[], None],
    ) -> bytes:
        chunks: list[bytes] = []
        total = 0
        too_large = False
        while True:
            chunk = await stream.read(64 * 1024)
            if not chunk:
                if too_large:
                    raise _OutputTooLarge
                return b"".join(chunks)
            total += len(chunk)
            if too_large:
                continue
            if total > limit:
                too_large = True
                chunks.clear()
                on_limit()
                continue
            chunks.append(chunk)

    async def _compile_snapshot(self) -> dict[str, Any]:
        root = self._require_source()
        compiler = root / "ontology_compiler.py"
        environment = {
            "PATH": "/usr/local/bin:/usr/bin:/bin",
            "LANG": "C.UTF-8",
            "LC_ALL": "C.UTF-8",
            "PYTHONIOENCODING": "utf-8",
            "PYTHONDONTWRITEBYTECODE": "1",
        }
        process = await asyncio.create_subprocess_exec(
            sys.executable,
            str(compiler),
            "snapshot",
            "--stdout",
            cwd=str(root),
            env=environment,
            stdin=asyncio.subprocess.DEVNULL,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        assert process.stdout is not None
        assert process.stderr is not None

        def kill_process() -> None:
            if process.returncode is None:
                try:
                    process.kill()
                except ProcessLookupError:
                    pass

        stdout_task = asyncio.create_task(
            self._read_limited(
                process.stdout,
                self.max_snapshot_bytes,
                kill_process,
            )
        )
        stderr_task = asyncio.create_task(
            self._read_limited(
                process.stderr,
                64 * 1024,
                kill_process,
            )
        )
        wait_task = asyncio.create_task(process.wait())
        combined = asyncio.gather(stdout_task, stderr_task, wait_task)
        try:
            stdout, _stderr, return_code = await asyncio.wait_for(
                asyncio.shield(combined),
                timeout=self.timeout_seconds,
            )
        except asyncio.TimeoutError as exc:
            kill_process()
            await asyncio.gather(
                stdout_task,
                stderr_task,
                wait_task,
                return_exceptions=True,
            )
            raise KnowledgeGovernanceError(
                "knowledge_governance_compile_timeout",
                "知识治理扫描超时，已保留上一次成功结果。",
            ) from exc
        except _OutputTooLarge as exc:
            kill_process()
            await asyncio.gather(
                stdout_task,
                stderr_task,
                wait_task,
                return_exceptions=True,
            )
            raise KnowledgeGovernanceError(
                "knowledge_governance_snapshot_too_large",
                "知识治理 Snapshot 超过安全大小上限。",
            ) from exc
        except asyncio.CancelledError:
            kill_process()
            await asyncio.gather(
                stdout_task,
                stderr_task,
                wait_task,
                return_exceptions=True,
            )
            raise
        if return_code != 0:
            raise KnowledgeGovernanceError(
                "knowledge_governance_compile_failed",
                "知识治理编译失败，已保留上一次成功结果。",
            )
        try:
            snapshot = json.loads(stdout.decode("utf-8"))
        except (UnicodeError, ValueError) as exc:
            raise KnowledgeGovernanceError(
                "knowledge_governance_snapshot_invalid",
                "知识治理编译器返回了无效 Snapshot。",
            ) from exc
        self._validate_snapshot(snapshot, raw_size=len(stdout))
        return snapshot

    def _validate_snapshot(self, snapshot: Any, *, raw_size: int) -> None:
        if raw_size > self.max_snapshot_bytes or not isinstance(snapshot, dict):
            raise KnowledgeGovernanceError(
                "knowledge_governance_snapshot_invalid",
                "知识治理 Snapshot 结构无效。",
            )
        required = {
            "schema_version",
            "generated_at",
            "source_hash",
            "summary",
            "validation",
            "catalogue",
            "graph",
            "dependency_index",
            "metric_contracts",
        }
        if not required.issubset(snapshot):
            raise KnowledgeGovernanceError(
                "knowledge_governance_snapshot_invalid",
                "知识治理 Snapshot 缺少必要字段。",
            )
        if snapshot["schema_version"] != SNAPSHOT_SCHEMA_VERSION:
            raise KnowledgeGovernanceError(
                "knowledge_governance_snapshot_invalid",
                "知识治理 Snapshot 版本不受支持。",
            )
        source_hash = snapshot.get("source_hash")
        if not isinstance(source_hash, str) or re.fullmatch(r"[0-9a-f]{64}", source_hash) is None:
            raise KnowledgeGovernanceError(
                "knowledge_governance_snapshot_invalid",
                "知识治理 Snapshot source_hash 无效。",
            )
        graph = snapshot.get("graph")
        nodes = graph.get("nodes") if isinstance(graph, dict) else None
        edges = graph.get("edges") if isinstance(graph, dict) else None
        if not isinstance(nodes, list) or not isinstance(edges, list):
            raise KnowledgeGovernanceError(
                "knowledge_governance_snapshot_invalid",
                "知识治理 Snapshot 图谱结构无效。",
            )
        if len(nodes) > self.max_nodes or len(edges) > self.max_edges:
            raise KnowledgeGovernanceError(
                "knowledge_governance_snapshot_too_large",
                "知识治理 Snapshot 节点或关系数量超过安全上限。",
            )
        node_ids: set[str] = set()
        for node in nodes:
            if (
                not isinstance(node, dict)
                or not isinstance(node.get("id"), str)
                or not isinstance(node.get("type"), str)
                or not isinstance(node.get("name"), str)
                or node["id"] in node_ids
            ):
                raise KnowledgeGovernanceError(
                    "knowledge_governance_snapshot_invalid",
                    "知识治理 Snapshot 节点结构无效。",
                )
            node_ids.add(node["id"])
        edge_ids: set[str] = set()
        for edge in edges:
            if (
                not isinstance(edge, dict)
                or not isinstance(edge.get("id"), str)
                or not isinstance(edge.get("type"), str)
                or edge.get("from") not in node_ids
                or edge.get("to") not in node_ids
                or edge["id"] in edge_ids
            ):
                raise KnowledgeGovernanceError(
                    "knowledge_governance_snapshot_invalid",
                    "知识治理 Snapshot 关系结构无效。",
                )
            edge_ids.add(edge["id"])
        self._reject_forbidden_keys(snapshot)

    def _reject_forbidden_keys(self, value: Any) -> None:
        if isinstance(value, dict):
            if FORBIDDEN_SNAPSHOT_KEYS.intersection(value):
                raise KnowledgeGovernanceError(
                    "knowledge_governance_snapshot_invalid",
                    "知识治理 Snapshot 包含禁止暴露的字段。",
                )
            for child in value.values():
                self._reject_forbidden_keys(child)
        elif isinstance(value, list):
            for child in value:
                self._reject_forbidden_keys(child)

    def _result(self, *, cache_state: str) -> GovernanceSnapshotResult:
        assert self._snapshot is not None
        return GovernanceSnapshotResult(
            snapshot=self._snapshot,
            cache={
                "state": cache_state,
                "source_fingerprint": self._source_fingerprint,
                "last_success_at": self._last_success_at,
                "last_attempt_at": self._last_attempt_at,
            },
            latest_attempt=self._latest_attempt,
        )

    async def get_snapshot(self, *, force: bool = False) -> GovernanceSnapshotResult:
        observed_generation = self._attempt_generation
        async with self._lock:
            # Coalesce force-refresh requests that arrived while another
            # compilation was in flight. A later, independently initiated
            # refresh still observes the current generation and recompiles.
            if force and observed_generation != self._attempt_generation:
                if self._snapshot is not None:
                    cache_state = (
                        "stale"
                        if self._latest_attempt.get("status") == "failed"
                        else "hit"
                    )
                    return self._result(cache_state=cache_state)
                raise KnowledgeGovernanceError(
                    "knowledge_governance_unavailable",
                    "知识治理暂时不可用，且没有可回退的成功 Snapshot。",
                )
            # The allow-list is intentionally small and this stat-only pass is
            # fast. Keeping it synchronous also avoids depending on the
            # process-wide default executor in restricted runtime sandboxes.
            fingerprint = self._fingerprint()
            if not force and self._snapshot is not None:
                if fingerprint == self._source_fingerprint:
                    return self._result(cache_state="hit")
                if (
                    fingerprint == self._last_attempt_fingerprint
                    and self._latest_attempt.get("status") == "failed"
                ):
                    return self._result(cache_state="stale")
            attempted_at = _utc_now()
            self._last_attempt_at = attempted_at
            self._last_attempt_fingerprint = fingerprint
            try:
                snapshot = await self._compile_snapshot()
            except KnowledgeGovernanceError as exc:
                self._latest_attempt = _safe_failure(
                    exc.code,
                    exc.message,
                    attempted_at,
                )
                self._attempt_generation += 1
                if self._snapshot is not None:
                    return self._result(cache_state="stale")
                raise KnowledgeGovernanceError(
                    "knowledge_governance_unavailable",
                    "知识治理暂时不可用，且没有可回退的成功 Snapshot。",
                ) from exc
            self._snapshot = snapshot
            self._source_fingerprint = fingerprint
            self._last_success_at = _utc_now()
            self._latest_attempt = {
                "status": "succeeded",
                "attempted_at": attempted_at,
                "source_hash": snapshot["source_hash"],
            }
            self._attempt_generation += 1
            return self._result(cache_state="refreshed")


_provider: KnowledgeGovernanceProvider | None = None
_provider_settings: tuple[Any, ...] | None = None


def get_knowledge_governance_provider() -> KnowledgeGovernanceProvider:
    global _provider, _provider_settings
    current = (
        settings.KNOWLEDGE_GOVERNANCE_SOURCE_ROOT,
        settings.KNOWLEDGE_GOVERNANCE_COMPILE_TIMEOUT_SECONDS,
        settings.KNOWLEDGE_GOVERNANCE_MAX_SNAPSHOT_BYTES,
        settings.KNOWLEDGE_GOVERNANCE_MAX_NODES,
        settings.KNOWLEDGE_GOVERNANCE_MAX_EDGES,
    )
    if _provider is None or _provider_settings != current:
        _provider = KnowledgeGovernanceProvider(
            settings.KNOWLEDGE_GOVERNANCE_SOURCE_ROOT,
            timeout_seconds=settings.KNOWLEDGE_GOVERNANCE_COMPILE_TIMEOUT_SECONDS,
            max_snapshot_bytes=settings.KNOWLEDGE_GOVERNANCE_MAX_SNAPSHOT_BYTES,
            max_nodes=settings.KNOWLEDGE_GOVERNANCE_MAX_NODES,
            max_edges=settings.KNOWLEDGE_GOVERNANCE_MAX_EDGES,
        )
        _provider_settings = current
    return _provider


def governance_summary(result: GovernanceSnapshotResult) -> dict[str, Any]:
    snapshot = result.snapshot
    return {
        "schema_version": snapshot["schema_version"],
        "generated_at": snapshot["generated_at"],
        "source_hash": snapshot["source_hash"],
        "summary": snapshot["summary"],
        "validation_summary": snapshot["validation"]["summary"],
        "cache": result.cache,
        "latest_attempt": result.latest_attempt,
    }


def list_governance_assets(
    result: GovernanceSnapshotResult,
    *,
    query: str | None,
    asset_type: str | None,
    page: int,
    page_size: int,
) -> dict[str, Any]:
    nodes = result.snapshot["graph"]["nodes"]
    if asset_type:
        nodes = [node for node in nodes if node["type"] == asset_type]
    if query:
        needle = query.casefold()
        nodes = [
            node
            for node in nodes
            if needle
            in (
                f"{node['id']} {node['name']} "
                f"{' '.join(str(source) for source in node.get('sources', []))}"
            ).casefold()
        ]
    total = len(nodes)
    start = (page - 1) * page_size
    return {
        "items": nodes[start : start + page_size],
        "total": total,
        "page": page,
        "page_size": page_size,
        "total_pages": (total + page_size - 1) // page_size,
        "cache": result.cache,
        "latest_attempt": result.latest_attempt,
    }


def governance_asset(
    result: GovernanceSnapshotResult,
    asset_id: str,
) -> dict[str, Any] | None:
    graph = result.snapshot["graph"]
    node = next((item for item in graph["nodes"] if item["id"] == asset_id), None)
    if node is None:
        return None
    edges = [
        edge
        for edge in graph["edges"]
        if edge["from"] == asset_id or edge["to"] == asset_id
    ]
    dependency = result.snapshot["dependency_index"]["assets"].get(asset_id)
    return {
        "asset": node,
        "relationships": edges,
        "dependency": dependency,
        "cache": result.cache,
        "latest_attempt": result.latest_attempt,
    }


def governance_impact(
    result: GovernanceSnapshotResult,
    asset_id: str,
) -> dict[str, Any] | None:
    dependency = result.snapshot["dependency_index"]["assets"].get(asset_id)
    if dependency is None:
        return None
    affected_ids = set(dependency.get("downstream", [])) | set(
        dependency.get("downstream_two_hops", [])
    )
    nodes = {
        node["id"]: node for node in result.snapshot["graph"]["nodes"]
    }
    affected = [nodes[node_id] for node_id in sorted(affected_ids) if node_id in nodes]
    affected_types = {node["type"] for node in affected}
    actions = ["运行 python workflows/ddl_init/check_consistency.py --strict"]
    if affected_types & {
        "semantic_model",
        "field",
        "metric_contract",
        "instruction_section",
    }:
        actions.append("运行 python data_service/build_index.py")
    actions.append("运行 python ontology_compiler.py validate --strict")
    if "skill" in affected_types:
        actions.append("冒烟验证受影响的报告 Skill")
    return {
        "asset_id": asset_id,
        "asset": dependency,
        "affected": affected,
        "recommended_actions": actions,
        "cache": result.cache,
        "latest_attempt": result.latest_attempt,
    }
