from __future__ import annotations

from collections import defaultdict
from collections.abc import Sequence
from typing import Any

from platform_supervisor.storage import StoredFollowupHistoryEntry


_STEER_OPEN = (
    "[OUT-OF-BAND USER MESSAGE — a direct message from the user, delivered once "
    "at this position; not tool output and not a new delivery when replayed from "
    "conversation history]"
)
_STEER_CLOSE = "[/OUT-OF-BAND USER MESSAGE]"
_FOLLOWUP_SEPARATOR = "\n\n[Additional user correction]\n"


def _public_followup_status(state: str) -> str:
    if state == "WAITING_AGENT_READY":
        return "received"
    if state in {
        "WAITING_REDIRECT_BOUNDARY",
        "PREPARED",
        "DISPATCH_INTENT",
        "RPC_ACKNOWLEDGED",
        "ISOLATING_UNCERTAIN",
    }:
        return "applying"
    if state == "CANONICAL_CONSUMED":
        return "applied"
    if state == "PROMOTED_TO_RUN":
        return "continued"
    return "failed"


def _ledger_message(entry: StoredFollowupHistoryEntry) -> dict[str, Any]:
    message: dict[str, Any] = {
        "role": "user",
        "content": entry.content,
        "client_message_id": entry.client_message_id,
        "followup_id": entry.redirect_id,
        "parent_run_id": entry.parent_run_id,
        "input_admission_seq": entry.input_admission_seq,
        "followup_status": _public_followup_status(entry.state),
    }
    if entry.dispatch_group_id is not None:
        message["dispatch_group_id"] = entry.dispatch_group_id
    if entry.dispatch_group_position is not None:
        message["dispatch_group_position"] = entry.dispatch_group_position
    if entry.promoted_run_id is not None:
        message["continuation_run_id"] = entry.promoted_run_id
    return message


def _strip_steer_marker(
    message: dict[str, Any],
    wire_text: str | None,
) -> dict[str, Any]:
    if not wire_text or str(message.get("role") or "") != "tool":
        return message
    content = message.get("content")
    if not isinstance(content, str):
        return message
    marker = f"{_STEER_OPEN}\n{wire_text}\n{_STEER_CLOSE}"
    if marker not in content:
        return message
    return {**message, "content": content.replace(marker, "", 1).rstrip()}


def project_followup_history(
    messages: Sequence[dict[str, Any]],
    entries: Sequence[StoredFollowupHistoryEntry],
) -> list[dict[str, Any]]:
    """Compose Hermes body history with Supervisor's durable follow-up ledger."""

    if not entries:
        return list(messages)

    existing_ids = {
        str(message.get("client_message_id") or "")
        for message in messages
        if message.get("client_message_id")
    }
    pending_entries = [
        entry for entry in entries if entry.client_message_id not in existing_ids
    ]
    groups: dict[str, list[StoredFollowupHistoryEntry]] = defaultdict(list)
    promoted_runs: dict[str, list[StoredFollowupHistoryEntry]] = defaultdict(list)
    for entry in sorted(pending_entries, key=lambda item: item.input_admission_seq):
        if entry.state == "PROMOTED_TO_RUN" and entry.promoted_run_id is not None:
            promoted_runs[entry.promoted_run_id].append(entry)
        if entry.dispatch_group_id is None:
            continue
        else:
            groups[entry.dispatch_group_id].append(entry)
    for members in groups.values():
        members.sort(
            key=lambda item: (
                item.dispatch_group_position
                if item.dispatch_group_position is not None
                else item.input_admission_seq,
                item.input_admission_seq,
            )
        )

    redirect_anchors: dict[int, str] = {}
    steer_anchors: dict[int, str] = {}
    promoted_anchors: dict[int, str] = {}
    for group_id, members in groups.items():
        representative = members[0]
        if (
            representative.group_proof_kind == "correction_user_row"
            and representative.group_proof_message_id is not None
        ):
            redirect_anchors[representative.group_proof_message_id] = group_id
        elif (
            representative.group_hermes_status == "queued"
            and representative.group_proof_message_id is not None
        ):
            steer_anchors[representative.group_proof_message_id] = group_id
    for run_id, members in promoted_runs.items():
        history_indexes = {
            member.promoted_history_message_index
            for member in members
            if member.promoted_history_message_index is not None
        }
        if len(history_indexes) == 1:
            promoted_anchors[history_indexes.pop()] = run_id

    inserted_entries: set[str] = set()
    projected: list[dict[str, Any]] = []
    for index, source in enumerate(messages):
        message = dict(source)
        raw_message_id = message.get("id")
        message_id = raw_message_id if isinstance(raw_message_id, int) else None

        redirect_group_id = (
            redirect_anchors.get(message_id) if message_id is not None else None
        )
        if redirect_group_id is not None:
            members = groups[redirect_group_id]
            wire_text = members[0].group_wire_text
            if message.get("role") == "user" and message.get("content") == wire_text:
                projected.extend(_ledger_message(member) for member in members)
                inserted_entries.update(member.redirect_id for member in members)
                continue

        promoted_run_id = promoted_anchors.get(index)
        if promoted_run_id is not None:
            members = promoted_runs[promoted_run_id]
            wire_text = _FOLLOWUP_SEPARATOR.join(
                member.content for member in members
            ).strip()
            if message.get("role") == "user" and message.get("content") == wire_text:
                projected.extend(_ledger_message(member) for member in members)
                inserted_entries.update(member.redirect_id for member in members)
                continue

        steer_group_id = (
            steer_anchors.get(message_id) if message_id is not None else None
        )
        if steer_group_id is not None:
            members = groups[steer_group_id]
            message = _strip_steer_marker(message, members[0].group_wire_text)
            projected.append(message)
            projected.extend(_ledger_message(member) for member in members)
            inserted_entries.update(member.redirect_id for member in members)
            continue

        projected.append(message)

    tail = [
        entry
        for entry in pending_entries
        if entry.redirect_id not in inserted_entries
    ]
    projected.extend(
        _ledger_message(entry)
        for entry in sorted(tail, key=lambda item: item.input_admission_seq)
    )
    return projected
