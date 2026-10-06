"""Create automation tables, including for databases initialized before automations."""

import sqlalchemy as sa
from alembic import op

revision = "0004"
down_revision = "0003"
branch_labels = None
depends_on = None


def upgrade() -> None:
    tables = sa.inspect(op.get_bind()).get_table_names()
    if "automations" not in tables:
        op.create_table(
            "automations",
            sa.Column("id", sa.String(32), primary_key=True),
            sa.Column("name", sa.String(200), nullable=False),
            sa.Column("enabled", sa.Boolean(), nullable=False),
            sa.Column("trigger", sa.JSON(), nullable=False),
            sa.Column("mode", sa.String(24), nullable=False),
            sa.Column("run_template", sa.JSON(), nullable=False),
            sa.Column("created_at", sa.DateTime(), nullable=False),
            sa.Column("last_checked_at", sa.DateTime(), nullable=True),
            sa.Column("last_error", sa.Text(), nullable=True),
        )
    if "automation_occurrences" not in tables:
        op.create_table(
            "automation_occurrences",
            sa.Column("id", sa.String(32), primary_key=True),
            sa.Column("automation_id", sa.String(32), nullable=True),
            sa.Column("automation_name", sa.String(200), nullable=False),
            sa.Column("limit_id", sa.String(200), nullable=False),
            sa.Column("window_minutes", sa.Integer(), nullable=False),
            sa.Column("resets_at", sa.DateTime(), nullable=False),
            sa.Column("matched_window", sa.JSON(), nullable=False),
            sa.Column("run_template", sa.JSON(), nullable=False),
            sa.Column("mode", sa.String(24), nullable=False),
            sa.Column("status", sa.String(24), nullable=False),
            sa.Column("created_at", sa.DateTime(), nullable=False),
            sa.Column("notified_at", sa.DateTime(), nullable=True),
            sa.Column("delivery_error", sa.Text(), nullable=True),
            sa.Column("last_error", sa.Text(), nullable=True),
            sa.Column("run_id", sa.String(32), nullable=True),
            sa.ForeignKeyConstraint(["automation_id"], ["automations.id"], ondelete="SET NULL"),
            sa.ForeignKeyConstraint(["run_id"], ["runs.id"], ondelete="SET NULL"),
        )
        op.create_index(
            "uq_automation_occurrence",
            "automation_occurrences",
            ["automation_id", "limit_id", "window_minutes", "resets_at"],
            unique=True,
        )


def downgrade() -> None:
    op.drop_table("automation_occurrences")
    op.drop_table("automations")
