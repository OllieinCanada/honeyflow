"""Storage boundary for immutable attribution manifests and review overlays."""

from __future__ import annotations

import asyncio
from typing import Any, Protocol

from pydantic import ValidationError

from app.attribution.canonical import content_hash
from app.attribution.domain import AttributionDomainError
from app.attribution.review import verify_manifest
from app.schemas.attribution import (
    AttributionManifest,
    ReviewOverlay,
)


class AttributionStoreError(RuntimeError):
    """A structured storage failure that can be mapped at an API boundary."""

    def __init__(self, code: str, detail: str):
        super().__init__(detail)
        self.code = code
        self.detail = detail


class AttributionNotFoundError(AttributionStoreError):
    pass


class AttributionConflictError(AttributionStoreError):
    pass


class AttributionIntegrityError(AttributionStoreError):
    pass


def ensure_manifest_integrity(manifest: AttributionManifest) -> AttributionManifest:
    try:
        verify_manifest(manifest)
    except AttributionDomainError as error:
        raise AttributionIntegrityError(
            "manifest_integrity_error",
            "stored attribution manifest failed semantic integrity checks",
        ) from error
    return manifest


def ensure_overlay_integrity(overlay: ReviewOverlay) -> ReviewOverlay:
    if content_hash(overlay.content) != overlay.overlay_hash:
        raise AttributionIntegrityError(
            "overlay_integrity_error",
            "stored attribution overlay failed its content-hash check",
        )
    contributor_ids = [adjustment.contributor_id for adjustment in overlay.content.adjustments]
    if (
        len(contributor_ids) != len(set(contributor_ids))
        or sum(adjustment.delta_weight_units for adjustment in overlay.content.adjustments) != 0
    ):
        raise AttributionIntegrityError(
            "overlay_integrity_error",
            "stored attribution overlay failed its adjustment-invariant check",
        )
    return overlay


def parse_manifest(payload: Any) -> AttributionManifest:
    try:
        return ensure_manifest_integrity(AttributionManifest.model_validate(payload))
    except ValidationError as error:
        raise AttributionIntegrityError(
            "manifest_integrity_error",
            "stored attribution manifest failed schema validation",
        ) from error


def parse_overlay(payload: Any) -> ReviewOverlay:
    try:
        return ensure_overlay_integrity(ReviewOverlay.model_validate(payload))
    except ValidationError as error:
        raise AttributionIntegrityError(
            "overlay_integrity_error",
            "stored attribution overlay failed schema validation",
        ) from error


def manifest_source_key(manifest: AttributionManifest) -> str:
    """Identify the one canonical manifest for a source revision and config.

    The manifest hash itself covers every evidence input.  The narrower source
    key deliberately excludes those inputs so a second, inconsistent evidence
    set for the same revision/configuration produces an explicit conflict
    instead of silently creating a competing canonical base.
    """

    content = manifest.content
    return content_hash(
        {
            "schema_version": content.schema_version,
            "project_identity": content.project_identity,
            "source_repository_url": content.source_repository_url,
            "source_commit_sha": content.source_commit_sha,
            "algorithm_version": content.algorithm_version,
            "configuration_fingerprint": content.configuration_fingerprint,
        }
    )


class AttributionStore(Protocol):
    async def create_or_get_manifest(
        self,
        manifest: AttributionManifest,
    ) -> tuple[AttributionManifest, bool]: ...

    async def get_manifest(self, manifest_hash: str) -> AttributionManifest: ...

    async def create_or_get_overlay(
        self,
        overlay: ReviewOverlay,
    ) -> tuple[ReviewOverlay, bool]: ...

    async def get_overlay(self, overlay_hash: str) -> ReviewOverlay: ...


class InMemoryAttributionStore:
    """Deterministic test/local store with the same idempotency semantics."""

    def __init__(self) -> None:
        self._lock = asyncio.Lock()
        self._manifests: dict[str, AttributionManifest] = {}
        self._manifest_hash_by_source_key: dict[str, str] = {}
        self._overlays: dict[str, ReviewOverlay] = {}

    async def create_or_get_manifest(
        self,
        manifest: AttributionManifest,
    ) -> tuple[AttributionManifest, bool]:
        ensure_manifest_integrity(manifest)
        source_key = manifest_source_key(manifest)
        async with self._lock:
            existing_hash = self._manifest_hash_by_source_key.get(source_key)
            if existing_hash is not None:
                existing = self._manifests[existing_hash]
                if existing.manifest_content_hash != manifest.manifest_content_hash:
                    raise AttributionConflictError(
                        "manifest_source_conflict",
                        "this source revision and configuration already have a different manifest",
                    )
                return existing.model_copy(deep=True), False
            self._manifests[manifest.manifest_content_hash] = manifest.model_copy(deep=True)
            self._manifest_hash_by_source_key[source_key] = manifest.manifest_content_hash
            return manifest.model_copy(deep=True), True

    async def get_manifest(self, manifest_hash: str) -> AttributionManifest:
        async with self._lock:
            manifest = self._manifests.get(manifest_hash)
            if manifest is None:
                raise AttributionNotFoundError(
                    "manifest_not_found",
                    "attribution manifest was not found",
                )
            return manifest.model_copy(deep=True)

    async def create_or_get_overlay(
        self,
        overlay: ReviewOverlay,
    ) -> tuple[ReviewOverlay, bool]:
        ensure_overlay_integrity(overlay)
        async with self._lock:
            existing = self._overlays.get(overlay.overlay_hash)
            if existing is not None:
                return existing.model_copy(deep=True), False
            if overlay.content.base_manifest_hash not in self._manifests:
                raise AttributionNotFoundError(
                    "manifest_not_found",
                    "overlay base manifest was not found",
                )
            self._overlays[overlay.overlay_hash] = overlay.model_copy(deep=True)
            return overlay.model_copy(deep=True), True

    async def get_overlay(self, overlay_hash: str) -> ReviewOverlay:
        async with self._lock:
            overlay = self._overlays.get(overlay_hash)
            if overlay is None:
                raise AttributionNotFoundError(
                    "overlay_not_found",
                    "attribution review overlay was not found",
                )
            return overlay.model_copy(deep=True)
