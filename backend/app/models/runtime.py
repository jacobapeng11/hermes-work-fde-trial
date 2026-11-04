from __future__ import annotations

from datetime import datetime

from sqlalchemy import BigInteger, Boolean, DateTime, ForeignKey, Index, Integer, JSON, String, Text, UniqueConstraint, func
from sqlalchemy.orm import Mapped, mapped_column

from app.models.base import Base
from app.models.mixins import TimestampMixin


class KnowledgeContextEpoch(Base):
    """Authoritative organization version for prepared knowledge context."""

    __tablename__ = "knowledge_context_epoch"

    organization_id: Mapped[int] = mapped_column(
        BigInteger,
        ForeignKey("organization.id"),
        primary_key=True,
    )
    generation: Mapped[int] = mapped_column(
        BigInteger,
        nullable=False,
        default=1,
        server_default="1",
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime,
        nullable=False,
        server_default=func.now(),
        onupdate=func.now(),
    )


class HermesRuntimeBinding(TimestampMixin, Base):
    __tablename__ = "hermes_runtime_binding"
    __table_args__ = (
        UniqueConstraint("runtime_workspace_id", name="uq_runtime_binding_workspace"),
        UniqueConstraint("runtime_id", name="uq_runtime_binding_runtime_id"),
        Index("ix_runtime_binding_status", "org_id", "status"),
        Index("ix_runtime_binding_owner", "org_id", "user_id"),
    )

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)
    org_id: Mapped[int] = mapped_column(BigInteger, ForeignKey("organization.id"), nullable=False, index=True)
    user_id: Mapped[int] = mapped_column(BigInteger, ForeignKey("user.id"), nullable=False, index=True)
    runtime_workspace_id: Mapped[int] = mapped_column(
        BigInteger,
        ForeignKey("cloud_hermes_profile.id"),
        nullable=False,
        index=True,
    )
    provider: Mapped[str] = mapped_column(String(32), nullable=False, default="knative")
    runtime_id: Mapped[str] = mapped_column(String(128), nullable=False)
    config_generation: Mapped[int] = mapped_column(Integer, nullable=False, default=1)
    config_hash: Mapped[str | None] = mapped_column(String(128), nullable=True)
    # PVC is the canonical config source; ConfigMap remains a compatibility mode.
    runtime_config_source: Mapped[str] = mapped_column(String(16), nullable=False, default="pvc")

    image: Mapped[str] = mapped_column(String(500), nullable=False)
    image_digest: Mapped[str | None] = mapped_column(String(255), nullable=True)
    k8s_namespace: Mapped[str] = mapped_column(String(128), nullable=False)
    knative_service: Mapped[str] = mapped_column(String(128), nullable=False, index=True)
    knative_status_url: Mapped[str | None] = mapped_column(String(500), nullable=True)

    route_host: Mapped[str | None] = mapped_column(String(255), nullable=True)
    endpoint_base_url: Mapped[str | None] = mapped_column(String(500), nullable=True)
    endpoint_host_header: Mapped[str | None] = mapped_column(String(255), nullable=True)

    runtime_root_host_path: Mapped[str | None] = mapped_column(String(500), nullable=True)
    runtime_root_pvc: Mapped[str | None] = mapped_column(String(128), nullable=True)
    runtime_root_pvc_uid: Mapped[str | None] = mapped_column(String(128), nullable=True)
    pvc_storage_identity_hash: Mapped[str | None] = mapped_column(String(71), nullable=True)
    hermes_home_sub_path: Mapped[str] = mapped_column(String(500), nullable=False)
    workspace_sub_path: Mapped[str] = mapped_column(String(500), nullable=False)
    hermes_home_volume_name: Mapped[str | None] = mapped_column(String(128), nullable=True)
    workspace_volume_name: Mapped[str | None] = mapped_column(String(128), nullable=True)
    hermes_home_mount_path: Mapped[str] = mapped_column(String(255), nullable=False)
    workspace_mount_path: Mapped[str] = mapped_column(String(255), nullable=False)

    api_key_encrypted: Mapped[str] = mapped_column(String(1000), nullable=False)
    status: Mapped[str] = mapped_column(String(32), nullable=False, default="starting", index=True)
    last_user_intent_at: Mapped[datetime | None] = mapped_column(nullable=True)
    last_activity_at: Mapped[datetime | None] = mapped_column(nullable=True)
    last_llm_call_started_at: Mapped[datetime | None] = mapped_column(nullable=True)
    last_llm_call_finished_at: Mapped[datetime | None] = mapped_column(nullable=True)
    llm_in_flight_count: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    active_run_count: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    pending_reload: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    pending_reload_scopes: Mapped[list | None] = mapped_column(JSON, nullable=True)
    mcp_operation_blocked: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    mcp_operation_block_reason: Mapped[str | None] = mapped_column(Text, nullable=True)
    last_error: Mapped[str | None] = mapped_column(Text, nullable=True)


class PersonalConfigBundleArtifact(TimestampMixin, Base):
    """个人 Runtime exact-generation ConfigBundle 的不可变控制面工件。

    同一 (runtime_workspace_id, config_generation) 的 files 与 digest 固定不变；
    身份等输入变化必须通过推进 Binding generation 产生新工件，禁止改写历史 revision。
    """

    __tablename__ = "personal_config_bundle_artifact"
    __table_args__ = (
        UniqueConstraint(
            "runtime_workspace_id",
            "config_generation",
            name="uq_pcb_artifact_ws_generation",
        ),
        Index(
            "ix_pcb_artifact_ws_generation",
            "runtime_workspace_id",
            "config_generation",
        ),
    )

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)
    org_id: Mapped[int] = mapped_column(BigInteger, nullable=False, index=True)
    runtime_workspace_id: Mapped[int] = mapped_column(
        BigInteger,
        ForeignKey("cloud_hermes_profile.id"),
        nullable=False,
    )
    config_generation: Mapped[int] = mapped_column(Integer, nullable=False)
    config_revision: Mapped[str] = mapped_column(String(64), nullable=False)
    config_digest: Mapped[str] = mapped_column(String(128), nullable=False)
    files: Mapped[dict] = mapped_column(JSON, nullable=False)


class RuntimeKanbanLease(TimestampMixin, Base):
    """Durable instruction to keep one Runtime awake while Kanban has work.

    Hermes' native board remains the task source of truth.  This row only
    preserves the control-plane keepalive/reconciliation intent across Backend
    restarts; it never stores a copied task status.
    """

    __tablename__ = "runtime_kanban_lease"
    __table_args__ = (
        UniqueConstraint("runtime_workspace_id", name="uq_runtime_kanban_lease_workspace"),
        Index("ix_runtime_kanban_lease_due", "next_reconcile_at"),
    )

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)
    org_id: Mapped[int] = mapped_column(
        BigInteger, ForeignKey("organization.id"), nullable=False, index=True
    )
    user_id: Mapped[int] = mapped_column(
        BigInteger, ForeignKey("user.id"), nullable=False, index=True
    )
    runtime_workspace_id: Mapped[int] = mapped_column(
        BigInteger,
        ForeignKey("cloud_hermes_profile.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    state: Mapped[str] = mapped_column(String(24), nullable=False, default="temporary")
    temporary_expires_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    release_after: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    next_reconcile_at: Mapped[datetime] = mapped_column(DateTime, nullable=False)
    last_reconciled_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    last_error: Mapped[str | None] = mapped_column(String(200), nullable=True)


class RuntimeKanbanLeaseEvent(TimestampMixin, Base):
    """Idempotent Runtime Plugin receipt; not a mirror of a Kanban task."""

    __tablename__ = "runtime_kanban_lease_event"
    __table_args__ = (
        UniqueConstraint(
            "runtime_workspace_id",
            "event_id",
            name="uq_runtime_kanban_lease_event_identity",
        ),
        Index(
            "ix_runtime_kanban_lease_event_workspace_created",
            "runtime_workspace_id",
            "created_at",
        ),
    )

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)
    org_id: Mapped[int] = mapped_column(
        BigInteger, ForeignKey("organization.id"), nullable=False, index=True
    )
    user_id: Mapped[int] = mapped_column(
        BigInteger, ForeignKey("user.id"), nullable=False, index=True
    )
    runtime_workspace_id: Mapped[int] = mapped_column(
        BigInteger,
        ForeignKey("cloud_hermes_profile.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    runtime_agent_profile_id: Mapped[int | None] = mapped_column(
        BigInteger,
        ForeignKey("runtime_agent_profile.id", ondelete="SET NULL"),
        nullable=True,
        index=True,
    )
    event_id: Mapped[str] = mapped_column(String(240), nullable=False)
    event_name: Mapped[str] = mapped_column(String(40), nullable=False)
    tool_name: Mapped[str | None] = mapped_column(String(80), nullable=True)
    task_id: Mapped[str | None] = mapped_column(String(100), nullable=True)
    board: Mapped[str | None] = mapped_column(String(100), nullable=True)
    outcome: Mapped[str | None] = mapped_column(String(24), nullable=True)
    runtime_session_id: Mapped[str | None] = mapped_column(String(200), nullable=True)
    tool_call_id: Mapped[str | None] = mapped_column(String(200), nullable=True)
    turn_id: Mapped[str | None] = mapped_column(String(200), nullable=True)
    assignee: Mapped[str | None] = mapped_column(String(128), nullable=True)
    profile_name: Mapped[str | None] = mapped_column(String(128), nullable=True)


class ExpertKanbanCollaboration(TimestampMixin, Base):
    """Durable delivery state for one Web chat turn that created Kanban work.

    Hermes' native Board remains authoritative for every task status. This row
    only correlates the originating chat Run with the later summary Run.
    """

    __tablename__ = "expert_kanban_collaboration"
    __table_args__ = (
        UniqueConstraint(
            "origin_run_id",
            name="uq_expert_kanban_collaboration_origin_run",
        ),
        UniqueConstraint(
            "public_id",
            name="uq_expert_kanban_collaboration_public_id",
        ),
        Index(
            "ix_expert_kanban_collaboration_owner_state",
            "org_id",
            "user_id",
            "state",
            "updated_at",
        ),
        Index(
            "ix_expert_kanban_collaboration_due",
            "state",
            "next_reconcile_at",
        ),
    )

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)
    public_id: Mapped[str] = mapped_column(String(64), nullable=False)
    org_id: Mapped[int] = mapped_column(
        BigInteger,
        ForeignKey("organization.id"),
        nullable=False,
        index=True,
    )
    user_id: Mapped[int] = mapped_column(
        BigInteger,
        ForeignKey("user.id"),
        nullable=False,
        index=True,
    )
    runtime_workspace_id: Mapped[int] = mapped_column(
        BigInteger,
        ForeignKey("cloud_hermes_profile.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    session_id: Mapped[int] = mapped_column(
        BigInteger,
        ForeignKey("runtime_session.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    origin_run_id: Mapped[int] = mapped_column(
        BigInteger,
        ForeignKey("runtime_run.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    final_run_id: Mapped[int | None] = mapped_column(
        BigInteger,
        ForeignKey("runtime_run.id", ondelete="SET NULL"),
        nullable=True,
        index=True,
    )
    state: Mapped[str] = mapped_column(
        String(32),
        nullable=False,
        default="waiting_tasks",
        index=True,
    )
    revision: Mapped[int] = mapped_column(Integer, nullable=False, default=1)
    result_snapshot: Mapped[dict | None] = mapped_column(JSON, nullable=True)
    attempt_count: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    wait_attempt_count: Mapped[int] = mapped_column(
        Integer, nullable=False, default=0
    )
    next_reconcile_at: Mapped[datetime] = mapped_column(DateTime, nullable=False)
    lease_expires_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    last_error_code: Mapped[str | None] = mapped_column(String(100), nullable=True)
    last_error_message: Mapped[str | None] = mapped_column(Text, nullable=True)
    delivered_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)


class ExpertKanbanCollaborationTask(TimestampMixin, Base):
    """One originating top-level native Kanban task binding."""

    __tablename__ = "expert_kanban_collaboration_task"
    __table_args__ = (
        UniqueConstraint(
            "runtime_workspace_id",
            "board",
            "task_id",
            name="uq_expert_kanban_collaboration_native_task",
        ),
        Index(
            "ix_expert_kanban_collaboration_task_parent",
            "collaboration_id",
            "id",
        ),
    )

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)
    collaboration_id: Mapped[int] = mapped_column(
        BigInteger,
        ForeignKey("expert_kanban_collaboration.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    runtime_workspace_id: Mapped[int] = mapped_column(
        BigInteger,
        ForeignKey("cloud_hermes_profile.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    board: Mapped[str] = mapped_column(String(100), nullable=False, default="default")
    task_id: Mapped[str] = mapped_column(String(100), nullable=False)
    tool_call_id: Mapped[str | None] = mapped_column(String(200), nullable=True)


class RuntimeSession(TimestampMixin, Base):
    __tablename__ = "runtime_session"
    __table_args__ = (
        UniqueConstraint(
            "runtime_workspace_id",
            "runtime_session_id",
            name="uq_runtime_session_runtime_id",
        ),
        Index("ix_runtime_session_owner_active", "org_id", "user_id", "status", "last_active_at"),
    )

    id: Mapped[int] = mapped_column(
        BigInteger().with_variant(Integer, "sqlite"),
        primary_key=True,
        autoincrement=True,
    )
    org_id: Mapped[int] = mapped_column(BigInteger, ForeignKey("organization.id"), nullable=False, index=True)
    user_id: Mapped[int] = mapped_column(BigInteger, ForeignKey("user.id"), nullable=False, index=True)
    runtime_workspace_id: Mapped[int] = mapped_column(
        BigInteger,
        ForeignKey("cloud_hermes_profile.id"),
        nullable=False,
        index=True,
    )
    runtime_agent_profile_id: Mapped[int] = mapped_column(
        BigInteger,
        ForeignKey("runtime_agent_profile.id"),
        nullable=False,
        index=True,
    )
    runtime_session_id: Mapped[str] = mapped_column(String(128), nullable=False)
    context_source_runtime_session_id: Mapped[str | None] = mapped_column(
        String(128),
        nullable=True,
        index=True,
    )
    context_head_runtime_session_id: Mapped[str | None] = mapped_column(
        String(128),
        nullable=True,
    )
    source_type: Mapped[str] = mapped_column(String(32), nullable=False, default="web")
    title_cache: Mapped[str | None] = mapped_column(String(255), nullable=True)
    message_count_cache: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    hermes_materialized_at: Mapped[datetime | None] = mapped_column(nullable=True)
    last_active_at: Mapped[datetime | None] = mapped_column(nullable=True)
    last_synced_at: Mapped[datetime | None] = mapped_column(nullable=True)
    chat_log_backfilled_at: Mapped[datetime | None] = mapped_column(nullable=True)
    status: Mapped[str] = mapped_column(String(32), nullable=False, default="active", index=True)
    session_model_entry_id: Mapped[int | None] = mapped_column(
        BigInteger,
        ForeignKey("model_entry.id"),
        nullable=True,
        index=True,
    )
    model_switch_events: Mapped[list | None] = mapped_column(JSON, nullable=True)


class RuntimeRun(TimestampMixin, Base):
    __tablename__ = "runtime_run"
    __table_args__ = (
        UniqueConstraint("runtime_run_id", name="uq_runtime_run_runtime_id"),
        Index("ix_runtime_run_active", "org_id", "runtime_workspace_id", "status"),
        Index("ix_runtime_run_session_status_id", "session_id", "status", "id"),
    )

    id: Mapped[int] = mapped_column(
        BigInteger().with_variant(Integer, "sqlite"),
        primary_key=True,
        autoincrement=True,
    )
    org_id: Mapped[int] = mapped_column(BigInteger, ForeignKey("organization.id"), nullable=False, index=True)
    user_id: Mapped[int] = mapped_column(BigInteger, ForeignKey("user.id"), nullable=False, index=True)
    runtime_workspace_id: Mapped[int] = mapped_column(
        BigInteger,
        ForeignKey("cloud_hermes_profile.id"),
        nullable=False,
        index=True,
    )
    session_id: Mapped[int | None] = mapped_column(
        BigInteger,
        ForeignKey("runtime_session.id"),
        nullable=True,
        index=True,
    )
    runtime_session_id: Mapped[str | None] = mapped_column(String(128), nullable=True, index=True)
    runtime_run_id: Mapped[str] = mapped_column(String(128), nullable=False)
    runtime_provider_run_id: Mapped[str | None] = mapped_column(
        String(128),
        nullable=True,
    )
    model_entry_id: Mapped[int | None] = mapped_column(
        BigInteger,
        ForeignKey("model_entry.id"),
        nullable=True,
        index=True,
    )
    status: Mapped[str] = mapped_column(String(32), nullable=False, default="pending", index=True)
    source_type: Mapped[str] = mapped_column(String(32), nullable=False, default="web")
    request_payload: Mapped[dict | None] = mapped_column(JSON, nullable=True)
    response_summary: Mapped[dict | None] = mapped_column(JSON, nullable=True)
    approval_status: Mapped[str | None] = mapped_column(String(32), nullable=True)
    stop_requested_at: Mapped[datetime | None] = mapped_column(nullable=True)
    stopped_by_user_id: Mapped[int | None] = mapped_column(BigInteger, nullable=True)
    started_at: Mapped[datetime | None] = mapped_column(nullable=True)
    finished_at: Mapped[datetime | None] = mapped_column(nullable=True)
    # Kept separate from ``status``: a completed user run can authorize the
    # narrowly bounded Hermes self-improvement review without blocking a new
    # user turn or being counted as an active run.
    self_evolution_status: Mapped[str | None] = mapped_column(String(16), nullable=True, index=True)
    self_evolution_activation_deadline_at: Mapped[datetime | None] = mapped_column(nullable=True, index=True)
    self_evolution_started_at: Mapped[datetime | None] = mapped_column(nullable=True)
    self_evolution_last_activity_at: Mapped[datetime | None] = mapped_column(nullable=True)
    self_evolution_deadline_at: Mapped[datetime | None] = mapped_column(nullable=True)
    self_evolution_error_message: Mapped[str | None] = mapped_column(Text, nullable=True)
    chat_log_projected_at: Mapped[datetime | None] = mapped_column(nullable=True, index=True)
    error_code: Mapped[str | None] = mapped_column(String(100), nullable=True)
    error_message: Mapped[str | None] = mapped_column(Text, nullable=True)


class RuntimeClarifyRequest(TimestampMixin, Base):
    """A persistent clarify question that outlives the Runtime Pod."""

    __tablename__ = "runtime_clarify_request"
    __table_args__ = (
        UniqueConstraint(
            "runtime_workspace_id",
            "event_id",
            name="uq_runtime_clarify_request_event",
        ),
        Index(
            "ix_runtime_clarify_request_session_status",
            "runtime_session_id",
            "status",
            "id",
        ),
        Index(
            "ix_runtime_clarify_request_auto_default",
            "status",
            "auto_default_at",
        ),
        Index(
            "ix_runtime_clarify_request_retry",
            "status",
            "next_resume_at",
        ),
    )

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)
    org_id: Mapped[int] = mapped_column(BigInteger, ForeignKey("organization.id"), nullable=False, index=True)
    user_id: Mapped[int] = mapped_column(BigInteger, ForeignKey("user.id"), nullable=False, index=True)
    runtime_workspace_id: Mapped[int] = mapped_column(
        BigInteger, ForeignKey("cloud_hermes_profile.id"), nullable=False, index=True
    )
    session_id: Mapped[int] = mapped_column(
        BigInteger, ForeignKey("runtime_session.id", ondelete="CASCADE"), nullable=False, index=True
    )
    parent_run_id: Mapped[int] = mapped_column(
        BigInteger, ForeignKey("runtime_run.id", ondelete="CASCADE"), nullable=False, index=True
    )
    request_id: Mapped[str] = mapped_column(String(128), nullable=False, unique=True)
    event_id: Mapped[str] = mapped_column(String(240), nullable=False)
    runtime_session_id: Mapped[str] = mapped_column(String(128), nullable=False)
    tool_call_id: Mapped[str | None] = mapped_column(String(200), nullable=True)
    question: Mapped[str] = mapped_column(Text, nullable=False)
    choices: Mapped[list | None] = mapped_column(JSON, nullable=True)
    multi_select: Mapped[bool] = mapped_column(nullable=False, default=False)
    status: Mapped[str] = mapped_column(String(32), nullable=False, default="waiting", index=True)
    answer: Mapped[dict | None] = mapped_column(JSON, nullable=True)
    answer_source: Mapped[str | None] = mapped_column(String(16), nullable=True)
    auto_default_at: Mapped[datetime | None] = mapped_column(nullable=True, index=True)
    expires_at: Mapped[datetime | None] = mapped_column(nullable=True, index=True)
    answered_at: Mapped[datetime | None] = mapped_column(nullable=True)
    canceled_at: Mapped[datetime | None] = mapped_column(nullable=True)
    resume_run_id: Mapped[str | None] = mapped_column(String(128), nullable=True, index=True)
    claim_expires_at: Mapped[datetime | None] = mapped_column(nullable=True)
    resume_attempt_count: Mapped[int] = mapped_column(
        Integer,
        nullable=False,
        default=0,
        server_default="0",
    )
    next_resume_at: Mapped[datetime | None] = mapped_column(nullable=True, index=True)
    version: Mapped[int] = mapped_column(Integer, nullable=False, default=1)
    # ``web_run`` keeps the parked-parent + continuation contract. An
    # ``expert_worker`` request belongs to a native Kanban expert worker: it is
    # answered in place and never creates a continuation Run.
    mode: Mapped[str] = mapped_column(
        String(32), nullable=False, default="web_run", server_default="web_run"
    )
    collaboration_task_id: Mapped[int | None] = mapped_column(
        BigInteger,
        ForeignKey("expert_kanban_collaboration_task.id", ondelete="SET NULL"),
        nullable=True,
        index=True,
    )
    expert_profile_id: Mapped[int | None] = mapped_column(
        BigInteger, nullable=True, index=True
    )
    requested_by: Mapped[str | None] = mapped_column(String(128), nullable=True)
