import json

from app.services.runtime_image_publisher import (
    RuntimeImageCommandResult,
    RuntimeImagePublisher,
)


IMAGE = "kind.local/hermes-agent:code-20260820-deadbeef"
MANIFEST = "sha256:" + "a" * 64
CONFIG = "sha256:" + "b" * 64


def test_kind_publisher_closes_containerd_manifest_and_config_identity() -> None:
    commands: list[list[str]] = []

    class Runner:
        def run(self, command: list[str]) -> RuntimeImageCommandResult:
            commands.append(command)
            if "inspect" in command:
                stdout = json.dumps(
                    {
                        "target": {
                            "mediaType": "application/vnd.oci.image.manifest.v1+json",
                            "digest": MANIFEST,
                        }
                    }
                )
            elif "content" in command:
                stdout = json.dumps({"config": {"digest": CONFIG}})
            else:
                stdout = ""
            return RuntimeImageCommandResult(stdout=stdout, stderr="", returncode=0)

    identity = RuntimeImagePublisher(
        Runner(),
        publisher="kind",
        kind_cluster_name="hermes",
    ).publish(image_reference=IMAGE, local_config_id=CONFIG)

    assert identity.reference == IMAGE
    assert identity.target_digest == MANIFEST
    assert identity.platform_manifest_digest == MANIFEST
    assert identity.config_id == CONFIG
    assert commands[0] == [
        "kind",
        "load",
        "docker-image",
        IMAGE,
        "--name",
        "hermes",
    ]


def test_containerd_tree_output_uses_exact_name_list_fallback() -> None:
    class Runner:
        def run(self, command: list[str]) -> RuntimeImageCommandResult:
            if command[-3:-1] == ["images", "inspect"]:
                return RuntimeImageCommandResult(stdout="Name: tree", stderr="", returncode=0)
            if command[-3:-1] == ["images", "list"]:
                return RuntimeImageCommandResult(
                    stdout=(
                        "REF TYPE DIGEST SIZE PLATFORMS LABELS\n"
                        f"{IMAGE} application/vnd.oci.image.manifest.v1+json "
                        f"{MANIFEST} 1MiB linux/amd64 -\n"
                    ),
                    stderr="",
                    returncode=0,
                )
            if command[-3:-1] == ["content", "get"]:
                return RuntimeImageCommandResult(
                    stdout=json.dumps({"config": {"digest": CONFIG}}),
                    stderr="",
                    returncode=0,
                )
            return RuntimeImageCommandResult(stdout="", stderr="", returncode=0)

    identity = RuntimeImagePublisher(Runner(), publisher="containerd").publish(
        image_reference=IMAGE,
        local_config_id=CONFIG,
        docker_archive_path="/tmp/runtime-image.tar",
    )

    assert identity.platform_manifest_digest == MANIFEST
    assert identity.config_id == CONFIG


def test_published_inspection_does_not_import_image() -> None:
    commands: list[list[str]] = []

    class Runner:
        def run(self, command: list[str]) -> RuntimeImageCommandResult:
            commands.append(command)
            if command[-3:-1] == ["images", "inspect"]:
                return RuntimeImageCommandResult(
                    stdout=json.dumps(
                        {
                            "target": {
                                "mediaType": "application/vnd.oci.image.manifest.v1+json",
                                "digest": MANIFEST,
                            }
                        }
                    ),
                    stderr="",
                    returncode=0,
                )
            return RuntimeImageCommandResult(
                stdout=json.dumps({"config": {"digest": CONFIG}}),
                stderr="",
                returncode=0,
            )

    identity = RuntimeImagePublisher(
        Runner(), publisher="kind", kind_cluster_name="hermes"
    ).inspect_published(image_reference=IMAGE, local_config_id=CONFIG)

    assert identity.platform_manifest_digest == MANIFEST
    assert all("load" not in command for command in commands)
