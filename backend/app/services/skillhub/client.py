from __future__ import annotations

import base64
import hashlib
import json
from dataclasses import dataclass
from io import BytesIO
from typing import Any
from urllib.parse import urlparse
from zipfile import BadZipFile, ZipFile

import httpx
from cryptography.exceptions import InvalidSignature
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PublicKey

from app.config import Settings, settings


class SkillHubError(RuntimeError):
    """A fail-closed provider or package verification error."""

    def __init__(self, code: str, message: str | None = None) -> None:
        self.code = code
        super().__init__(message or code)


@dataclass(frozen=True)
class SkillHubSkill:
    namespace: str
    public_slug: str
    version: str
    name: str
    description: str | None
    category: str | None
    downloads: int | None
    installs: int | None
    visibility: str | None
    pricing: str | None
    upstream_internal_slug: str | None = None


@dataclass(frozen=True)
class SkillHubPage:
    items: tuple[SkillHubSkill, ...]
    next_cursor: str | None


@dataclass(frozen=True)
class SkillHubVerifiedPackage:
    content: bytes
    package_sha256: str
    signature_key_id: str | None
    verified_content_sha256: str | None


class SkillHubClient:
    """The only component that knows SkillHub's community HTTP protocol."""

    def __init__(
        self, *, config: Settings = settings, transport: httpx.AsyncBaseTransport | None = None
    ) -> None:
        self.config = config
        self.transport = transport
        parsed = urlparse(config.SKILLHUB_API_BASE_URL)
        self.base_url = f"{parsed.scheme}://{parsed.netloc}".rstrip("/")
        self.api_hostname = parsed.hostname or ""
        self.api_host_header = parsed.netloc
        self.proxy_url = config.SKILLHUB_PROXY_URL.strip() or None
        self.proxy_connect_ip = config.SKILLHUB_PROXY_CONNECT_IP.strip() or None

    async def list_skills(
        self, *, feed: str, query: str | None, cursor: str | None, limit: int
    ) -> SkillHubPage:
        if feed not in self.config.skillhub_allowed_feeds:
            raise SkillHubError("skillhub_feed_not_allowed")
        # SkillHub's public marketplace uses /api/skills.  The legacy
        # /api/v1/search endpoint accepts unknown q/query parameters but
        # silently returns its default candidates, which makes a no-match
        # search look like a successful default listing.
        params: dict[str, str | int] = {
            "page": 1,
            "pageSize": limit,
            "sortBy": {"featured": "score", "hot": "downloads", "trending": "updated_at"}[feed],
            "order": "desc",
        }
        if query:
            params["keyword"] = query
        # The provider's public search endpoint uses numbered pages rather
        # than cursors.  Keep the caller contract stable, but never reuse a
        # cursor as a search parameter the provider does not understand.
        payload = await self._json("/api/skills", params=params)
        data = _data(payload)
        raw_items = data.get("items") or data.get("skills") or data.get("results") or []
        if not isinstance(raw_items, list):
            raise SkillHubError("skillhub_protocol_invalid", "search response has no skill list")
        return SkillHubPage(
            items=tuple(_skill(item) for item in raw_items if isinstance(item, dict)),
            next_cursor=_string(
                data.get("next_cursor") or data.get("nextCursor") or data.get("cursor")
            ),
        )

    async def resolve(
        self, *, namespace: str, public_slug: str, version: str | None = None
    ) -> SkillHubSkill:
        coordinate = f"@{namespace}/{public_slug}" if namespace else public_slug
        params: dict[str, str] = {"coordinate": coordinate}
        if version:
            params["version"] = version
        try:
            payload = await self._json("/api/v1/skills/resolve", params=params)
        except SkillHubError as exc:
            if exc.code != "skillhub_not_found":
                raise
            payload = await self._json(f"/api/v1/skills/{public_slug}")
        return _skill(_data(payload))

    async def download_and_verify(self, *, skill: SkillHubSkill) -> SkillHubVerifiedPackage:
        content = await self._download(skill=skill)
        package_sha256 = hashlib.sha256(content).hexdigest()
        content_hash = _content_hash(content, self.config)
        signature = await self._json(
            f"/api/v1/open/skills/{skill.public_slug}/versions/{skill.version}/signature"
        )
        verified_hash, key_id = await self._verify_signature(signature, content_hash)
        return SkillHubVerifiedPackage(
            content=content,
            package_sha256=package_sha256,
            signature_key_id=key_id,
            verified_content_sha256=verified_hash,
        )

    async def _download(self, *, skill: SkillHubSkill) -> bytes:
        params = {"slug": skill.public_slug, "version": skill.version}
        try:
            response = await self._request("GET", "/api/v1/download", params=params)
        except SkillHubError:
            raise
        if response.is_redirect:
            location = response.headers.get("location")
            parsed = urlparse(location or "")
            if (
                parsed.scheme != "https"
                or not parsed.hostname
                or parsed.username
                or parsed.password
                or not parsed.hostname.endswith(".cos.accelerate.myqcloud.com")
            ):
                raise SkillHubError("skillhub_download_redirect_rejected")
            try:
                async with httpx.AsyncClient(
                    timeout=self.config.SKILLHUB_REQUEST_TIMEOUT_SECONDS,
                    follow_redirects=False,
                    trust_env=False,
                    transport=self.transport,
                ) as client:
                    response = await client.get(location, headers={"Accept": "application/zip"})
            except httpx.HTTPError as exc:
                raise SkillHubError("skillhub_network_error") from exc
            if response.is_error or response.is_redirect:
                raise SkillHubError("skillhub_download_failed")
        content = response.content
        if (
            not content
            or len(content) > self.config.SKILLHUB_MAX_PACKAGE_BYTES
            or not content.startswith(b"PK")
        ):
            raise SkillHubError(
                "skillhub_package_too_large" if content else "skillhub_package_empty"
            )
        return content

    async def _verify_signature(
        self, raw_signature: dict[str, Any], content_hash: str
    ) -> tuple[str | None, str | None]:
        signature = _data(raw_signature)
        expected_hash = _string(
            signature.get("sha256")
            or signature.get("content_sha256")
            or signature.get("contentSha256")
            or signature.get("content_hash")
        )
        if expected_hash and expected_hash.casefold() != content_hash:
            raise SkillHubError("skillhub_hash_mismatch")
        key_id = _string(signature.get("key_id") or signature.get("keyId"))
        encoded_signature = _string(signature.get("signature"))
        payload = _signature_payload(signature)
        if not encoded_signature or not payload:
            if self.config.SKILLHUB_SIGNATURE_REQUIRED:
                raise SkillHubError("skillhub_signature_missing")
            return expected_hash, key_id
        keys_document = _data(await self._json("/api/v1/open/platform/keys"))
        public_key = _find_public_key(keys_document, key_id)
        if public_key is None:
            raise SkillHubError("skillhub_signature_key_missing")
        try:
            Ed25519PublicKey.from_public_bytes(_decode(public_key)).verify(
                _decode(encoded_signature), payload
            )
        except (InvalidSignature, ValueError) as exc:
            raise SkillHubError("skillhub_signature_invalid") from exc
        return expected_hash or content_hash, key_id

    async def _json(
        self, path: str, *, params: dict[str, str | int] | None = None
    ) -> dict[str, Any]:
        response = await self._request("GET", path, params=params)
        try:
            payload = response.json()
        except ValueError as exc:
            raise SkillHubError(
                "skillhub_protocol_invalid", "provider returned non-JSON response"
            ) from exc
        if not isinstance(payload, dict):
            raise SkillHubError(
                "skillhub_protocol_invalid", "provider returned an invalid JSON object"
            )
        return payload

    async def _request(
        self, method: str, path: str, *, params: dict[str, str | int] | None = None
    ) -> httpx.Response:
        url = httpx.URL(f"{self.base_url}{path}")
        headers = {"Accept": "application/json, application/zip"}
        extensions: dict[str, Any] = {}
        if self.proxy_url and self.proxy_connect_ip:
            url = url.copy_with(host=self.proxy_connect_ip)
            headers["Host"] = self.api_host_header
            extensions["sni_hostname"] = self.api_hostname
        client_options: dict[str, Any] = {
            "timeout": self.config.SKILLHUB_REQUEST_TIMEOUT_SECONDS,
            "follow_redirects": False,
            "transport": self.transport,
        }
        if self.proxy_url and self.transport is None:
            client_options["proxy"] = self.proxy_url
        try:
            async with httpx.AsyncClient(trust_env=False, **client_options) as client:
                response = await client.request(
                    method,
                    url,
                    params=params,
                    headers=headers,
                    extensions=extensions,
                )
        except httpx.HTTPError as exc:
            raise SkillHubError("skillhub_network_error") from exc
        if response.status_code == 404:
            raise SkillHubError("skillhub_not_found")
        if response.is_redirect:
            return response
        if response.is_error:
            raise SkillHubError(
                "skillhub_request_failed", f"SkillHub returned {response.status_code}"
            )
        return response


def _data(payload: dict[str, Any]) -> dict[str, Any]:
    data = payload.get("data", payload)
    return data if isinstance(data, dict) else payload


def _string(value: Any) -> str | None:
    return value.strip() if isinstance(value, str) and value.strip() else None


def _integer(value: Any) -> int | None:
    if isinstance(value, bool):
        return None
    try:
        return int(value) if value is not None else None
    except (TypeError, ValueError):
        return None


def _skill(raw: dict[str, Any]) -> SkillHubSkill:
    detail = raw.get("skill") if isinstance(raw.get("skill"), dict) else raw
    latest = raw.get("latestVersion") if isinstance(raw.get("latestVersion"), dict) else {}
    namespace_value = raw.get("namespace") or detail.get("namespace")
    coordinate = _string(raw.get("coordinate"))
    namespace = _string(namespace_value if isinstance(namespace_value, str) else None)
    if isinstance(namespace_value, dict):
        namespace = _string(
            namespace_value.get("handle")
            or namespace_value.get("namespace")
            or namespace_value.get("displayName")
        )
    namespace = namespace or _string(raw.get("owner") or raw.get("publisher"))
    slug = _string(
        raw.get("public_slug") or raw.get("publicSlug") or raw.get("slug") or detail.get("slug")
    )
    if coordinate and coordinate.startswith("@") and "/" in coordinate:
        namespace, slug = coordinate[1:].split("/", 1)
    version = _string(raw.get("version") or raw.get("latest_version")) or _string(
        latest.get("version")
    )
    if not namespace or not slug or not version:
        raise SkillHubError(
            "skillhub_protocol_invalid", "provider skill is missing coordinate or version"
        )
    metrics = raw.get("metrics") if isinstance(raw.get("metrics"), dict) else {}
    stats = detail.get("stats") if isinstance(detail.get("stats"), dict) else {}
    source = _string(raw.get("source") or detail.get("source"))
    # The open search/resolve endpoints only return community-public assets.
    # They currently expose no separate paid/private flags, so recognise only
    # their documented community origins and fail closed for all unknown ones.
    inferred_community = (
        source.casefold() in {"clawhub", "github", "skillhub", "community"} if source else False
    )
    return SkillHubSkill(
        namespace=namespace,
        public_slug=slug,
        version=version,
        name=_string(
            raw.get("name")
            or raw.get("title")
            or raw.get("displayName")
            or detail.get("displayName")
        )
        or slug,
        description=_string(raw.get("description") or raw.get("summary") or detail.get("summary")),
        category=_string(
            raw.get("category")
            or raw.get("category_slug")
            or raw.get("categorySlug")
            or detail.get("category")
        ),
        downloads=_integer(
            raw.get("downloads") or metrics.get("downloads") or stats.get("downloads")
        ),
        installs=_integer(
            raw.get("installs")
            or raw.get("install_count")
            or metrics.get("installs")
            or stats.get("installs")
        ),
        visibility=_string(raw.get("visibility") or raw.get("access"))
        or ("community" if inferred_community else None),
        pricing=_string(raw.get("pricing") or raw.get("price_type") or raw.get("priceType"))
        or ("free" if inferred_community else None),
        upstream_internal_slug=_string(
            raw.get("id") or raw.get("internal_slug") or raw.get("internalSlug") or detail.get("id")
        ),
    )


def _decode(value: str) -> bytes:
    padded = value + "=" * (-len(value) % 4)
    try:
        return base64.b64decode(padded, validate=True)
    except ValueError:
        return bytes.fromhex(value)


def _signature_payload(signature: dict[str, Any]) -> bytes:
    payload = (
        signature.get("payload")
        or signature.get("signed_payload")
        or signature.get("signedPayload")
    )
    if isinstance(payload, str):
        return payload.encode("utf-8")
    if isinstance(payload, dict):
        return json.dumps(
            payload, ensure_ascii=False, sort_keys=True, separators=(",", ":")
        ).encode("utf-8")
    return b""


def _find_public_key(document: dict[str, Any], key_id: str | None) -> str | None:
    candidates = document.get("keys", document.get("items", []))
    if isinstance(candidates, dict):
        candidates = [
            dict(value, key_id=key)
            if isinstance(value, dict)
            else {"key_id": key, "public_key": value}
            for key, value in candidates.items()
        ]
    if not isinstance(candidates, list):
        return None
    for item in candidates:
        if not isinstance(item, dict):
            continue
        candidate_id = _string(item.get("key_id") or item.get("keyId") or item.get("id"))
        if key_id is None or candidate_id == key_id:
            return _string(
                item.get("public_key")
                or item.get("publicKey")
                or item.get("public_key_raw_b64")
                or item.get("key")
            )
    return None


def _content_hash(content: bytes, config: Settings) -> str:
    """Implement SkillHub's signed, order-independent ZIP content hash."""
    try:
        with ZipFile(BytesIO(content)) as archive:
            entries = [
                entry
                for entry in archive.infolist()
                if not entry.is_dir() and not _ignored_content_path(entry.filename)
            ]
            if len(entries) > config.SKILLHUB_MAX_FILE_COUNT:
                raise SkillHubError("skillhub_package_too_many_files")
            total = 0
            lines: list[str] = []
            for entry in entries:
                if entry.file_size > config.SKILLHUB_MAX_SINGLE_FILE_BYTES:
                    raise SkillHubError("skillhub_package_too_large")
                total += entry.file_size
                if total > config.SKILLHUB_MAX_UNPACKED_BYTES:
                    raise SkillHubError("skillhub_package_too_large")
                path = entry.filename.replace("\\", "/")
                lines.append(f"{path}:{hashlib.sha256(archive.read(entry)).hexdigest()}")
    except SkillHubError:
        raise
    except BadZipFile as exc:
        raise SkillHubError("skillhub_package_invalid") from exc
    return hashlib.sha256(("\n".join(sorted(lines)) + "\n").encode("utf-8")).hexdigest()


def _ignored_content_path(path: str) -> bool:
    normalized = path.replace("\\", "/")
    name = normalized.rsplit("/", 1)[-1]
    return (
        normalized == "_meta.json"
        or normalized.startswith("__MACOSX/")
        or name in {".DS_Store", "Thumbs.db"}
        or name.startswith("._")
    )
