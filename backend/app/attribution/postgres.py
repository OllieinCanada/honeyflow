"""PostgreSQL implementation of the attribution storage boundary."""

from __future__ import annotations

from sqlalchemy import select
from sqlalchemy.dialects.postgresql import insert

from app.attribution.store import (
    AttributionConflictError,
    AttributionIntegrityError,
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


def _manifest_from_record(
    record: AttributionManifestRecord,
    *,
    expected_manifest_hash: str | None = None,
    expected_source_key: str | None = None,
) -> AttributionManifest:
    """Validate JSON content against every content-addressed row binding."""
    manifest = parse_manifest(record.manifest_json)
    bindings_match = (
        manifest.manifest_content_hash == record.manifest_content_hash
        and manifest_source_key(manifest) == record.source_key
        and manifest.content.project_identity == record.project_identity
        and manifest.content.source_repository_url == record.source_repository_url
        and manifest.content.source_commit_sha == record.source_commit_sha
        and manifest.content.algorithm_version == record.algorithm_version
        and (manifest.content.configuration_fingerprint == record.configuration_fingerprint)
    )
    if expected_manifest_hash is not None:
        bindings_match = bindings_match and record.manifest_content_hash == expected_manifest_hash
    if expected_source_key is not None:
        bindings_match = bindings_match and record.source_key == expected_source_key
    if not bindings_match:
        raise AttributionIntegrityError(
            "manifest_row_binding_error",
            "stored attribution manifest does not match its row identity",
        )
    return manifest


def _overlay_from_record(
    record: AttributionReviewOverlayRecord,
    *,
    expected_overlay_hash: str | None = None,
    expected_base_manifest_hash: str | None = None,
) -> ReviewOverlay:
    """Validate overlay JSON against its row key and manifest foreign key."""
    overlay = parse_overlay(record.overlay_json)
    bindings_match = (
        overlay.overlay_hash == record.overlay_hash
        and overlay.content.base_manifest_hash == record.manifest_content_hash
    )
    if expected_overlay_hash is not None:
        bindings_match = bindings_match and record.overlay_hash == expected_overlay_hash
    if expected_base_manifest_hash is not None:
        bindings_match = (
            bindings_match and record.manifest_content_hash == expected_base_manifest_hash
        )
    if not bindings_match:
        raise AttributionIntegrityError(
            "overlay_row_binding_error",
            "stored attribution overlay does not match its row identity",
        )
    return overlay


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
            stored_manifest = _manifest_from_record(
                stored,
                expected_source_key=source_key,
            )
            if inserted_hash is not None and inserted_hash != manifest.manifest_content_hash:
                raise AttributionIntegrityError(
                    "manifest_row_binding_error",
                    "inserted attribution manifest returned an unexpected identity",
                )
            if stored_manifest.manifest_content_hash != manifest.manifest_content_hash:
                raise AttributionConflictError(
                    "manifest_source_conflict",
                    "this source revision and configuration already have a different manifest",
                )
            return stored_manifest, inserted_hash is not None

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
            return _manifest_from_record(
                stored,
                expected_manifest_hash=manifest_hash,
            )

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
            stored_overlay = _overlay_from_record(
                stored,
                expected_overlay_hash=overlay.overlay_hash,
                expected_base_manifest_hash=overlay.content.base_manifest_hash,
            )
            if inserted_hash is not None and inserted_hash != overlay.overlay_hash:
                raise AttributionIntegrityError(
                    "overlay_row_binding_error",
                    "inserted attribution overlay returned an unexpected identity",
                )
            return stored_overlay, inserted_hash is not None

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
            return _overlay_from_record(
                stored,
                expected_overlay_hash=overlay_hash,
            )
