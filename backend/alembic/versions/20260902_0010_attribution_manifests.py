"""add immutable attribution manifests and review overlays

Revision ID: 20260902_0010
Revises: 20260221_0009
Create Date: 2026-09-02
"""

import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

from alembic import op

revision = "20260902_0010"
down_revision = "20260221_0009"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "attribution_manifests",
        sa.Column("manifest_content_hash", sa.String(length=64), nullable=False),
        sa.Column("source_key", sa.String(length=64), nullable=False),
        sa.Column("project_identity", sa.String(length=300), nullable=False),
        sa.Column("source_repository_url", sa.String(length=2000), nullable=False),
        sa.Column("source_commit_sha", sa.String(length=64), nullable=False),
        sa.Column("algorithm_version", sa.String(length=100), nullable=False),
        sa.Column("configuration_fingerprint", sa.String(length=64), nullable=False),
        sa.Column("manifest_json", postgresql.JSONB(astext_type=sa.Text()), nullable=False),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.PrimaryKeyConstraint("manifest_content_hash"),
        sa.UniqueConstraint("source_key"),
    )
    op.create_index(
        "ix_attribution_manifest_source",
        "attribution_manifests",
        ["source_repository_url", "source_commit_sha"],
        unique=False,
    )
    op.create_table(
        "attribution_review_overlays",
        sa.Column("overlay_hash", sa.String(length=64), nullable=False),
        sa.Column("manifest_content_hash", sa.String(length=64), nullable=False),
        sa.Column("overlay_json", postgresql.JSONB(astext_type=sa.Text()), nullable=False),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.ForeignKeyConstraint(
            ["manifest_content_hash"],
            ["attribution_manifests.manifest_content_hash"],
            ondelete="RESTRICT",
        ),
        sa.PrimaryKeyConstraint("overlay_hash"),
    )
    op.create_index(
        "ix_attribution_review_overlay_manifest",
        "attribution_review_overlays",
        ["manifest_content_hash"],
        unique=False,
    )


def downgrade() -> None:
    op.drop_index(
        "ix_attribution_review_overlay_manifest",
        table_name="attribution_review_overlays",
    )
    op.drop_table("attribution_review_overlays")
    op.drop_index(
        "ix_attribution_manifest_source",
        table_name="attribution_manifests",
    )
    op.drop_table("attribution_manifests")
