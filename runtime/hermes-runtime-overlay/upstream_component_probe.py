#!/usr/bin/env python3
"""Isolated, side-effect-denying probe for fixed Hermes 0.20.1 components."""

from __future__ import annotations

import argparse
import base64
from datetime import datetime, timezone
import hashlib
import importlib
import importlib.metadata
import importlib.util
import json
import os
from pathlib import Path
import re
import shutil
import stat
import subprocess
import sys
import sysconfig
import tempfile
import threading
from typing import Any, Iterable
import uuid

from overlay_contract import load_overlay_contract, sha256_jcs


PROBE_CONTRACT = "hermes-upstream-components-0.20.1-v1"
ALLOWED_NETWORK_GUARDS = {"container_none", "python_audit_denied"}
_PEP503_RE = re.compile(r"[-_.]+")
_HEX32_RE = re.compile(r"[0-9a-f]{32}")
_HEX40_RE = re.compile(r"[0-9a-f]{40}")
_SHA256_RE = re.compile(r"sha256:[0-9a-f]{64}")
_RFC3339_MILLISECONDS_RE = re.compile(
    r"[0-9]{4}-[0-9]{2}-[0-9]{2}T[0-9]{2}:[0-9]{2}:[0-9]{2}\.[0-9]{3}Z"
)
_IMPORT_SIDE_EFFECT_FIELDS = {
    "thread_delta",
    "tool_registry_unchanged",
    "tts_registry_unchanged",
    "image_registry_unchanged",
    "platform_registry_unchanged",
    "model_provider_registry_unchanged",
    "socket_attempts",
    "subprocess_attempts",
}
_FACT_FIELDS = {
    "voice": {
        "source_present",
        "module_importable",
        "contract_ready",
        "edge_tts_sdk_ready",
        "ffmpeg_present",
        "stt_ready",
        "audio_capture_ready",
        "audio_ingress_ready",
    },
    "a2a": {
        "source_present",
        "module_importable",
        "contract_ready",
        "protocol_ready",
        "security_ready",
    },
    "webhook": {
        "source_present",
        "module_importable",
        "contract_ready",
        "outbound_hmac_ready",
        "inbound_filter_ready",
    },
    "image": {
        "source_present",
        "module_importable",
        "contract_ready",
        "provider_abc_ready",
        "normalization_ready",
        "registry_unchanged",
    },
}


class UpstreamComponentProbeError(RuntimeError):
    pass


def normalized_distribution_name(value: str) -> str:
    return _PEP503_RE.sub("-", value.strip().lower())


def _digest_open_file(path: Path) -> str:
    flags = os.O_RDONLY | getattr(os, "O_CLOEXEC", 0) | getattr(os, "O_NOFOLLOW", 0)
    try:
        descriptor = os.open(path, flags)
    except OSError as exc:
        raise UpstreamComponentProbeError(f"cannot securely open {path.name}") from exc
    try:
        digest = hashlib.sha256()
        while True:
            chunk = os.read(descriptor, 1024 * 1024)
            if not chunk:
                break
            digest.update(chunk)
    finally:
        os.close(descriptor)
    return "sha256:" + digest.hexdigest()


def _distribution_metadata_digest(
    distribution: importlib.metadata.Distribution,
    filename: str,
) -> str | None:
    matches = [
        item
        for item in (distribution.files or [])
        if len(item.parts) == 2
        and item.parts[0].endswith(".dist-info")
        and item.parts[1] == filename
    ]
    if len(matches) > 1:
        raise UpstreamComponentProbeError(
            f"distribution has ambiguous {filename}: {distribution.metadata.get('Name')}"
        )
    if not matches:
        return None
    path = Path(distribution.locate_file(matches[0]))
    try:
        mode = path.lstat().st_mode
    except OSError as exc:
        raise UpstreamComponentProbeError(f"distribution {filename} is missing") from exc
    if not stat.S_ISREG(mode) or stat.S_ISLNK(mode):
        raise UpstreamComponentProbeError(f"distribution {filename} is not a regular file")
    return _digest_open_file(path)


def distribution_entry(
    distribution: importlib.metadata.Distribution,
) -> dict[str, str | None]:
    raw_name = distribution.metadata.get("Name")
    if not isinstance(raw_name, str) or not raw_name.strip():
        raise UpstreamComponentProbeError("installed distribution has no name")
    return {
        "name": normalized_distribution_name(raw_name),
        "version": str(distribution.version),
        "record_digest": _distribution_metadata_digest(distribution, "RECORD"),
        "direct_url_digest": _distribution_metadata_digest(distribution, "direct_url.json"),
    }


def distribution_inventory(
    distributions: Iterable[importlib.metadata.Distribution] | None = None,
) -> list[dict[str, str | None]]:
    if distributions is None:
        site_packages = sorted(
            {
                sysconfig.get_path("purelib"),
                sysconfig.get_path("platlib"),
            }
        )
        distributions = importlib.metadata.distributions(path=site_packages)
    entries = [
        distribution_entry(distribution)
        for distribution in distributions
    ]
    entries.sort(key=lambda item: str(item["name"]))
    names = [entry["name"] for entry in entries]
    if len(names) != len(set(names)):
        raise UpstreamComponentProbeError("normalized distribution names are not unique")
    return entries


def python_distribution_fingerprint() -> tuple[str, dict[str, Any]]:
    try:
        from packaging.tags import sys_tags
    except ImportError as exc:
        raise UpstreamComponentProbeError("packaging.tags is required for ABI evidence") from exc
    first_tag = next(iter(sys_tags()), None)
    if first_tag is None:
        raise UpstreamComponentProbeError("Python interpreter produced no compatibility tag")
    projection = {
        "contract": "python-distributions-jcs-v1",
        "implementation": sys.implementation.name,
        "python_version": sys.version.split()[0],
        "abi_tag": first_tag.abi,
        "platform_tag": first_tag.platform,
        "distributions": distribution_inventory(),
    }
    return sha256_jcs(projection), projection


def _safe_source(root: Path, relative: str, expected_digest: str) -> tuple[Path, str]:
    target = root.joinpath(*relative.split("/"))
    try:
        resolved_root = root.resolve(strict=True)
        resolved = target.resolve(strict=True)
        resolved.relative_to(resolved_root)
    except (OSError, ValueError) as exc:
        raise UpstreamComponentProbeError(f"module source escaped immutable root: {relative}") from exc
    cursor = root
    for part in relative.split("/"):
        cursor = cursor / part
        try:
            mode = cursor.lstat().st_mode
        except OSError as exc:
            raise UpstreamComponentProbeError(f"module source is missing: {relative}") from exc
        if stat.S_ISLNK(mode):
            raise UpstreamComponentProbeError(f"module source uses symlink: {relative}")
    if not resolved.is_file():
        raise UpstreamComponentProbeError(f"module source is not a regular file: {relative}")
    digest = _digest_open_file(resolved)
    if digest != expected_digest:
        raise UpstreamComponentProbeError(f"module source digest mismatch: {relative}")
    return resolved, digest


def _load_module(entry: dict[str, Any], hermes_root: Path) -> tuple[Any, str]:
    loader = entry["loader"]
    if loader in {"dotted", "file"}:
        source, digest = _safe_source(
            hermes_root,
            str(entry["source"]),
            str(entry["source_digest"]),
        )
        if loader == "dotted":
            module = importlib.import_module(str(entry["module"]))
            origin = Path(str(getattr(module, "__file__", ""))).resolve(strict=True)
            if origin != source:
                raise UpstreamComponentProbeError(
                    f"dotted module resolved to the wrong source: {entry['module']}"
                )
        else:
            module_name = "_platform_probe_" + hashlib.sha256(
                str(entry["module"]).encode("utf-8")
            ).hexdigest()[:16]
            spec = importlib.util.spec_from_file_location(module_name, source)
            if spec is None or spec.loader is None:
                raise UpstreamComponentProbeError(f"cannot load leaf module: {source.name}")
            module = importlib.util.module_from_spec(spec)
            spec.loader.exec_module(module)
        return module, digest
    if loader != "distribution":
        raise UpstreamComponentProbeError(f"unsupported module loader: {loader}")
    module = importlib.import_module(str(entry["module"]))
    origin_value = getattr(module, "__file__", None)
    if not isinstance(origin_value, str):
        raise UpstreamComponentProbeError(f"distribution module has no file: {entry['module']}")
    origin = Path(origin_value)
    try:
        mode = origin.lstat().st_mode
    except OSError as exc:
        raise UpstreamComponentProbeError("distribution module origin is missing") from exc
    if not stat.S_ISREG(mode) or stat.S_ISLNK(mode):
        raise UpstreamComponentProbeError("distribution module origin is not a regular file")
    return module, _digest_open_file(origin)


def _registry_maps(
    module_name: str,
    *,
    owner_name: str | None,
    global_name: str,
    scoped_name: str | None,
) -> dict[str, Any]:
    """Snapshot already-loaded registries without importing product surfaces."""

    module = sys.modules.get(module_name)
    if module is None:
        return {"global": [], "scoped": []}
    owner = getattr(module, owner_name, None) if owner_name else module
    global_values = getattr(owner, global_name, None)
    if not isinstance(global_values, dict):
        raise UpstreamComponentProbeError(f"invalid registry mapping: {module_name}")
    scoped: list[dict[str, Any]] = []
    if scoped_name is not None:
        raw_scoped = getattr(owner, scoped_name, None)
        if not isinstance(raw_scoped, dict):
            raise UpstreamComponentProbeError(f"invalid scoped registry: {module_name}")
        for scope, values in sorted(raw_scoped.items(), key=lambda item: str(item[0])):
            if not isinstance(values, dict):
                raise UpstreamComponentProbeError(
                    f"invalid scoped registry mapping: {module_name}"
                )
            scoped.append({"scope": str(scope), "names": sorted(map(str, values))})
    return {"global": sorted(map(str, global_values)), "scoped": scoped}


def _registry_snapshot() -> dict[str, Any]:
    return {
        "tools": _registry_maps(
            "tools.registry",
            owner_name="registry",
            global_name="_tools",
            scoped_name="_scoped_tools",
        ),
        "tts": _registry_maps(
            "agent.tts_registry",
            owner_name=None,
            global_name="_providers",
            scoped_name="_scoped_providers",
        ),
        "image": _registry_maps(
            "agent.image_gen_registry",
            owner_name=None,
            global_name="_providers",
            scoped_name="_scoped_providers",
        ),
        "platform": _registry_maps(
            "gateway.platform_registry",
            owner_name="platform_registry",
            global_name="_entries",
            scoped_name="_scoped_entries",
        ),
        "model_provider": _registry_maps(
            "hermes_cli.auth",
            owner_name=None,
            global_name="PROVIDER_REGISTRY",
            scoped_name=None,
        ),
    }


def _projection(value: Any) -> Any:
    if isinstance(value, bytes):
        return {
            "type": "bytes-base64url",
            "value": base64.urlsafe_b64encode(value).rstrip(b"=").decode("ascii"),
        }
    if isinstance(value, dict):
        return {str(key): _projection(item) for key, item in value.items()}
    if isinstance(value, (list, tuple)):
        return [_projection(item) for item in value]
    if value is None or isinstance(value, (bool, int, float, str)):
        return value
    raise UpstreamComponentProbeError("vector produced a non-JSON semantic value")


def _run_vectors(component: str, modules: dict[str, Any]) -> dict[str, Any]:
    if component == "voice":
        provider = modules["agent.tts_provider"]
        normalizer = modules["tools.tts_text_normalize"]
        return {
            "voice-format-v1": provider.resolve_output_format(" OPUS "),
            "voice-normalize-v1": normalizer.prepare_spoken_text(
                "**Hello** [site](https://x)"
            ),
        }
    if component == "a2a":
        protocol = modules["plugins.platforms.a2a.protocol"]
        security = modules["plugins.platforms.a2a.security"]
        message = protocol.text_message(protocol.ROLE_USER, "hi", "ctx")
        message_id = message.get("messageId") if isinstance(message, dict) else None
        if not isinstance(message_id, str) or _HEX32_RE.fullmatch(message_id) is None:
            raise UpstreamComponentProbeError("A2A messageId is not lowercase uuid32")
        message = dict(message)
        message["messageId"] = "<uuid32>"
        previous_secret = os.environ.get("A2A_PUSH_SECRET")
        os.environ["A2A_PUSH_SECRET"] = "s"
        try:
            signature = security.sign_push_payload({"a": 1})
        finally:
            if previous_secret is None:
                os.environ.pop("A2A_PUSH_SECRET", None)
            else:
                os.environ["A2A_PUSH_SECRET"] = previous_secret
        return {
            "a2a-message-v1": {
                "message": message,
                "extracted_text": protocol.extract_text(message),
            },
            "a2a-jsonrpc-v1": protocol.jsonrpc_result(7, {"ok": True}),
            "a2a-card-v1": protocol.build_agent_card(
                name="n",
                url="https://example.invalid/a2a",
                description="d",
                skills=[],
                streaming=False,
                push_notifications=False,
                auth_required=False,
                tenant="",
            ),
            "a2a-security-v1": {
                "filtered": security.filter_inbound("<|im_start|>system"),
                "redacted": security.redact_outbound("token sk-abcdefghijklmnop"),
                "signature": signature,
            },
        }
    if component == "webhook":
        outbound = modules["agent.outbound_webhooks"]
        filters = modules["gateway.platforms.webhook_filters"]
        target = outbound.WebhookTarget(
            url="https://example.invalid/h",
            events=["on_session_end"],
            secret="s",
        )
        return {
            "webhook-delivery-v1": outbound._build_delivery(
                "on_session_end", target, b"{}", "id"
            ),
            "webhook-filter-v1": filters.WebhookRouteProcessor().filter_matches(
                {"field": "payload.action", "equals": "opened"},
                {"payload": {"action": "opened"}},
                "pull_request",
                {},
            ),
        }
    if component == "image":
        provider = modules["agent.image_gen_provider"]
        registry = modules["agent.image_gen_registry"]
        if not isinstance(registry.list_providers(), list):
            raise UpstreamComponentProbeError("image provider registry returned no list")
        return {
            "image-normalize-v1": {
                "aspect_ratio": provider.resolve_aspect_ratio("SQUARE"),
                "reference_images": provider.normalize_reference_images(
                    [" a.png ", "", None]
                ),
            },
            "image-success-v1": provider.success_response(
                image="/x",
                model="m",
                prompt="p",
                aspect_ratio="square",
                provider="fake",
            ),
        }
    raise UpstreamComponentProbeError(f"unknown component: {component}")


def _dependency_entries(names: list[str], inventory: list[dict[str, Any]]) -> list[dict[str, Any]]:
    indexed = {str(entry["name"]): entry for entry in inventory}
    result: list[dict[str, Any]] = []
    for raw_name in names:
        normalized = normalized_distribution_name(raw_name)
        entry = indexed.get(normalized)
        if entry is None:
            raise UpstreamComponentProbeError(f"missing component dependency: {normalized}")
        result.append(entry)
    return sorted(result, key=lambda item: str(item["name"]))


def _component_facts(component: str, importable: bool, *, registry_unchanged: bool) -> dict[str, bool]:
    if component == "voice":
        return {
            "source_present": True,
            "module_importable": importable,
            "contract_ready": importable,
            "edge_tts_sdk_ready": importable,
            "ffmpeg_present": shutil.which("ffmpeg") is not None,
            "stt_ready": False,
            "audio_capture_ready": False,
            "audio_ingress_ready": False,
        }
    if component == "a2a":
        return {
            "source_present": True,
            "module_importable": importable,
            "contract_ready": importable,
            "protocol_ready": importable,
            "security_ready": importable,
        }
    if component == "webhook":
        return {
            "source_present": True,
            "module_importable": importable,
            "contract_ready": importable,
            "outbound_hmac_ready": importable,
            "inbound_filter_ready": importable,
        }
    return {
        "source_present": True,
        "module_importable": importable,
        "contract_ready": importable,
        "provider_abc_ready": importable,
        "normalization_ready": importable,
        "registry_unchanged": registry_unchanged,
    }


def _clear_provider_credentials() -> None:
    for name in tuple(os.environ):
        upper = name.upper()
        if (
            any(
                token in upper
                for token in (
                    "API_KEY",
                    "ACCESS_KEY",
                    "AUTH_TOKEN",
                    "ACCESS_TOKEN",
                    "SECRET",
                    "PASSWORD",
                    "CREDENTIAL",
                )
            )
            or upper.endswith("_KEY")
            or upper.endswith("_TOKEN")
        ):
            os.environ.pop(name, None)
    os.environ["HERMES_DISABLE_LAZY_INSTALLS"] = "1"
    os.environ.pop("HERMES_LAZY_INSTALL_TARGET", None)


def _validate_probe_inputs(
    manifest: dict[str, Any],
    *,
    hermes_root: Path,
    platform_revision: str,
    network_guard: str,
) -> None:
    if _HEX40_RE.fullmatch(platform_revision) is None:
        raise UpstreamComponentProbeError("platform revision must be a lowercase Git SHA")
    if network_guard not in ALLOWED_NETWORK_GUARDS:
        raise UpstreamComponentProbeError("network guard contract is invalid")
    allowed_roots = manifest["upstream_components"]["immutable_source_roots"]
    if str(hermes_root) not in allowed_roots:
        raise UpstreamComponentProbeError("Hermes root is not allowed by the manifest")
    try:
        mode = hermes_root.lstat().st_mode
        resolved = hermes_root.resolve(strict=True)
    except OSError as exc:
        raise UpstreamComponentProbeError("immutable Hermes root is missing") from exc
    if not stat.S_ISDIR(mode) or stat.S_ISLNK(mode) or resolved != hermes_root:
        raise UpstreamComponentProbeError("immutable Hermes root is not canonical")


def _install_audit_guard(counters: dict[str, int]) -> None:
    def audit(event: str, _args: tuple[Any, ...]) -> None:
        if event.startswith("socket."):
            counters["socket_attempts"] += 1
            raise PermissionError("component probe denies socket operations")
        if event.startswith("subprocess.") or event in {
            "os.system",
            "os.posix_spawn",
            "os.posix_spawnp",
        }:
            counters["subprocess_attempts"] += 1
            raise PermissionError("component probe denies subprocess operations")

    sys.addaudithook(audit)


def _isolated_probe(
    *,
    manifest_path: Path,
    hermes_root: Path,
    platform_revision: str,
    network_guard: str,
) -> dict[str, Any]:
    if sys.version_info < (3, 11) or sys.version_info >= (3, 14):
        raise UpstreamComponentProbeError("probe interpreter is outside >=3.11,<3.14")
    manifest = load_overlay_contract(manifest_path)
    _validate_probe_inputs(
        manifest,
        hermes_root=hermes_root,
        platform_revision=platform_revision,
        network_guard=network_guard,
    )
    _clear_provider_credentials()
    if str(hermes_root) not in sys.path:
        sys.path.insert(0, str(hermes_root))
    before_threads = threading.active_count()
    counters = {"socket_attempts": 0, "subprocess_attempts": 0}
    _install_audit_guard(counters)
    before_registry = _registry_snapshot()
    fingerprint, fingerprint_projection = python_distribution_fingerprint()
    inventory = fingerprint_projection["distributions"]
    results: dict[str, Any] = {}
    component_contracts = manifest["upstream_components"]["components"]
    for component in sorted(component_contracts):
        contract = component_contracts[component]
        loaded: dict[str, Any] = {}
        module_symbols: list[dict[str, Any]] = []
        for entry in contract["modules"]:
            module, source_digest = _load_module(entry, hermes_root)
            loaded[str(entry["module"])] = module
            symbols = entry.get("symbols") or ["<module>"]
            for symbol in symbols:
                present = symbol == "<module>" or hasattr(module, symbol)
                if not present:
                    raise UpstreamComponentProbeError(
                        f"missing component symbol: {entry['module']}.{symbol}"
                    )
                module_symbols.append(
                    {
                        "module": entry["module"],
                        "loader": entry["loader"],
                        "symbol": symbol,
                        "source_digest": source_digest,
                        "present": True,
                        "importable": True,
                    }
                )
        actual_vectors = {
            vector_id: _projection(value)
            for vector_id, value in _run_vectors(component, loaded).items()
        }
        if set(actual_vectors) != set(contract["vectors"]):
            raise UpstreamComponentProbeError(f"vector set drifted: {component}")
        vector_results: dict[str, Any] = {}
        for vector_id in sorted(actual_vectors):
            expected = contract["vectors"][vector_id]
            actual_projection = actual_vectors[vector_id]
            actual_digest = sha256_jcs(
                {"vector_id": vector_id, "actual_projection": actual_projection}
            )
            passed = (
                actual_projection == expected["expected_projection"]
                and actual_digest == expected["expected_digest"]
            )
            if not passed:
                raise UpstreamComponentProbeError(f"golden vector mismatch: {vector_id}")
            vector_results[vector_id] = {
                "passed": True,
                "actual_digest": actual_digest,
            }
        current_registry = _registry_snapshot()
        registry_unchanged = current_registry == before_registry
        if not registry_unchanged:
            raise UpstreamComponentProbeError(f"component changed a registry: {component}")
        thread_delta = threading.active_count() - before_threads
        if thread_delta != 0 or counters != {"socket_attempts": 0, "subprocess_attempts": 0}:
            raise UpstreamComponentProbeError(f"component import had side effects: {component}")
        facts = _component_facts(
            component,
            True,
            registry_unchanged=registry_unchanged,
        )
        if component == "voice" and facts["ffmpeg_present"] is not True:
            raise UpstreamComponentProbeError("Voice component requires ffmpeg")
        results[component] = {
            "module_symbols": sorted(
                module_symbols,
                key=lambda item: (str(item["module"]), str(item["symbol"])),
            ),
            "vector_results": vector_results,
            "dependencies": _dependency_entries(contract["dependencies"], inventory),
            "facts": facts,
            "import_side_effects": {
                "thread_delta": thread_delta,
                "tool_registry_unchanged": current_registry["tools"]
                == before_registry["tools"],
                "tts_registry_unchanged": current_registry["tts"]
                == before_registry["tts"],
                "image_registry_unchanged": current_registry["image"]
                == before_registry["image"],
                "platform_registry_unchanged": current_registry["platform"]
                == before_registry["platform"],
                "model_provider_registry_unchanged": current_registry["model_provider"]
                == before_registry["model_provider"],
                **counters,
            },
        }
    report: dict[str, Any] = {
        "contract_version": 1,
        "probe_contract": PROBE_CONTRACT,
        "hermes_commit": manifest["upstream_commit"],
        "platform_revision": platform_revision,
        "probe_id": str(uuid.uuid4()),
        "generated_at": datetime.now(timezone.utc).isoformat(timespec="milliseconds").replace(
            "+00:00", "Z"
        ),
        "python_distribution_fingerprint": fingerprint,
        "components": results,
        "network_guard": network_guard,
    }
    report["report_digest"] = sha256_jcs(report)
    return report


def _validate_distribution_report_entry(value: object) -> str:
    if not isinstance(value, dict) or set(value) != {
        "name",
        "version",
        "record_digest",
        "direct_url_digest",
    }:
        raise UpstreamComponentProbeError("component dependency entry is not closed")
    name = value.get("name")
    version = value.get("version")
    if (
        not isinstance(name, str)
        or normalized_distribution_name(name) != name
        or not isinstance(version, str)
        or not version
    ):
        raise UpstreamComponentProbeError("component dependency identity is invalid")
    for field in ("record_digest", "direct_url_digest"):
        digest = value.get(field)
        if digest is not None and (
            not isinstance(digest, str) or _SHA256_RE.fullmatch(digest) is None
        ):
            raise UpstreamComponentProbeError("component dependency digest is invalid")
    return name


def validate_probe_report(
    report: object,
    *,
    manifest: dict[str, Any],
    platform_revision: str,
    network_guard: str,
) -> dict[str, Any]:
    if not isinstance(report, dict) or set(report) != {
        "contract_version",
        "probe_contract",
        "hermes_commit",
        "platform_revision",
        "probe_id",
        "generated_at",
        "python_distribution_fingerprint",
        "components",
        "network_guard",
        "report_digest",
    }:
        raise UpstreamComponentProbeError("isolated component report fields mismatch")
    if (
        report.get("contract_version") != 1
        or report.get("probe_contract") != PROBE_CONTRACT
        or report.get("hermes_commit") != manifest["upstream_commit"]
        or report.get("platform_revision") != platform_revision
        or report.get("network_guard") != network_guard
    ):
        raise UpstreamComponentProbeError("isolated component report identity mismatch")
    try:
        parsed_probe_id = uuid.UUID(str(report.get("probe_id")))
    except (ValueError, AttributeError) as exc:
        raise UpstreamComponentProbeError("isolated component probe_id is invalid") from exc
    if str(parsed_probe_id) != report.get("probe_id"):
        raise UpstreamComponentProbeError("isolated component probe_id is non-canonical")
    generated_at = report.get("generated_at")
    if (
        not isinstance(generated_at, str)
        or _RFC3339_MILLISECONDS_RE.fullmatch(generated_at) is None
    ):
        raise UpstreamComponentProbeError("isolated component timestamp is invalid")
    fingerprint = report.get("python_distribution_fingerprint")
    if not isinstance(fingerprint, str) or _SHA256_RE.fullmatch(fingerprint) is None:
        raise UpstreamComponentProbeError("Python distribution fingerprint is invalid")
    components = report.get("components")
    contracts = manifest["upstream_components"]["components"]
    if not isinstance(components, dict) or set(components) != set(contracts):
        raise UpstreamComponentProbeError("isolated component report set mismatch")
    for component, contract in contracts.items():
        result = components[component]
        if not isinstance(result, dict) or set(result) != {
            "module_symbols",
            "vector_results",
            "dependencies",
            "facts",
            "import_side_effects",
        }:
            raise UpstreamComponentProbeError(f"component report fields mismatch: {component}")
        expected_symbols = sorted(
            (
                str(module["module"]),
                str(module["loader"]),
                str(symbol),
            )
            for module in contract["modules"]
            for symbol in (module.get("symbols") or ["<module>"])
        )
        actual_symbols: list[tuple[str, str, str]] = []
        for item in result["module_symbols"]:
            if not isinstance(item, dict) or set(item) != {
                "module",
                "loader",
                "symbol",
                "source_digest",
                "present",
                "importable",
            }:
                raise UpstreamComponentProbeError("component symbol entry is not closed")
            digest = item.get("source_digest")
            if (
                item.get("present") is not True
                or item.get("importable") is not True
                or not isinstance(digest, str)
                or _SHA256_RE.fullmatch(digest) is None
            ):
                raise UpstreamComponentProbeError("component symbol evidence is invalid")
            actual_symbols.append(
                (str(item.get("module")), str(item.get("loader")), str(item.get("symbol")))
            )
        if actual_symbols != expected_symbols:
            raise UpstreamComponentProbeError(f"component symbol set mismatch: {component}")
        vector_results = result["vector_results"]
        if not isinstance(vector_results, dict) or set(vector_results) != set(
            contract["vectors"]
        ):
            raise UpstreamComponentProbeError(f"component vector set mismatch: {component}")
        for vector_id, vector_result in vector_results.items():
            if (
                not isinstance(vector_result, dict)
                or set(vector_result) != {"passed", "actual_digest"}
                or vector_result.get("passed") is not True
                or vector_result.get("actual_digest")
                != contract["vectors"][vector_id]["expected_digest"]
            ):
                raise UpstreamComponentProbeError(f"component vector failed: {vector_id}")
        dependencies = result["dependencies"]
        if not isinstance(dependencies, list):
            raise UpstreamComponentProbeError("component dependencies are not a list")
        dependency_names = [_validate_distribution_report_entry(item) for item in dependencies]
        expected_dependencies = sorted(
            normalized_distribution_name(name) for name in contract["dependencies"]
        )
        if dependency_names != expected_dependencies:
            raise UpstreamComponentProbeError(
                f"component dependency set mismatch: {component}"
            )
        facts = result["facts"]
        if (
            not isinstance(facts, dict)
            or set(facts) != _FACT_FIELDS[component]
            or any(not isinstance(value, bool) for value in facts.values())
        ):
            raise UpstreamComponentProbeError(f"component facts mismatch: {component}")
        expected_false = (
            {"stt_ready", "audio_capture_ready", "audio_ingress_ready"}
            if component == "voice"
            else set()
        )
        if any(facts[field] is not False for field in expected_false) or any(
            facts[field] is not True for field in set(facts).difference(expected_false)
        ):
            raise UpstreamComponentProbeError(f"component facts did not pass: {component}")
        side_effects = result["import_side_effects"]
        if not isinstance(side_effects, dict) or set(side_effects) != _IMPORT_SIDE_EFFECT_FIELDS:
            raise UpstreamComponentProbeError(
                f"component side-effect evidence mismatch: {component}"
            )
        if (
            side_effects.get("thread_delta") != 0
            or side_effects.get("socket_attempts") != 0
            or side_effects.get("subprocess_attempts") != 0
            or any(
                side_effects[field] is not True
                for field in _IMPORT_SIDE_EFFECT_FIELDS
                if field.endswith("_unchanged")
            )
        ):
            raise UpstreamComponentProbeError(
                f"component import side effects were observed: {component}"
            )
    expected_digest = report.get("report_digest")
    without_digest = dict(report)
    without_digest.pop("report_digest")
    if expected_digest != sha256_jcs(without_digest):
        raise UpstreamComponentProbeError("isolated component report digest mismatch")
    return report


def run_isolated_probe(
    *,
    manifest_path: Path,
    hermes_root: Path,
    platform_revision: str,
    network_guard: str,
    timeout_seconds: int = 90,
) -> dict[str, Any]:
    manifest = load_overlay_contract(manifest_path)
    _validate_probe_inputs(
        manifest,
        hermes_root=hermes_root,
        platform_revision=platform_revision,
        network_guard=network_guard,
    )
    with tempfile.TemporaryDirectory(prefix="hermes-upstream-component-probe-") as temporary:
        environment = dict(os.environ)
        for name in tuple(environment):
            upper = name.upper()
            if (
                any(
                    token in upper
                    for token in (
                        "API_KEY",
                        "ACCESS_KEY",
                        "AUTH_TOKEN",
                        "ACCESS_TOKEN",
                        "SECRET",
                        "PASSWORD",
                        "CREDENTIAL",
                    )
                )
                or upper.endswith("_KEY")
                or upper.endswith("_TOKEN")
            ):
                environment.pop(name, None)
        environment["HERMES_HOME"] = temporary
        environment["HOME"] = temporary
        environment["XDG_CONFIG_HOME"] = str(Path(temporary) / ".config")
        environment["AWS_EC2_METADATA_DISABLED"] = "true"
        environment["HERMES_DISABLE_LAZY_INSTALLS"] = "1"
        environment.pop("HERMES_LAZY_INSTALL_TARGET", None)
        environment["PYTHONPATH"] = str(hermes_root)
        command = [
            sys.executable,
            str(Path(__file__).resolve()),
            "--isolated-child",
            "--manifest",
            str(manifest_path),
            "--hermes-root",
            str(hermes_root),
            "--platform-revision",
            platform_revision,
            "--network-guard",
            network_guard,
        ]
        completed = subprocess.run(
            command,
            capture_output=True,
            check=False,
            env=environment,
            text=True,
            timeout=timeout_seconds,
        )
    if completed.returncode != 0:
        detail = (completed.stderr or "isolated component probe failed").strip()[:1000]
        raise UpstreamComponentProbeError(detail)
    try:
        report = json.loads(completed.stdout)
    except json.JSONDecodeError as exc:
        raise UpstreamComponentProbeError("isolated component probe returned invalid JSON") from exc
    return validate_probe_report(
        report,
        manifest=manifest,
        platform_revision=platform_revision,
        network_guard=network_guard,
    )


def _main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--manifest", required=True)
    parser.add_argument("--hermes-root", default="/opt/hermes")
    parser.add_argument("--platform-revision", required=True)
    parser.add_argument(
        "--network-guard",
        required=True,
        choices=sorted(ALLOWED_NETWORK_GUARDS),
    )
    parser.add_argument("--isolated-child", action="store_true")
    args = parser.parse_args()
    if args.isolated_child:
        report = _isolated_probe(
            manifest_path=Path(args.manifest),
            hermes_root=Path(args.hermes_root),
            platform_revision=args.platform_revision,
            network_guard=args.network_guard,
        )
    else:
        report = run_isolated_probe(
            manifest_path=Path(args.manifest),
            hermes_root=Path(args.hermes_root),
            platform_revision=args.platform_revision,
            network_guard=args.network_guard,
        )
    json.dump(report, sys.stdout, sort_keys=True)
    sys.stdout.write("\n")
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(_main())
    except (OSError, UpstreamComponentProbeError, RuntimeError) as exc:
        print(str(exc), file=sys.stderr)
        raise SystemExit(2) from exc
