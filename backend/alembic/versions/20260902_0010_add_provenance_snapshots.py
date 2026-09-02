"""add immutable provenance snapshots

Revision ID: 20260902_0010
Revises: 20260221_0009
Create Date: 2026-09-02
"""

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

revision = "20260902_0010"
down_revision = "20260221_0009"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "provenance_snapshots",
        sa.Column("id", sa.Integer(), autoincrement=True, nullable=False),
        sa.Column("project_id", sa.Integer(), nullable=False),
        sa.Column("sequence", sa.Integer(), nullable=False),
        sa.Column("source_commit_sha", sa.String(length=64), nullable=False),
        sa.Column("digest", sa.String(length=64), nullable=False),
        sa.Column("previous_digest", sa.String(length=64), nullable=True),
        sa.Column("manifest", postgresql.JSONB(astext_type=sa.Text()), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.CheckConstraint("sequence > 0", name="ck_provenance_sequence_positive"),
        sa.ForeignKeyConstraint(["project_id"], ["projects.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("project_id", "digest", name="uq_provenance_project_digest"),
        sa.UniqueConstraint("project_id", "sequence", name="uq_provenance_project_sequence"),
    )
    op.create_index("ix_provenance_project_created", "provenance_snapshots", ["project_id", "created_at"])
    op.create_index("ix_provenance_source_commit", "provenance_snapshots", ["source_commit_sha"])


def downgrade() -> None:
    op.drop_index("ix_provenance_source_commit", table_name="provenance_snapshots")
    op.drop_index("ix_provenance_project_created", table_name="provenance_snapshots")
    op.drop_table("provenance_snapshots")
