#!/usr/bin/env python3
"""Verify the exact Chinese font payload used by the Office Runtime."""

from __future__ import annotations

import argparse
import hashlib
import json
from pathlib import Path
import re
import sys


class FontManifestError(RuntimeError):
    pass


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def verify(*, manifest_path: Path, font_dir: Path) -> dict[str, object]:
    try:
        payload = json.loads(manifest_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise FontManifestError("无法读取字体 manifest") from exc
    fonts = payload.get("fonts") if isinstance(payload, dict) else None
    if not isinstance(fonts, list) or len(fonts) != 6:
        raise FontManifestError("字体 manifest 必须精确声明 6 个文件")
    expected: dict[str, str] = {}
    for item in fonts:
        if not isinstance(item, dict):
            raise FontManifestError("字体条目必须是对象")
        filename = item.get("file")
        digest = item.get("sha256")
        if (
            not isinstance(filename, str)
            or Path(filename).name != filename
            or not isinstance(digest, str)
            or re.fullmatch(r"[0-9a-f]{64}", digest) is None
            or not isinstance(item.get("family"), str)
        ):
            raise FontManifestError("字体条目缺少规范文件名、摘要或 family")
        if filename in expected:
            raise FontManifestError(f"字体文件重复: {filename}")
        expected[filename] = digest
    actual = {
        path.name
        for path in font_dir.iterdir()
        if path.is_file() and path.suffix.lower() in {".otf", ".ttf"}
    }
    if actual != set(expected):
        raise FontManifestError(
            f"字体文件集合不一致: expected={sorted(expected)}; actual={sorted(actual)}"
        )
    for filename, expected_digest in sorted(expected.items()):
        actual_digest = _sha256(font_dir / filename)
        if actual_digest != expected_digest:
            raise FontManifestError(
                f"字体摘要不一致: {filename}; expected={expected_digest}; "
                f"actual={actual_digest}"
            )
    canonical = json.dumps(payload, sort_keys=True, separators=(",", ":")).encode()
    return {
        "status": "passed",
        "font_count": len(expected),
        "manifest_sha256": hashlib.sha256(canonical).hexdigest(),
        "files": sorted(expected),
    }


def _main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--manifest", required=True)
    parser.add_argument("--font-dir", required=True)
    args = parser.parse_args()
    report = verify(
        manifest_path=Path(args.manifest),
        font_dir=Path(args.font_dir),
    )
    json.dump(report, sys.stdout, ensure_ascii=False, sort_keys=True)
    sys.stdout.write("\n")
    return 0


if __name__ == "__main__":
    raise SystemExit(_main())
