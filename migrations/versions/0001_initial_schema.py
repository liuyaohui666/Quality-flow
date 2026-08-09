"""Create the initial QualityFlow persistence schema.

Revision ID: 0001_initial_schema
Revises:
"""

from alembic import op
import sqlalchemy as sa


revision = "0001_initial_schema"
down_revision = None
branch_labels = None
depends_on = None


run_status = sa.Enum(
    "queued", "running", "completed", "infra_failed", "timed_out",
    name="run_status", native_enum=False, create_constraint=False,
)
run_outcome = sa.Enum(
    "unknown", "passed", "failed",
    name="run_outcome", native_enum=False, create_constraint=False,
)
attempt_status = sa.Enum(
    "dispatched", "running", "passed", "test_failed", "infra_failed",
    "timed_out", "abandoned", name="attempt_status", native_enum=False,
    create_constraint=False,
)


def upgrade() -> None:
    op.create_table(
        "runs",
        sa.Column("run_id", sa.Uuid(), primary_key=True),
        sa.Column("suite_id", sa.String(255), nullable=False),
        sa.Column("idempotency_key", sa.String(255), nullable=False),
        sa.Column("parameters", sa.JSON(), nullable=False),
        sa.Column("suite_snapshot", sa.JSON(), nullable=False),
        sa.Column("gate_policy_snapshot", sa.JSON(), nullable=False),
        sa.Column("status", run_status, nullable=False),
        sa.Column("outcome", run_outcome, nullable=False, server_default="unknown"),
        sa.Column("version", sa.Integer(), nullable=False, server_default="1"),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("started_at", sa.DateTime(timezone=True)),
        sa.Column("finished_at", sa.DateTime(timezone=True)),
        sa.UniqueConstraint("idempotency_key", name="uq_runs_idempotency_key"),
        sa.CheckConstraint(
            "status IN ('queued', 'running', 'completed', 'infra_failed', 'timed_out')",
            name="ck_runs_status",
        ),
        sa.CheckConstraint(
            "outcome IN ('unknown', 'passed', 'failed')",
            name="ck_runs_outcome",
        ),
    )
    op.create_index("ix_runs_created_at", "runs", ["created_at"])
    op.create_index("ix_runs_status_created_at", "runs", ["status", "created_at"])

    op.create_table(
        "run_attempts",
        sa.Column("attempt_id", sa.Uuid(), primary_key=True),
        sa.Column("run_id", sa.Uuid(), sa.ForeignKey("runs.run_id", ondelete="CASCADE"), nullable=False),
        sa.Column("attempt_no", sa.Integer(), nullable=False),
        sa.Column("status", attempt_status, nullable=False),
        sa.Column("worker_id", sa.String(255)),
        sa.Column("lease_token", sa.Uuid()),
        sa.Column("heartbeat_at", sa.DateTime(timezone=True)),
        sa.Column("lease_expires_at", sa.DateTime(timezone=True)),
        sa.Column("exit_code", sa.Integer()),
        sa.Column("failure_reason", sa.Text()),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("started_at", sa.DateTime(timezone=True)),
        sa.Column("finished_at", sa.DateTime(timezone=True)),
        sa.UniqueConstraint("run_id", "attempt_no", name="uq_run_attempts_run_attempt"),
        sa.CheckConstraint(
            "status IN ('dispatched', 'running', 'passed', 'test_failed', "
            "'infra_failed', 'timed_out', 'abandoned')",
            name="ck_run_attempts_status",
        ),
    )
    op.create_index("ix_run_attempts_run_id", "run_attempts", ["run_id"])
    op.create_index("ix_run_attempts_created_at", "run_attempts", ["created_at"])
    op.create_index("ix_run_attempts_status_created_at", "run_attempts", ["status", "created_at"])

    op.create_table(
        "case_results",
        sa.Column("case_result_id", sa.Uuid(), primary_key=True),
        sa.Column("attempt_id", sa.Uuid(), sa.ForeignKey("run_attempts.attempt_id", ondelete="CASCADE"), nullable=False),
        sa.Column("node_id", sa.String(1000), nullable=False),
        sa.Column("status", sa.String(32), nullable=False),
        sa.Column("duration_ms", sa.Float()),
        sa.Column("message", sa.Text()),
        sa.Column("details", sa.JSON(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.UniqueConstraint("attempt_id", "node_id", name="uq_case_results_attempt_node"),
    )
    op.create_index("ix_case_results_attempt_id", "case_results", ["attempt_id"])
    op.create_index("ix_case_results_status", "case_results", ["status"])
    op.create_index("ix_case_results_created_at", "case_results", ["created_at"])

    op.create_table(
        "metrics",
        sa.Column("metric_id", sa.Uuid(), primary_key=True),
        sa.Column("attempt_id", sa.Uuid(), sa.ForeignKey("run_attempts.attempt_id", ondelete="CASCADE"), nullable=False),
        sa.Column("metric_name", sa.String(255), nullable=False),
        sa.Column("metric_value", sa.Float(), nullable=False),
        sa.Column("unit", sa.String(64)),
        sa.Column("details", sa.JSON(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.UniqueConstraint("attempt_id", "metric_name", name="uq_metrics_attempt_name"),
    )
    op.create_index("ix_metrics_attempt_id", "metrics", ["attempt_id"])
    op.create_index("ix_metrics_created_at", "metrics", ["created_at"])

    op.create_table(
        "artifacts",
        sa.Column("artifact_id", sa.Uuid(), primary_key=True),
        sa.Column("attempt_id", sa.Uuid(), sa.ForeignKey("run_attempts.attempt_id", ondelete="CASCADE"), nullable=False),
        sa.Column("artifact_type", sa.String(64), nullable=False),
        sa.Column("uri", sa.Text(), nullable=False),
        sa.Column("checksum", sa.String(255)),
        sa.Column("metadata", sa.JSON(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
    )
    op.create_index("ix_artifacts_attempt_id", "artifacts", ["attempt_id"])
    op.create_index("ix_artifacts_created_at", "artifacts", ["created_at"])

    op.create_table(
        "gate_evaluations",
        sa.Column("gate_evaluation_id", sa.Uuid(), primary_key=True),
        sa.Column("attempt_id", sa.Uuid(), sa.ForeignKey("run_attempts.attempt_id", ondelete="CASCADE"), nullable=False),
        sa.Column("gate_type", sa.String(64), nullable=False),
        sa.Column("passed", sa.Boolean(), nullable=False),
        sa.Column("reason_codes", sa.JSON(), nullable=False),
        sa.Column("details", sa.JSON(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
    )
    op.create_index("ix_gate_evaluations_attempt_id", "gate_evaluations", ["attempt_id"])
    op.create_index("ix_gate_evaluations_created_at", "gate_evaluations", ["created_at"])

    op.create_table(
        "run_events",
        sa.Column("event_id", sa.Uuid(), primary_key=True),
        sa.Column("run_id", sa.Uuid(), sa.ForeignKey("runs.run_id", ondelete="CASCADE"), nullable=False),
        sa.Column("event_type", sa.String(64), nullable=False),
        sa.Column("payload", sa.JSON(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
    )
    op.create_index("ix_run_events_run_id", "run_events", ["run_id"])
    op.create_index("ix_run_events_created_at", "run_events", ["created_at"])

    op.create_table(
        "outbox_events",
        sa.Column("outbox_event_id", sa.Uuid(), primary_key=True),
        sa.Column("aggregate_type", sa.String(64), nullable=False),
        sa.Column("aggregate_id", sa.Uuid(), nullable=False),
        sa.Column("event_type", sa.String(64), nullable=False),
        sa.Column("payload", sa.JSON(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("published_at", sa.DateTime(timezone=True)),
        sa.Column("publish_attempts", sa.Integer(), nullable=False, server_default="0"),
    )
    op.create_index("ix_outbox_events_aggregate_id", "outbox_events", ["aggregate_id"])
    op.create_index("ix_outbox_events_created_at", "outbox_events", ["created_at"])
    op.create_index("ix_outbox_events_unpublished_created", "outbox_events", ["published_at", "created_at"])


def downgrade() -> None:
    op.drop_table("outbox_events")
    op.drop_table("run_events")
    op.drop_table("gate_evaluations")
    op.drop_table("artifacts")
    op.drop_table("metrics")
    op.drop_table("case_results")
    op.drop_table("run_attempts")
    op.drop_table("runs")
