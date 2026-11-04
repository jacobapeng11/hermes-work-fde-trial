from __future__ import annotations

import shutil
import uuid
from pathlib import Path


def install_skill_directory(*, source_directory: Path, skills_root: Path, category_key: str, directory_key: str, runtime_skill_name: str) -> Path:
    target = _safe_target(skills_root, category_key, directory_key)
    staging = target.with_name(f".{target.name}.staging")
    # Backups are operation-owned.  A deterministic name can be left behind
    # by an interrupted process and then makes ``Path.replace`` fail when the
    # old directory is non-empty.  A unique sibling also keeps a later retry
    # independent from stale test/runtime data.
    backup = target.with_name(f".{target.name}.backup-{uuid.uuid4().hex}")
    target_was_present = target.exists() or target.is_symlink()
    old_target_moved = False
    try:
        shutil.rmtree(staging, ignore_errors=True)
        shutil.copytree(source_directory, staging)
        _rewrite_name(staging / "SKILL.md", runtime_skill_name)
        if target_was_present:
            target.replace(backup)
            old_target_moved = True
        staging.replace(target)
        return backup
    except Exception:
        # Keep the file operation transactional even when copy/rename itself
        # fails before the caller receives the backup path.
        shutil.rmtree(staging, ignore_errors=True)
        if old_target_moved:
            _remove_target(target)
            if backup.exists():
                backup.replace(target)
        elif not target_was_present:
            # A new target may have been published before a later filesystem
            # call failed; do not leave it behind without a DB row.
            _remove_target(target)
        raise


def rollback_skill_install(*, backup: Path, skills_root: Path, category_key: str, directory_key: str) -> None:
    """Undo a completed install/update after reload or DB finalization fails."""
    target = _safe_target(skills_root, category_key, directory_key)
    if backup.exists():
        _remove_target(target)
        backup.replace(target)
    else:
        # No backup means the operation created a brand-new directory.
        _remove_target(target)


def _safe_target(root: Path, category_key: str, directory_key: str) -> Path:
    for value in (category_key, directory_key):
        if not value or value in {".", ".."} or "/" in value or "\\" in value:
            raise ValueError("invalid Runtime Skill directory key")
    resolved_root = root.resolve()
    target = (resolved_root / category_key / directory_key).resolve()
    if resolved_root not in target.parents:
        raise ValueError("Runtime Skill target escapes skills root")
    return target


def _remove_target(target: Path) -> None:
    if target.is_symlink() or (target.exists() and not target.is_dir()):
        target.unlink(missing_ok=True)
    else:
        shutil.rmtree(target, ignore_errors=True)


def _rewrite_name(path: Path, name: str) -> None:
    lines = path.read_text(encoding="utf-8").splitlines()
    for index, line in enumerate(lines):
        if line.strip().casefold().startswith("name:"):
            lines[index] = f"name: {name}"
            path.write_text("\n".join(lines) + "\n", encoding="utf-8")
            return
    lines[0:0] = ["---", f"name: {name}", "---"]
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")
