"""Enforce complete running leases and accelerate stale scans.

Revision ID: 0002_attempt_lease_invariants
Revises: 0001_initial_schema
"""

from alembic import op


revision = "0002_attempt_lease_invariants"
down_revision = "0001_initial_schema"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_check_constraint(
        "ck_run_attempts_running_lease",
        "run_attempts",
        "status <> 'running' OR (lease_token IS NOT NULL AND "
        "heartbeat_at IS NOT NULL AND lease_expires_at IS NOT NULL AND "
        "lease_expires_at > heartbeat_at)",
    )
    op.create_index(
        "ix_run_attempts_status_lease_expires_at",
        "run_attempts",
        ["status", "lease_expires_at"],
    )


def downgrade() -> None:
    op.drop_index(
        "ix_run_attempts_status_lease_expires_at", table_name="run_attempts"
    )
    op.drop_constraint(
        "ck_run_attempts_running_lease", "run_attempts", type_="check"
    )
