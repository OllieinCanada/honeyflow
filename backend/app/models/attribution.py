"""Persistence models for immutable attribution artifacts."""

from datetime import datetime

from sqlalchemy import DateTime, ForeignKey, Index, String, func
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column

from app.models.base import Base


class AttributionManifestRecord(Base):
    """A content-addressed, immutable Attribution Manifest V1."""

    __tablename__ = "attribution_manifests"

    manifest_content_hash: Mapped[str] = mapped_column(String(64), primary_key=True)
    source_key: Mapped[str] = mapped_column(String(64), unique=True, nullable=False)
    project_identity: Mapped[str] = mapped_column(String(300), nullable=False)
    source_repository_url: Mapped[str] = mapped_column(String(2000), nullable=False)
    source_commit_sha: Mapped[str] = mapped_column(String(64), nullable=False)
    algorithm_version: Mapped[str] = mapped_column(String(100), nullable=False)
    configuration_fingerprint: Mapped[str] = mapped_column(String(64), nullable=False)
    manifest_json: Mapped[dict] = mapped_column(JSONB, nullable=False)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )

    __table_args__ = (
        Index(
            "ix_attribution_manifest_source",
            "source_repository_url",
            "source_commit_sha",
        ),
    )


class AttributionReviewOverlayRecord(Base):
    """An immutable human-review delta that never rewrites its base manifest."""

    __tablename__ = "attribution_review_overlays"

    overlay_hash: Mapped[str] = mapped_column(String(64), primary_key=True)
    manifest_content_hash: Mapped[str] = mapped_column(
        String(64),
        ForeignKey(
            "attribution_manifests.manifest_content_hash",
            ondelete="RESTRICT",
        ),
        nullable=False,
    )
    overlay_json: Mapped[dict] = mapped_column(JSONB, nullable=False)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )

    __table_args__ = (
        Index(
            "ix_attribution_review_overlay_manifest",
            "manifest_content_hash",
        ),
    )
