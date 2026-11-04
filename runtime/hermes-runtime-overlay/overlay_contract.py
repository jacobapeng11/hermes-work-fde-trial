#!/usr/bin/env python3
"""Closed loader for the Hermes 0.20.1 Runtime overlay contract."""

from __future__ import annotations

import base64
import hashlib
import json
from pathlib import Path, PurePosixPath
import re
from typing import Any

import yaml


SCHEMA_VERSION = 2
UPSTREAM_VERSION = "v0.20.1"
RELEASE_TAG = "v2026.8.13"
UPSTREAM_COMMIT = "f80f453ae0679347e38abc917c7f94f717bf96c5"
SOURCE_ARCHIVE_SHA256 = "570900ec79d2c292a9bf04a015032b417412fb962f329c2c7a13381beda9d154"
STATE_SCHEMA = 25
CONFIG_SCHEMA = 34
COMPONENTS = frozenset({"voice", "a2a", "webhook", "image"})

_REQUIRED_CHECKS = [
    "source_integrity",
    "environment_integrity",
    "clean_start",
    "personal_runtime_smoke",
    "application_runtime_smoke",
    "storage_clone_upgrade",
    "restart_after_upgrade",
    "forward_failure_freeze",
    "official_skill_inventory",
    "upstream_component_isolation",
    "boundary_plugin_integrity",
    "lazy_install_negative",
    "python_distribution_fingerprint",
]
_SYNC_EXTRAS = [
    "all",
    "messaging",
    "otlp",
    "anthropic",
    "bedrock",
    "azure-identity",
    "hindsight",
    "matrix",
    "edge-tts",
    "fal",
]
_SKILL_CHECKS = [
    "duplicate_frontmatter_name",
    "missing_skill_file",
    "symlink_escape",
    "linux_effective_visibility",
    "kanban_effective_visibility",
    "platform_and_user_overlay",
]
_PLUGIN_SOURCE_FILES = {
    "pyproject.toml",
    "src/platform_main_conversation_boundary/__init__.py",
    "src/platform_main_conversation_boundary/marker.py",
    "src/platform_main_conversation_boundary/plugin.py",
}
_FTS5_CJK_SOURCE_FILES = {
    "fts5_cjk.c": (
        "sha256:814e7dd9ab1b4c4f69673715ed147a2f8203e3443661c0152d36ecfabcb4768f"
    ),
    "build.sh": (
        "sha256:fed26cb860b755e9b6055658e3f1f89d3ca6d0634321ea76c2d391aed2cb9b4c"
    ),
    "vendor/sqlite3.h": (
        "sha256:abd1514e0351f79393d1be882830afdb40a8099e8257f311f0bfdf8486f11bea"
    ),
    "vendor/sqlite3ext.h": (
        "sha256:9a91de0d5e5ccc04ec59041275c67972d6f8894f7543a10033e387b69987beb5"
    ),
}
_STATE_SCHEMA_PROBE = {
    "contract": "hermes-state-schema-probe-f80f453-v1",
    "row_digest_contract": "hermes-sqlite-row-stream-v1",
    "supported_source_schemas": [23, 25],
    "target_schema": 25,
    "core_tables_by_schema": {
        23: ["schema_version", "sessions", "messages", "state_meta"],
        25: [
            "schema_version",
            "system_prompts",
            "sessions",
            "messages",
            "state_meta",
        ],
    },
    "stable_session_columns": [
        "id", "source", "user_id", "session_key", "chat_id", "chat_type",
        "thread_id", "display_name", "origin_json", "expiry_finalized", "model",
        "model_config", "parent_session_id", "started_at", "ended_at",
        "end_reason", "message_count", "tool_call_count", "input_tokens",
        "output_tokens", "cache_read_tokens", "cache_write_tokens",
        "reasoning_tokens", "cwd", "git_branch", "git_repo_root",
        "billing_provider", "billing_base_url", "billing_mode",
        "estimated_cost_usd", "actual_cost_usd", "cost_status", "cost_source",
        "pricing_version", "title", "api_call_count", "handoff_state",
        "handoff_platform", "handoff_error", "compression_failure_cooldown_until",
        "compression_failure_error", "compression_fallback_streak",
        "compression_ineffective_count", "profile_name", "rewind_count",
        "archived", "pinned",
    ],
    "stable_message_columns": [
        "id", "session_id", "role", "content", "tool_call_id", "tool_calls",
        "tool_name", "effect_disposition", "timestamp", "token_count",
        "finish_reason", "reasoning", "reasoning_content", "reasoning_details",
        "codex_reasoning_items", "codex_message_items", "platform_message_id",
        "observed", "active", "compacted", "api_content", "display_kind",
        "display_metadata",
    ],
    "fts_objects": {
        "base_tables": ["messages_fts", "messages_fts_trigram"],
        "external_views": ["messages_fts_trigram_src"],
        "sync_triggers": [
            "messages_fts_insert", "messages_fts_delete", "messages_fts_update",
            "messages_fts_trigram_insert", "messages_fts_trigram_delete",
            "messages_fts_trigram_update",
        ],
        "cjk_table": "messages_fts_cjk",
        "cjk_view": "messages_fts_cjk_src",
        "cjk_triggers": [
            "messages_fts_cjk_insert", "messages_fts_cjk_delete",
            "messages_fts_cjk_update",
        ],
    },
}
_COMPONENT_SURFACES: dict[str, list[dict[str, Any]]] = {
    "voice": [
        {
            "module": "agent.tts_provider",
            "loader": "dotted",
            "source": "agent/tts_provider.py",
            "symbols": ["TTSProvider", "resolve_output_format"],
        },
        {
            "module": "tools.tts_text_normalize",
            "loader": "dotted",
            "source": "tools/tts_text_normalize.py",
            "symbols": ["prepare_spoken_text"],
        },
        {
            "module": "edge_tts",
            "loader": "distribution",
            "distribution": "edge-tts",
            "symbols": ["Communicate"],
        },
    ],
    "a2a": [
        {
            "module": "plugins.platforms.a2a.protocol",
            "loader": "dotted",
            "source": "plugins/platforms/a2a/protocol.py",
            "symbols": [
                "ROLE_USER",
                "text_message",
                "extract_text",
                "jsonrpc_result",
                "build_agent_card",
            ],
        },
        {
            "module": "plugins.platforms.a2a.security",
            "loader": "dotted",
            "source": "plugins/platforms/a2a/security.py",
            "symbols": ["filter_inbound", "redact_outbound", "sign_push_payload"],
        },
    ],
    "webhook": [
        {
            "module": "agent.outbound_webhooks",
            "loader": "dotted",
            "source": "agent/outbound_webhooks.py",
            "symbols": ["WebhookTarget", "_build_delivery"],
        },
        {
            "module": "gateway.platforms.webhook_filters",
            "loader": "file",
            "source": "gateway/platforms/webhook_filters.py",
            "symbols": ["WebhookRouteProcessor"],
        },
    ],
    "image": [
        {
            "module": "agent.image_gen_provider",
            "loader": "dotted",
            "source": "agent/image_gen_provider.py",
            "symbols": [
                "ImageGenProvider",
                "resolve_aspect_ratio",
                "normalize_reference_images",
                "success_response",
                "error_response",
            ],
        },
        {
            "module": "agent.image_gen_registry",
            "loader": "dotted",
            "source": "agent/image_gen_registry.py",
            "symbols": ["list_providers"],
        },
        {
            "module": "fal_client",
            "loader": "distribution",
            "distribution": "fal-client",
            "symbols": [],
        },
        {
            "module": "PIL",
            "loader": "distribution",
            "distribution": "Pillow",
            "symbols": [],
        },
    ],
}
_COMPONENT_DEPENDENCIES = {
    "voice": [
        "aiohappyeyeballs",
        "aiohttp",
        "aiosignal",
        "attrs",
        "certifi",
        "edge-tts",
        "frozenlist",
        "idna",
        "multidict",
        "propcache",
        "tabulate",
        "typing-extensions",
        "yarl",
    ],
    "a2a": [],
    "webhook": [],
    "image": [
        "anyio",
        "certifi",
        "fal-client",
        "h11",
        "httpcore",
        "httpx",
        "httpx-sse",
        "idna",
        "msgpack",
        "Pillow",
        "sniffio",
        "typing-extensions",
        "websockets",
    ],
}
_VECTOR_DIGESTS = {
    "voice-format-v1": "sha256:33ee49a43df53857eac98863eff709d0ba679cd62e422686f743263064595358",
    "voice-normalize-v1": "sha256:6ee5dd58e73cd94caff01944f5678ed59bfb908b7bbc0d0549c7f18f1b2bf365",
    "a2a-message-v1": "sha256:f66fd879264bf203161adb023f0131a0f2e0797f134a9225c4dc5875983d00e3",
    "a2a-jsonrpc-v1": "sha256:2a55f5d5933070c7cf00708d7cbb22257a0118da00e43cb8da3f0172c48bc67f",
    "a2a-card-v1": "sha256:a78847943e95f78452339b6df5b75925360863c6f1f92cda949c8e3c8713b45c",
    "a2a-security-v1": "sha256:5ee767d7fa1e5fd52c9b107437ebb2d9869eb16167723b5c67e6f9a71fc18010",
    "webhook-delivery-v1": "sha256:0c2f28356820186d5bfc8052e46908ab7d1bb9fd4d3f91943c5b491a75c8f3df",
    "webhook-filter-v1": "sha256:d510bc979a3ffe97ee664d2261ee5bb7fc7fd537667e3d6ebe0646907b15d3f0",
    "image-normalize-v1": "sha256:dcc78e2ffee9eb8fca65da4404ef0e443f1e96025394a673faf3839c3bf6273f",
    "image-success-v1": "sha256:c4c99a15bc8115fde46f221e610984139d29ce51c6ad0b42c0f9730b2c307147",
}

_TOP_LEVEL_FIELDS = {
    "schema_version",
    "upstream_version",
    "release_tag",
    "upstream_commit",
    "source_archive_sha256",
    "state_schema",
    "config_schema",
    "supported_pod_configs",
    "required_checks",
    "runtime_overlay",
    "upstream_components",
}
_SHA256_RE = re.compile(r"sha256:[0-9a-f]{64}")


class OverlayContractError(RuntimeError):
    """A fixed Candidate contract is missing, unsafe, or ambiguous."""


def canonical_json(value: object) -> bytes:
    return json.dumps(
        value,
        ensure_ascii=False,
        allow_nan=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")


def sha256_jcs(value: object) -> str:
    return "sha256:" + hashlib.sha256(canonical_json(value)).hexdigest()


def canonical_relative_path(value: object) -> PurePosixPath:
    if not isinstance(value, str):
        raise OverlayContractError("manifest source path must be a string")
    path = PurePosixPath(value)
    if path.is_absolute() or not path.parts or ".." in path.parts:
        raise OverlayContractError(f"unsafe manifest source path: {value!r}")
    if path.as_posix() != value:
        raise OverlayContractError(f"non-canonical manifest source path: {value!r}")
    return path


def _require_exact_fields(payload: dict[str, Any], expected: set[str], label: str) -> None:
    if set(payload) != expected:
        missing = sorted(expected.difference(payload))
        unknown = sorted(set(payload).difference(expected))
        raise OverlayContractError(
            f"{label} fields mismatch; missing={missing}; unknown={unknown}"
        )


def _validate_vectors(components: dict[str, Any]) -> None:
    expected_vectors = {
        "voice": {"voice-format-v1", "voice-normalize-v1"},
        "a2a": {
            "a2a-message-v1",
            "a2a-jsonrpc-v1",
            "a2a-card-v1",
            "a2a-security-v1",
        },
        "webhook": {"webhook-delivery-v1", "webhook-filter-v1"},
        "image": {"image-normalize-v1", "image-success-v1"},
    }
    for component, vector_ids in expected_vectors.items():
        value = components.get(component)
        if not isinstance(value, dict):
            raise OverlayContractError(f"missing component contract: {component}")
        if set(value) != {"modules", "dependencies", "vectors"}:
            raise OverlayContractError(f"component fields mismatch: {component}")
        vectors = value.get("vectors")
        if not isinstance(vectors, dict) or set(vectors) != vector_ids:
            raise OverlayContractError(f"vector set mismatch: {component}")
        for vector_id, vector in vectors.items():
            if not isinstance(vector, dict) or set(vector) != {
                "expected_projection",
                "expected_digest",
            }:
                raise OverlayContractError(f"invalid vector contract: {vector_id}")
            expected_digest = sha256_jcs(
                {
                    "vector_id": vector_id,
                    "actual_projection": vector["expected_projection"],
                }
            )
            if (
                vector.get("expected_digest") != expected_digest
                or vector.get("expected_digest") != _VECTOR_DIGESTS[vector_id]
            ):
                raise OverlayContractError(f"vector digest mismatch: {vector_id}")


def _validate_runtime_overlay(value: object) -> None:
    if not isinstance(value, dict):
        raise OverlayContractError("runtime_overlay contract is missing")
    _require_exact_fields(
        value,
        {
            "python_requires",
            "hermes_root",
            "dependency_lock",
            "official_skills",
            "fts5_cjk_extension",
            "state_schema_probe",
            "platform_main_conversation_boundary",
        },
        "runtime_overlay",
    )
    if value["python_requires"] != ">=3.11,<3.14" or value["hermes_root"] != "/opt/hermes":
        raise OverlayContractError("target Python or Hermes root mismatch")
    dependency_lock = value["dependency_lock"]
    if not isinstance(dependency_lock, dict):
        raise OverlayContractError("Runtime dependency lock is missing")
    _require_exact_fields(
        dependency_lock,
        {"kind", "path", "sync_extras", "required_distributions"},
        "dependency_lock",
    )
    if (
        dependency_lock["kind"] != "upstream-uv-lock"
        or dependency_lock["path"] != "/opt/hermes/uv.lock"
        or dependency_lock["sync_extras"] != _SYNC_EXTRAS
        or dependency_lock["required_distributions"]
        != {
            "Pillow": "12.3.0",
            "edge-tts": "7.2.7",
            "fal-client": "0.13.1",
            "simple-term-menu": "1.6.6",
            "setuptools": "83.0.0",
            "watchfiles": "1.1.1",
        }
    ):
        raise OverlayContractError("locked Runtime distribution contract mismatch")

    skills = value["official_skills"]
    if not isinstance(skills, dict):
        raise OverlayContractError("official Skill policy contract is missing")
    _require_exact_fields(
        skills,
        {
            "source_root",
            "official_source_count",
            "policy_version",
            "default_policy",
            "required_checks",
        },
        "official_skills",
    )
    if (
        skills["source_root"] != "/opt/hermes/skills"
        or type(skills["official_source_count"]) is not int
        or skills["official_source_count"] != 81
        or type(skills["policy_version"]) is not int
        or skills["policy_version"] != 3
        or skills["default_policy"] != "balanced_allowlist"
        or skills["required_checks"] != _SKILL_CHECKS
    ):
        raise OverlayContractError("official Skill policy contract mismatch")
    _validate_fts5_cjk_extension(value["fts5_cjk_extension"])
    if value["state_schema_probe"] != _STATE_SCHEMA_PROBE:
        raise OverlayContractError("state schema probe contract mismatch")
    _validate_boundary_plugin(value["platform_main_conversation_boundary"])


def _validate_fts5_cjk_extension(value: object) -> None:
    if not isinstance(value, dict):
        raise OverlayContractError("FTS5 CJK extension contract is missing")
    _require_exact_fields(
        value,
        {
            "contract",
            "immutable_path",
            "source_root",
            "source_files",
            "init_symbol",
            "tokenizer",
            "build_cflags",
        },
        "fts5_cjk_extension",
    )
    if (
        value["contract"] != "hermes-fts5-cjk-extension-v1"
        or value["immutable_path"]
        != "/opt/hermes-platform/lib/libfts5_cjk.so"
        or value["source_root"] != "native/fts5_cjk"
        or value["source_files"] != _FTS5_CJK_SOURCE_FILES
        or value["init_symbol"] != "sqlite3_fts5_cjk_init"
        or value["tokenizer"] != "cjk_unicode61"
        or value["build_cflags"]
        != ["-shared", "-fPIC", "-O2", "-Wall", "-Wextra"]
    ):
        raise OverlayContractError("FTS5 CJK extension contract mismatch")


def _validate_boundary_plugin(value: object) -> None:
    if not isinstance(value, dict):
        raise OverlayContractError("boundary marker plugin contract is missing")
    _require_exact_fields(
        value,
        {
            "contract",
            "distribution",
            "version",
            "entry_point_group",
            "entry_point_name",
            "entry_point_value",
            "middleware_kind",
            "bootstrap_fd",
            "header_name",
            "source_files",
            "golden_vector",
        },
        "platform_main_conversation_boundary",
    )
    identity = {
        "contract": "platform-main-conversation-boundary-v1",
        "distribution": "hermes-hermes-platform-main-conversation-boundary",
        "version": "0.1.0",
        "entry_point_group": "hermes_agent.plugins",
        "entry_point_name": "platform-main-conversation-boundary-v1",
        "entry_point_value": "platform_main_conversation_boundary.plugin",
        "middleware_kind": "llm_request",
        "bootstrap_fd": 198,
        "header_name": "X-Hermes-Platform-Main-Boundary",
    }
    if any(value[field] != expected for field, expected in identity.items()):
        raise OverlayContractError("boundary marker plugin identity mismatch")
    if type(value["bootstrap_fd"]) is not int:
        raise OverlayContractError("boundary marker bootstrap descriptor is invalid")
    source_files = value["source_files"]
    if not isinstance(source_files, dict) or set(source_files) != _PLUGIN_SOURCE_FILES:
        raise OverlayContractError("boundary plugin source set mismatch")
    for relative, digest in source_files.items():
        canonical_relative_path(relative)
        if not isinstance(digest, str) or _SHA256_RE.fullmatch(digest) is None:
            raise OverlayContractError("boundary plugin source digest is invalid")

    golden = value["golden_vector"]
    if not isinstance(golden, dict):
        raise OverlayContractError("boundary marker golden vector is missing")
    _require_exact_fields(
        golden,
        {
            "canonical_base_url",
            "key_base64url",
            "payload",
            "canonical_payload",
            "payload_digest",
            "header",
            "invalid_base_urls",
        },
        "boundary_marker_golden_vector",
    )
    payload = golden["payload"]
    if not isinstance(payload, dict):
        raise OverlayContractError("boundary marker golden payload is missing")
    payload_fields = {
        "contract_version",
        "route_token",
        "session_id",
        "task_id",
        "turn_id",
        "api_request_id",
        "api_call_count",
        "boundary_epoch_nonce",
        "request_nonce",
        "issued_at",
        "expires_at",
    }
    _require_exact_fields(payload, payload_fields, "boundary_marker_golden_payload")
    if type(payload["contract_version"]) is not int or payload["contract_version"] != 1:
        raise OverlayContractError("boundary marker contract version mismatch")
    if type(payload["api_call_count"]) is not int or payload["api_call_count"] < 0:
        raise OverlayContractError("boundary marker call count is invalid")
    route_token = payload["route_token"]
    if not isinstance(route_token, str) or re.fullmatch(r"[0-9a-f]{64}", route_token) is None:
        raise OverlayContractError("boundary marker route token is invalid")
    if golden["canonical_base_url"] != (
        f"http://127.0.0.1:9121/model-relay/{route_token}/v1"
    ):
        raise OverlayContractError("boundary marker canonical base URL mismatch")
    for field in payload_fields.difference({"contract_version", "api_call_count"}):
        if not isinstance(payload[field], str) or not payload[field]:
            raise OverlayContractError("boundary marker payload string is invalid")
    for field, expected_bytes in (
        ("key_base64url", 32),
        ("boundary_epoch_nonce", 16),
        ("request_nonce", 16),
    ):
        encoded = golden[field] if field == "key_base64url" else payload[field]
        try:
            decoded = base64.b64decode(
                encoded + "=" * (-len(encoded) % 4),
                altchars=b"-_",
                validate=True,
            )
        except (ValueError, TypeError) as exc:
            raise OverlayContractError("boundary marker base64url value is invalid") from exc
        if (
            len(decoded) != expected_bytes
            or base64.urlsafe_b64encode(decoded).rstrip(b"=").decode("ascii") != encoded
        ):
            raise OverlayContractError("boundary marker base64url value is invalid")
    encoded_payload = canonical_json(payload)
    if golden["canonical_payload"] != encoded_payload.decode("utf-8"):
        raise OverlayContractError("boundary marker canonical payload mismatch")
    if golden["payload_digest"] != "sha256:" + hashlib.sha256(encoded_payload).hexdigest():
        raise OverlayContractError("boundary marker payload digest mismatch")
    if not isinstance(golden["header"], str) or not golden["header"]:
        raise OverlayContractError("boundary marker golden header is invalid")
    invalid_urls = golden["invalid_base_urls"]
    if (
        not isinstance(invalid_urls, list)
        or len(invalid_urls) < 9
        or len(invalid_urls) != len(set(invalid_urls))
        or any(not isinstance(item, str) or not item for item in invalid_urls)
        or golden["canonical_base_url"] in invalid_urls
    ):
        raise OverlayContractError("boundary marker invalid URL set is malformed")


def _validate_component_surfaces(components: dict[str, Any]) -> None:
    for component, expected_modules in _COMPONENT_SURFACES.items():
        contract = components[component]
        modules = contract["modules"]
        if not isinstance(modules, list) or len(modules) != len(expected_modules):
            raise OverlayContractError(f"component module set mismatch: {component}")
        for actual, expected in zip(modules, expected_modules, strict=True):
            if not isinstance(actual, dict):
                raise OverlayContractError("component module entry must be a mapping")
            fields = set(expected)
            if expected["loader"] in {"dotted", "file"}:
                fields.add("source_digest")
            if set(actual) != fields or any(actual[field] != value for field, value in expected.items()):
                raise OverlayContractError(f"component module surface mismatch: {component}")
            if expected["loader"] in {"dotted", "file"}:
                canonical_relative_path(actual["source"])
                digest = actual["source_digest"]
                if not isinstance(digest, str) or _SHA256_RE.fullmatch(digest) is None:
                    raise OverlayContractError("component source digest is invalid")
        if contract["dependencies"] != _COMPONENT_DEPENDENCIES[component]:
            raise OverlayContractError(f"component dependency set mismatch: {component}")


def load_overlay_contract(path: Path) -> dict[str, Any]:
    try:
        payload = yaml.safe_load(path.read_text(encoding="utf-8"))
    except (OSError, yaml.YAMLError) as exc:
        raise OverlayContractError("cannot read Runtime overlay compatibility manifest") from exc
    if not isinstance(payload, dict):
        raise OverlayContractError("Runtime overlay compatibility manifest is not a mapping")
    _require_exact_fields(payload, _TOP_LEVEL_FIELDS, "manifest")
    expected_identity = {
        "schema_version": SCHEMA_VERSION,
        "upstream_version": UPSTREAM_VERSION,
        "release_tag": RELEASE_TAG,
        "upstream_commit": UPSTREAM_COMMIT,
        "source_archive_sha256": SOURCE_ARCHIVE_SHA256,
        "state_schema": STATE_SCHEMA,
        "config_schema": CONFIG_SCHEMA,
    }
    for field, expected in expected_identity.items():
        if payload.get(field) != expected:
            raise OverlayContractError(f"fixed upstream identity mismatch: {field}")
    for field in ("schema_version", "state_schema", "config_schema"):
        if type(payload[field]) is not int:
            raise OverlayContractError(f"fixed upstream identity has wrong type: {field}")
    modes = payload.get("supported_pod_configs")
    if not isinstance(modes, dict) or modes != {
        "personal_agent": True,
        "application_agent": True,
    }:
        raise OverlayContractError("supported Pod configurations mismatch")

    if payload.get("required_checks") != _REQUIRED_CHECKS:
        raise OverlayContractError("required Candidate check set mismatch")
    _validate_runtime_overlay(payload.get("runtime_overlay"))

    upstream = payload.get("upstream_components")
    if not isinstance(upstream, dict) or set(upstream) != {
        "contract_version",
        "probe_contract",
        "immutable_source_roots",
        "vector_digest_contract",
        "distribution_fingerprint_contract",
        "components",
    }:
        raise OverlayContractError("upstream component probe contract mismatch")
    if (
        type(upstream.get("contract_version")) is not int
        or upstream.get("contract_version") != 1
        or upstream.get("probe_contract")
        != "hermes-upstream-components-0.20.1-v1"
        or upstream.get("immutable_source_roots") != ["/opt/hermes"]
        or upstream.get("vector_digest_contract") != "vector-actual-projection-v1"
        or upstream.get("distribution_fingerprint_contract")
        != "python-distributions-jcs-v1"
    ):
        raise OverlayContractError("upstream component contract identity mismatch")
    components = upstream.get("components")
    if not isinstance(components, dict) or set(components) != COMPONENTS:
        raise OverlayContractError("upstream component set mismatch")
    _validate_vectors(components)
    _validate_component_surfaces(components)
    return payload
