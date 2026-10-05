"""GitHub access modes and shared host-managed repository policy."""

import sqlalchemy as sa
from alembic import op

revision = "0003"
down_revision = "0002"
branch_labels = None
depends_on = None


def upgrade() -> None:
    # Existing raw grants become read-only until a user explicitly chooses a write mode.
    op.add_column(
        "project_github_integrations",
        sa.Column("access_mode", sa.String(24), nullable=False, server_default="read_only"),
    )
    op.add_column(
        "project_github_integrations",
        sa.Column("allow_workflows", sa.Boolean(), nullable=False, server_default=sa.false()),
    )
    op.execute("UPDATE project_github_integrations SET permissions='{}'")
    op.create_table(
        "github_repository_policies",
        sa.Column("repository_id", sa.Integer(), primary_key=True),
        sa.Column("installation_id", sa.Integer(), nullable=False),
        sa.Column("repository_name", sa.String(300), nullable=False),
        sa.Column("ruleset_id", sa.Integer(), nullable=True),
        sa.Column("may_exist", sa.Boolean(), nullable=False, server_default=sa.false()),
        sa.Column("error", sa.String(1000), nullable=True),
    )


def downgrade() -> None:
    op.drop_table("github_repository_policies")
    op.drop_column("project_github_integrations", "allow_workflows")
    op.drop_column("project_github_integrations", "access_mode")
