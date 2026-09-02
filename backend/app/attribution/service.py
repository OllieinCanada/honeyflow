"""Application service for deterministic attribution operations."""

from __future__ import annotations

from app.attribution.domain import build_manifest
from app.attribution.review import build_overlay, build_payout_preview
from app.attribution.store import AttributionStore
from app.schemas.attribution import (
    AttributionManifest,
    CreateManifestRequest,
    CreateOverlayRequest,
    PayoutPreview,
    PayoutPreviewRequest,
    ReviewOverlay,
)


class AttributionService:
    def __init__(self, store: AttributionStore):
        self._store = store

    async def create_manifest(
        self,
        request: CreateManifestRequest,
    ) -> tuple[AttributionManifest, bool]:
        return await self._store.create_or_get_manifest(build_manifest(request))

    async def get_manifest(self, manifest_hash: str) -> AttributionManifest:
        return await self._store.get_manifest(manifest_hash)

    async def create_overlay(
        self,
        manifest_hash: str,
        request: CreateOverlayRequest,
    ) -> tuple[ReviewOverlay, bool]:
        manifest = await self._store.get_manifest(manifest_hash)
        overlay = build_overlay(manifest, request)
        return await self._store.create_or_get_overlay(overlay)

    async def get_overlay(self, overlay_hash: str) -> ReviewOverlay:
        return await self._store.get_overlay(overlay_hash)

    async def preview_payout(
        self,
        manifest_hash: str,
        request: PayoutPreviewRequest,
    ) -> PayoutPreview:
        manifest = await self._store.get_manifest(manifest_hash)
        overlay = None
        if request.overlay_hash is not None:
            overlay = await self._store.get_overlay(request.overlay_hash)
        return build_payout_preview(manifest, request, overlay)
