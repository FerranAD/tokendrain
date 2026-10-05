"""Kanban, execution outcomes and threshold policy. No task log migration."""

import sqlalchemy as sa
from alembic import op

revision = "0002"
down_revision = "0001"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column(
        "runs",
        sa.Column("threshold_mode", sa.String(16), nullable=False, server_default="graceful"),
    )
    for column in [
        sa.Column("termination_reason", sa.String(40)),
        sa.Column("termination_detail", sa.Text()),
        sa.Column("threshold_mode", sa.String(16)),
        sa.Column("interrupted", sa.Boolean(), nullable=False, server_default=sa.false()),
    ]:
        op.add_column("project_executions", column)
    op.execute(
        "UPDATE project_executions SET termination_reason='user_cancelled', "
        "interrupted=1 WHERE status='cancelled' AND error='Cancelled by user'"
    )
    op.execute(
        "UPDATE project_executions SET termination_reason='infrastructure_error', "
        "interrupted=1 WHERE status='failed' AND error IS NOT NULL"
    )
    with op.batch_alter_table("projects") as batch:
        batch.drop_column("task_log")
    op.create_table(
        "project_tasks",
        sa.Column("id", sa.String(32), primary_key=True),
        sa.Column(
            "project_id",
            sa.String(32),
            sa.ForeignKey("projects.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("title", sa.String(300), nullable=False),
        sa.Column("description", sa.Text(), nullable=False),
        sa.Column("column", sa.String(24), nullable=False),
        sa.Column("position", sa.Integer(), nullable=False),
        sa.Column("origin", sa.String(16), nullable=False),
        sa.Column("created_at", sa.DateTime(), nullable=False),
        sa.Column("updated_at", sa.DateTime(), nullable=False),
    )
    op.create_index("ix_project_tasks_project_id", "project_tasks", ["project_id"])


def downgrade() -> None:
    op.drop_table("project_tasks")
    with op.batch_alter_table("projects") as batch:
        batch.add_column(sa.Column("task_log", sa.Text(), nullable=False, server_default=""))
    for name in ("termination_reason", "termination_detail", "threshold_mode", "interrupted"):
        op.drop_column("project_executions", name)
    op.drop_column("runs", "threshold_mode")
