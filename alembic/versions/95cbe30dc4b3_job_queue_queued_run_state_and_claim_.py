"""job queue: queued run state and claim fields

Adds the QUEUED run state and the bookkeeping a worker needs to claim a run.

Two things autogenerate could not get right, both corrected by hand:

1. The new NOT NULL columns need a server_default. Autogenerate emitted plain
   NOT NULL, which fails the moment the table already has rows -- which it will,
   on any database that has been used.

2. Autogenerate was run against SQLite, which has no native enum type, so it
   could not see that `runstatus` gained a value. PostgreSQL does have one, and
   needs an explicit ALTER TYPE. `ADD VALUE IF NOT EXISTS` is safe inside a
   transaction on PostgreSQL 12+ provided the new value is not *used* in the same
   transaction, and it is not: this migration only adds columns.

Revision ID: 95cbe30dc4b3
Revises: edf630005bfc
Create Date: 2026-09-15 22:48:35.378044
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "95cbe30dc4b3"
down_revision: str | Sequence[str] | None = "edf630005bfc"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    bind = op.get_bind()

    if bind.dialect.name == "postgresql":
        op.execute("ALTER TYPE runstatus ADD VALUE IF NOT EXISTS 'QUEUED'")

    with op.batch_alter_table("agent_runs", schema=None) as batch_op:
        batch_op.add_column(
            sa.Column(
                "enable_diarization",
                sa.Boolean(),
                nullable=False,
                server_default=sa.false(),
            )
        )
        batch_op.add_column(sa.Column("claimed_at", sa.DateTime(timezone=True), nullable=True))
        batch_op.add_column(sa.Column("claimed_by", sa.String(length=128), nullable=True))
        batch_op.add_column(
            sa.Column("attempts", sa.Integer(), nullable=False, server_default="0")
        )
        batch_op.add_column(sa.Column("queued_at", sa.DateTime(timezone=True), nullable=True))

    # Index the claim lookup: the worker polls this on every tick.
    op.create_index(
        "ix_agent_runs_status_queued_at", "agent_runs", ["status", "queued_at"], unique=False
    )


def downgrade() -> None:
    op.drop_index("ix_agent_runs_status_queued_at", table_name="agent_runs")

    with op.batch_alter_table("agent_runs", schema=None) as batch_op:
        batch_op.drop_column("queued_at")
        batch_op.drop_column("attempts")
        batch_op.drop_column("claimed_by")
        batch_op.drop_column("claimed_at")
        batch_op.drop_column("enable_diarization")

    # PostgreSQL cannot remove a value from an enum type. Any row still holding
    # 'QUEUED' would be orphaned by a rebuild, so the value is deliberately left
    # in place: harmless, and safer than a destructive type swap on downgrade.
