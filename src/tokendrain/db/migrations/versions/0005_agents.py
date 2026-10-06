"""Global agent choice, separate project sessions and scoped usage history."""

import sqlalchemy as sa
from alembic import op

revision = "0005"
down_revision = "0004"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column(
        "projects", sa.Column("agent_state", sa.JSON(), nullable=False, server_default="{}")
    )
    for table in ("runs", "project_executions", "usage_snapshots"):
        op.add_column(
            table, sa.Column("agent", sa.String(24), nullable=False, server_default="codex")
        )
    op.add_column("usage_snapshots", sa.Column("account_id", sa.String(200), nullable=True))
    existing = {c["name"] for c in sa.inspect(op.get_bind()).get_columns("automation_occurrences")}
    if "account_scope" not in existing:
        op.add_column(
            "automation_occurrences",
            sa.Column("account_scope", sa.String(250), nullable=False, server_default="codex:"),
        )
        op.drop_index("uq_automation_occurrence", table_name="automation_occurrences")
        op.create_index(
            "uq_automation_occurrence",
            "automation_occurrences",
            ["automation_id", "account_scope", "limit_id", "window_minutes", "resets_at"],
            unique=True,
        )
    for table in ("schedules", "automations", "automation_occurrences"):
        op.execute(
            f"UPDATE {table} SET run_template=json_set(run_template, '$.configured_agent', 'codex')"
        )


def downgrade() -> None:
    op.drop_index("uq_automation_occurrence", table_name="automation_occurrences")
    op.drop_column("automation_occurrences", "account_scope")
    op.create_index(
        "uq_automation_occurrence",
        "automation_occurrences",
        ["automation_id", "limit_id", "window_minutes", "resets_at"],
        unique=True,
    )
    op.drop_column("usage_snapshots", "account_id")
    for table in ("runs", "project_executions", "usage_snapshots"):
        op.drop_column(table, "agent")
    op.drop_column("projects", "agent_state")
