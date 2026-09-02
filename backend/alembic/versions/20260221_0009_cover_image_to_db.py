"""store cover image data in database as BYTEA

Revision ID: 20260221_0009
Revises: 20260221_0008
Create Date: 2026-02-21
"""

from alembic import op
import sqlalchemy as sa

revision = "20260221_0009"
down_revision = "20260221_0008"
branch_labels = None
depends_on = None


def upgrade() -> None:
    # 0008 owns this column. This guard also repairs databases where the old,
    # duplicate 0008 revision identifier marked the wrong operation applied.
    bind = op.get_bind()
    columns = {column["name"] for column in sa.inspect(bind).get_columns("projects")}
    if "cover_image_data" not in columns:
        op.add_column(
            "projects",
            sa.Column("cover_image_data", sa.LargeBinary(), nullable=True),
        )


def downgrade() -> None:
    # 0008 owns the column, so downgrading this compatibility repair is a no-op.
    pass
