from __future__ import annotations

from dataclasses import dataclass
import json
import platform
import re
from typing import Protocol


_SHA256 = re.compile(r"^sha256:[a-f0-9]{64}$")
_INDEX_MEDIA_TYPES = {
    "application/vnd.oci.image.index.v1+json",
    "application/vnd.docker.distribution.manifest.list.v2+json",
}
_MANIFEST_MEDIA_TYPES = {
    "application/vnd.oci.image.manifest.v1+json",
    "application/vnd.docker.distribution.manifest.v2+json",
}


class RuntimeImagePublishError(RuntimeError):
    pass


@dataclass(frozen=True)
class RuntimeImageCommandResult:
    stdout: str
    stderr: str
    returncode: int


class RuntimeImageCommandRunner(Protocol):
    def run(self, command: list[str]) -> RuntimeImageCommandResult: ...


@dataclass(frozen=True)
class PublishedRuntimeImageIdentity:
    reference: str
    target_digest: str
    platform_manifest_digest: str
    config_id: str
    publisher: str


class RuntimeImagePublisher:
    """Publish one local image and close its Kubernetes-visible identity."""

    def __init__(
        self,
        runner: RuntimeImageCommandRunner,
        *,
        publisher: str,
        kind_cluster_name: str = "hermes",
    ) -> None:
        if publisher not in {"kind", "containerd"}:
            raise ValueError("publisher must be kind or containerd")
        self.runner = runner
        self.publisher = publisher
        self.kind_cluster_name = kind_cluster_name

    def publish(
        self,
        *,
        image_reference: str,
        local_config_id: str,
        docker_archive_path: str | None = None,
    ) -> PublishedRuntimeImageIdentity:
        self._digest(local_config_id, "local image config ID")
        if self.publisher == "kind":
            self._checked(
                [
                    "kind",
                    "load",
                    "docker-image",
                    image_reference,
                    "--name",
                    self.kind_cluster_name,
                ]
            )
        else:
            if not docker_archive_path:
                raise ValueError("containerd publisher requires a Docker archive path")
            # A retry may reach this point after docker save/import already
            # completed. Reuse the exact image instead of exporting another
            # multi-gigabyte archive.
            try:
                return self.inspect_published(
                    image_reference=image_reference,
                    local_config_id=local_config_id,
                )
            except RuntimeImagePublishError:
                pass
            self._checked(["docker", "save", "-o", docker_archive_path, image_reference])
            self._checked(["ctr", "-n", "k8s.io", "images", "import", docker_archive_path])
        return self.inspect_published(
            image_reference=image_reference,
            local_config_id=local_config_id,
        )

    def inspect_published(
        self,
        *,
        image_reference: str,
        local_config_id: str | None = None,
    ) -> PublishedRuntimeImageIdentity:
        """Read the already-published containerd identity without importing anything."""
        prefix = (
            [
                "docker",
                "exec",
                f"{self.kind_cluster_name}-control-plane",
                "ctr",
                "-n",
                "k8s.io",
            ]
            if self.publisher == "kind"
            else ["ctr", "-n", "k8s.io"]
        )
        target = self._target(prefix, image_reference)
        target_digest = self._digest(str(target.get("digest") or ""), "target digest")
        media_type = str(target.get("mediaType") or "")
        target_document = self._content(prefix, target_digest)
        if media_type in _INDEX_MEDIA_TYPES:
            architecture = {"x86_64": "amd64", "aarch64": "arm64"}.get(
                platform.machine().lower(),
                platform.machine().lower(),
            )
            manifests = [
                item
                for item in target_document.get("manifests") or []
                if isinstance(item, dict)
                and isinstance(item.get("platform"), dict)
                and item["platform"].get("os") == "linux"
                and item["platform"].get("architecture") == architecture
            ]
            if len(manifests) != 1:
                raise RuntimeImagePublishError(
                    "published image does not have one current-platform manifest"
                )
            platform_manifest_digest = self._digest(
                str(manifests[0].get("digest") or ""),
                "platform manifest digest",
            )
            manifest = self._content(prefix, platform_manifest_digest)
        elif media_type in _MANIFEST_MEDIA_TYPES:
            platform_manifest_digest = target_digest
            manifest = target_document
        else:
            raise RuntimeImagePublishError("published image media type is unsupported")

        config = manifest.get("config")
        config_id = self._digest(
            str(config.get("digest") or "") if isinstance(config, dict) else "",
            "published config ID",
        )
        if local_config_id is not None:
            self._digest(local_config_id, "local image config ID")
        if local_config_id is not None and local_config_id not in {
            target_digest,
            platform_manifest_digest,
            config_id,
        }:
            raise RuntimeImagePublishError(
                "published image identity does not close over the local build"
            )
        return PublishedRuntimeImageIdentity(
            reference=image_reference,
            target_digest=target_digest,
            platform_manifest_digest=platform_manifest_digest,
            config_id=config_id,
            publisher=self.publisher,
        )

    def _target(self, prefix: list[str], image_reference: str) -> dict[str, object]:
        inspected = self._checked([*prefix, "images", "inspect", image_reference])
        try:
            document = json.loads(inspected.stdout)
        except json.JSONDecodeError:
            listed = self._checked([*prefix, "images", "list", f"name=={image_reference}"])
            rows = [
                line.split()
                for line in listed.stdout.splitlines()
                if line.split() and line.split()[0] == image_reference
            ]
            if len(rows) != 1 or len(rows[0]) < 3:
                raise RuntimeImagePublishError(
                    "containerd did not return one exact published image"
                )
            return {"mediaType": rows[0][1], "digest": rows[0][2]}
        target = document.get("target") if isinstance(document, dict) else None
        if not isinstance(target, dict):
            raise RuntimeImagePublishError("published image target is unavailable")
        return target

    def _content(self, prefix: list[str], digest: str) -> dict[str, object]:
        result = self._checked([*prefix, "content", "get", digest])
        try:
            document = json.loads(result.stdout)
        except json.JSONDecodeError as exc:
            raise RuntimeImagePublishError("published image content is invalid JSON") from exc
        if not isinstance(document, dict):
            raise RuntimeImagePublishError("published image content is not an object")
        return document

    def _checked(self, command: list[str]) -> RuntimeImageCommandResult:
        result = self.runner.run(command)
        if result.returncode != 0:
            message = result.stderr.strip() or f"exit status {result.returncode}"
            raise RuntimeImagePublishError(f"{' '.join(command)} failed: {message}")
        return result

    @staticmethod
    def _digest(value: str, label: str) -> str:
        if _SHA256.fullmatch(value) is None:
            raise RuntimeImagePublishError(f"{label} is not a sha256 identity")
        return value


__all__ = [
    "PublishedRuntimeImageIdentity",
    "RuntimeImageCommandResult",
    "RuntimeImagePublishError",
    "RuntimeImagePublisher",
]
