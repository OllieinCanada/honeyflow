"""Idempotency and concurrency contract tests for attribution storage."""

from __future__ import annotations

import asyncio
from copy import deepcopy

import pytest

from app.attribution.canonical import content_hash
from app.attribution.domain import build_manifest
from app.attribution.review import build_overlay
from app.attribution.service import AttributionService
from app.attribution.store import (
    AttributionConflictError,
    AttributionIntegrityError,
    AttributionNotFoundError,
    InMemoryAttributionStore,
    ensure_overlay_integrity,
    parse_manifest,
)
from app.schemas.attribution import CreateManifestRequest, CreateOverlayRequest


def test_concurrent_manifest_creation_has_one_canonical_result(manifest_request) -> None:
    async def scenario() -> None:
        store = InMemoryAttributionStore()
        manifest = build_manifest(manifest_request)
        results = await asyncio.gather(*(store.create_or_get_manifest(manifest) for _ in range(32)))

        assert sum(created for _, created in results) == 1
        assert {item.manifest_content_hash for item, _ in results} == {
            manifest.manifest_content_hash
        }

    asyncio.run(scenario())


def test_changed_evidence_for_same_source_key_is_an_explicit_conflict(
    manifest_payload: dict,
) -> None:
    async def scenario() -> None:
        store = InMemoryAttributionStore()
        first = build_manifest(CreateManifestRequest.model_validate(manifest_payload))
        changed_payload = deepcopy(manifest_payload)
        changed_payload["records"][0]["files"][0]["additions"] += 20
        conflicting = build_manifest(CreateManifestRequest.model_validate(changed_payload))
        await store.create_or_get_manifest(first)

        with pytest.raises(AttributionConflictError) as caught:
            await store.create_or_get_manifest(conflicting)
        assert caught.value.code == "manifest_source_conflict"

    asyncio.run(scenario())


def test_changed_excluded_evidence_is_an_explicit_source_conflict(
    manifest_payload: dict,
) -> None:
    async def scenario() -> None:
        store = InMemoryAttributionStore()
        payload = deepcopy(manifest_payload)
        payload["records"].append(
            {
                "record_id": "excluded-input-commitment",
                "commit_sha": "1" * 40,
                "author": {
                    "display_name": "Excluded Fixture",
                    "github_login": "excluded-fixture",
                },
                "files": [{"path": "vendor/input.py", "additions": 10}],
            }
        )
        first = build_manifest(CreateManifestRequest.model_validate(payload))
        changed_payload = deepcopy(payload)
        changed_payload["records"][-1]["files"][0]["additions"] = 11
        conflicting = build_manifest(CreateManifestRequest.model_validate(changed_payload))
        await store.create_or_get_manifest(first)

        with pytest.raises(AttributionConflictError) as caught:
            await store.create_or_get_manifest(conflicting)

        assert first.manifest_content_hash != conflicting.manifest_content_hash
        assert caught.value.code == "manifest_source_conflict"

    asyncio.run(scenario())


def test_store_returns_defensive_copies(manifest_request) -> None:
    async def scenario() -> None:
        store = InMemoryAttributionStore()
        manifest = build_manifest(manifest_request)
        stored, _ = await store.create_or_get_manifest(manifest)
        original_weight = stored.content.canonical_contributors[0].weight_units
        stored.content.canonical_contributors[0].weight_units = 0

        reread = await store.get_manifest(manifest.manifest_content_hash)
        assert reread.content.canonical_contributors[0].weight_units == original_weight

    asyncio.run(scenario())


def test_store_rejects_tampered_manifest(manifest_request) -> None:
    async def scenario() -> None:
        store = InMemoryAttributionStore()
        manifest = build_manifest(manifest_request)
        manifest.content.canonical_contributors[0].weight_units -= 1

        with pytest.raises(AttributionIntegrityError) as caught:
            await store.create_or_get_manifest(manifest)
        assert caught.value.code == "manifest_integrity_error"

    asyncio.run(scenario())


def test_store_rejects_rehashed_manifest_with_stale_configuration_fingerprint(
    manifest_request,
) -> None:
    async def scenario() -> None:
        store = InMemoryAttributionStore()
        manifest = build_manifest(manifest_request)
        manifest.content.attribution_configuration.rules.line_unit += 1
        manifest.manifest_content_hash = content_hash(manifest.content)

        with pytest.raises(AttributionIntegrityError) as caught:
            await store.create_or_get_manifest(manifest)
        assert caught.value.code == "manifest_integrity_error"

    asyncio.run(scenario())


def test_store_rejects_rehashed_manifest_with_stale_evidence_hash(
    manifest_request,
) -> None:
    async def scenario() -> None:
        store = InMemoryAttributionStore()
        manifest = build_manifest(manifest_request)
        manifest.content.evidence_records[0].included_files[0].additions += 1
        manifest.manifest_content_hash = content_hash(manifest.content)

        with pytest.raises(AttributionIntegrityError) as caught:
            await store.create_or_get_manifest(manifest)
        assert caught.value.code == "manifest_integrity_error"

    asyncio.run(scenario())


def test_store_rejects_rehashed_manifest_with_broken_evidence_reference(
    manifest_request,
) -> None:
    async def scenario() -> None:
        store = InMemoryAttributionStore()
        manifest = build_manifest(manifest_request)
        manifest.content.canonical_contributors[0].evidence_ids.pop()
        manifest.manifest_content_hash = content_hash(manifest.content)

        with pytest.raises(AttributionIntegrityError) as caught:
            await store.create_or_get_manifest(manifest)
        assert caught.value.code == "manifest_integrity_error"

    asyncio.run(scenario())


def test_persisted_manifest_schema_failure_is_structured() -> None:
    with pytest.raises(AttributionIntegrityError) as caught:
        parse_manifest({"content": {}})

    assert caught.value.code == "manifest_integrity_error"


def test_overlay_integrity_rejects_rehashed_nonzero_sum(manifest_request) -> None:
    manifest = build_manifest(manifest_request)
    contributors = manifest.content.canonical_contributors
    overlay = build_overlay(
        manifest,
        CreateOverlayRequest.model_validate(
            {
                "review_reference": "review:integrity-test",
                "adjustments": [
                    {
                        "contributor_id": contributors[0].contributor_id,
                        "delta_weight_units": -1,
                        "reason_code": "evidence_correction",
                        "explanation": "Synthetic integrity fixture.",
                    },
                    {
                        "contributor_id": contributors[1].contributor_id,
                        "delta_weight_units": 1,
                        "reason_code": "evidence_correction",
                        "explanation": "Synthetic integrity fixture.",
                    },
                ],
            }
        ),
    )
    overlay.content.adjustments[1].delta_weight_units = 2
    overlay.overlay_hash = content_hash(overlay.content)

    with pytest.raises(AttributionIntegrityError) as caught:
        ensure_overlay_integrity(overlay)

    assert caught.value.code == "overlay_integrity_error"


def test_overlay_creation_is_idempotent_and_base_stays_immutable(
    manifest_request,
) -> None:
    async def scenario() -> None:
        store = InMemoryAttributionStore()
        manifest = build_manifest(manifest_request)
        await store.create_or_get_manifest(manifest)
        contributors = manifest.content.canonical_contributors
        overlay = build_overlay(
            manifest,
            CreateOverlayRequest.model_validate(
                {
                    "review_reference": "review:store-test",
                    "adjustments": [
                        {
                            "contributor_id": contributors[0].contributor_id,
                            "delta_weight_units": -1,
                            "reason_code": "scope_correction",
                            "explanation": "Synthetic one-unit review adjustment.",
                        },
                        {
                            "contributor_id": contributors[1].contributor_id,
                            "delta_weight_units": 1,
                            "reason_code": "scope_correction",
                            "explanation": "Synthetic one-unit review adjustment.",
                        },
                    ],
                }
            ),
        )

        first, first_created = await store.create_or_get_overlay(overlay)
        second, second_created = await store.create_or_get_overlay(overlay)
        base = await store.get_manifest(manifest.manifest_content_hash)

        assert first == second
        assert first_created is True
        assert second_created is False
        assert base == manifest

    asyncio.run(scenario())


def test_service_rejects_unknown_overlay(manifest_request) -> None:
    async def scenario() -> None:
        service = AttributionService(InMemoryAttributionStore())
        with pytest.raises(AttributionNotFoundError) as caught:
            await service.get_overlay("0" * 64)
        assert caught.value.code == "overlay_not_found"

    asyncio.run(scenario())
