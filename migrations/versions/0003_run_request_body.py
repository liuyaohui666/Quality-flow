"""Store the validated business request body with each Run.

Revision ID: 0003_run_request_body
Revises: 0002_attempt_lease_invariants
"""

from alembic import op
import sqlalchemy as sa


revision = "0003_run_request_body"
down_revision = "0002_attempt_lease_invariants"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column("runs", sa.Column("request_body", sa.JSON(), nullable=True))


def downgrade() -> None:
    op.drop_column("runs", "request_body")
