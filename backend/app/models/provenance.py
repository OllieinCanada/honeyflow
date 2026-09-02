"""Immutable database snapshots of contribution provenance manifests."""

from datetime import datetime

from sqlalchemy import (
    CheckConstraint,
    DateTime,
    ForeignKey,
    Index,
    Integer,
    String,
    UniqueConstraint,
    func,
)
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column

from app.models.base import Base


class ProvenanceSnapshot(Base):
    __tablename__ = "provenance_snapshots"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    project_id: Mapped[int] = mapped_column(
        ForeignKey("projects.id", ondelete="CASCADE"), nullable=False
    )
    sequence: Mapped[int] = mapped_column(Integer, nullable=False)
    source_commit_sha: Mapped[str] = mapped_column(String(64), nullable=False)
    digest: Mapped[str] = mapped_column(String(64), nullable=False)
    previous_digest: Mapped[str | None] = mapped_column(String(64), nullable=True)
    manifest: Mapped[dict] = mapped_column(JSONB, nullable=False)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )

    __table_args__ = (
        CheckConstraint("sequence > 0", name="ck_provenance_sequence_positive"),
        UniqueConstraint("project_id", "sequence", name="uq_provenance_project_sequence"),
        UniqueConstraint("project_id", "digest", name="uq_provenance_project_digest"),
        Index("ix_provenance_project_created", "project_id", "created_at"),
        Index("ix_provenance_source_commit", "source_commit_sha"),
    )
