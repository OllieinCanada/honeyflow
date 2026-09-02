"""store cover image data in database as BYTEA

Revision ID: 20260221_0009
Revises: 20260221_0008
Create Date: 2026-02-21
"""

import sqlalchemy as sa

from alembic import op

revision = "20260221_0009"
down_revision = "20260221_0008"
branch_labels = None
depends_on = None


def upgrade() -> None:
    bind = op.get_bind()
    inspector = sa.inspect(bind)
    column_names = {column["name"] for column in inspector.get_columns("projects")}
    if "cover_image_data" not in column_names:
        op.add_column(
            "projects",
            sa.Column("cover_image_data", sa.LargeBinary(), nullable=True),
        )


def downgrade() -> None:
    bind = op.get_bind()
    inspector = sa.inspect(bind)
    column_names = {column["name"] for column in inspector.get_columns("projects")}
    if "cover_image_data" in column_names:
        op.drop_column("projects", "cover_image_data")
