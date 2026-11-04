"""Shared error contract for expert command/query boundaries."""

from __future__ import annotations


class ExpertProfileError(RuntimeError):
    def __init__(self, code: str, *, status_code: int = 400) -> None:
        super().__init__(code)
        self.code = code
        self.status_code = status_code


def derived_expert_plugin_ids(
    *,
    knowledge_kinds: set[str],
    enterprise_knowledge_enabled: bool,
    submitted_plugin_ids: list[str],
) -> list[str]:
    required_plugins: set[str] = set()
    if "personal" in knowledge_kinds:
        required_plugins.add("personal-knowledge")
    if enterprise_knowledge_enabled:
        required_plugins.add("enterprise-knowledge")
    if submitted_plugin_ids and set(submitted_plugin_ids) != required_plugins:
        raise ExpertProfileError(
            "expert_plugin_selection_not_editable",
            status_code=403,
        )
    return sorted(required_plugins)


__all__ = ["ExpertProfileError", "derived_expert_plugin_ids"]
