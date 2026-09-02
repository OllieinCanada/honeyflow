"""PostgreSQL implementation of the attribution storage boundary."""

from __future__ import annotations

from sqlalchemy import select
from sqlalchemy.dialects.postgresql import insert

from app.attribution.store import (
    AttributionConflictError,
    AttributionNotFoundError,
    ensure_manifest_integrity,
    ensure_overlay_integrity,
    manifest_source_key,
    parse_manifest,
    parse_overlay,
)
from app.database import session_scope
from app.models.attribution import (
    AttributionManifestRecord,
    AttributionReviewOverlayRecord,
)
from app.schemas.attribution import AttributionManifest, ReviewOverlay


class PostgresAttributionStore:
    """Transaction-safe storage using targeted PostgreSQL conflict handling."""

    async def create_or_get_manifest(
        self,
        manifest: AttributionManifest,
    ) -> tuple[AttributionManifest, bool]:
        ensure_manifest_integrity(manifest)
        source_key = manifest_source_key(manifest)
        values = {
            "manifest_content_hash": manifest.manifest_content_hash,
            "source_key": source_key,
            "project_identity": manifest.content.project_identity,
            "source_repository_url": manifest.content.source_repository_url,
            "source_commit_sha": manifest.content.source_commit_sha,
            "algorithm_version": manifest.content.algorithm_version,
            "configuration_fingerprint": manifest.content.configuration_fingerprint,
            "manifest_json": manifest.model_dump(mode="json"),
        }
        statement = (
            insert(AttributionManifestRecord)
            .values(**values)
            .on_conflict_do_nothing(index_elements=["source_key"])
            .returning(AttributionManifestRecord.manifest_content_hash)
        )
        async with session_scope() as session:
            inserted_hash = (await session.execute(statement)).scalar_one_or_none()
            stored = (
                await session.execute(
                    select(AttributionManifestRecord).where(
                        AttributionManifestRecord.source_key == source_key
                    )
                )
            ).scalar_one()
            if stored.manifest_content_hash != manifest.manifest_content_hash:
                raise AttributionConflictError(
                    "manifest_source_conflict",
                    "this source revision and configuration already have a different manifest",
                )
            return parse_manifest(stored.manifest_json), inserted_hash is not None

    async def get_manifest(self, manifest_hash: str) -> AttributionManifest:
        async with session_scope() as session:
            stored = (
                await session.execute(
                    select(AttributionManifestRecord).where(
                        AttributionManifestRecord.manifest_content_hash == manifest_hash
                    )
                )
            ).scalar_one_or_none()
            if stored is None:
                raise AttributionNotFoundError(
                    "manifest_not_found",
                    "attribution manifest was not found",
                )
            return parse_manifest(stored.manifest_json)

    async def create_or_get_overlay(
        self,
        overlay: ReviewOverlay,
    ) -> tuple[ReviewOverlay, bool]:
        ensure_overlay_integrity(overlay)
        statement = (
            insert(AttributionReviewOverlayRecord)
            .values(
                overlay_hash=overlay.overlay_hash,
                manifest_content_hash=overlay.content.base_manifest_hash,
                overlay_json=overlay.model_dump(mode="json"),
            )
            .on_conflict_do_nothing(index_elements=["overlay_hash"])
            .returning(AttributionReviewOverlayRecord.overlay_hash)
        )
        async with session_scope() as session:
            inserted_hash = (await session.execute(statement)).scalar_one_or_none()
            stored = (
                await session.execute(
                    select(AttributionReviewOverlayRecord).where(
                        AttributionReviewOverlayRecord.overlay_hash == overlay.overlay_hash
                    )
                )
            ).scalar_one()
            return parse_overlay(stored.overlay_json), inserted_hash is not None

    async def get_overlay(self, overlay_hash: str) -> ReviewOverlay:
        async with session_scope() as session:
            stored = (
                await session.execute(
                    select(AttributionReviewOverlayRecord).where(
                        AttributionReviewOverlayRecord.overlay_hash == overlay_hash
                    )
                )
            ).scalar_one_or_none()
            if stored is None:
                raise AttributionNotFoundError(
                    "overlay_not_found",
                    "attribution review overlay was not found",
                )
            return parse_overlay(stored.overlay_json)
