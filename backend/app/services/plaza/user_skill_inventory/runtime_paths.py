from __future__ import annotations

from pathlib import Path


def runtime_skills_root(binding) -> Path:
    """Return the backend-visible host path for one Runtime's Skill tree.

    ``hermes_home_mount_path`` (for example ``/opt/data``) is the path inside
    the Hermes container and must not be used by the platform worker when it
    copies files.  The backend shares the Runtime's local persistent volume
    through ``runtime_root_host_path``; the Hermes home is its configured
    ``hermes_home_sub_path`` below that root.

    The mount-path fallback keeps contract/unit fixtures that predate the
    host-storage fields usable, while real bindings always take the host-path
    branch.
    """
    host_root = str(getattr(binding, "runtime_root_host_path", "") or "").strip()
    home_sub_path = str(getattr(binding, "hermes_home_sub_path", "") or "").strip()
    if host_root and home_sub_path:
        root = Path(host_root).resolve(strict=False)
        home = (root / home_sub_path).resolve(strict=False)
        if not home.is_relative_to(root):
            raise ValueError("Runtime Hermes home escapes the persistent root")
        return home / "skills"
    return Path(str(binding.hermes_home_mount_path)) / "skills"
