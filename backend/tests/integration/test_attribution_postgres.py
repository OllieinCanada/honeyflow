"""PostgreSQL concurrency test; enabled when TEST_DATABASE_URL is configured."""

from __future__ import annotations

import asyncio
import os
from copy import deepcopy

import pytest

from app.attribution.domain import build_manifest
from app.attribution.review import build_overlay
from app.attribution.store import AttributionIntegrityError, manifest_source_key
from app.schemas.attribution import CreateManifestRequest, CreateOverlayRequest

pytestmark = pytest.mark.skipif(
    not os.getenv("TEST_DATABASE_URL"),
    reason="TEST_DATABASE_URL is required for PostgreSQL integration tests",
)


def test_concurrent_postgres_creation_produces_one_manifest() -> None:
    async def scenario() -> None:
        from sqlalchemy import delete

        from app.attribution.postgres import PostgresAttributionStore
        from app.attribution.store import manifest_source_key
        from app.database import session_scope
        from app.models.attribution import AttributionManifestRecord

        request = CreateManifestRequest.model_validate(
            {
                "project_identity": "integration/postgres-concurrency",
                "source_repository_url": "https://github.com/example/postgres-fixture",
                "source_commit_sha": "7" * 40,
                "records": [
                    {
                        "record_id": "integration-commit",
                        "commit_sha": "6" * 40,
                        "author": {
                            "display_name": "Integration Fixture",
                            "github_login": "integration-fixture",
                        },
                        "files": [{"path": "src/integration.py", "additions": 3}],
                    }
                ],
            }
        )
        manifest = build_manifest(request)
        source_key = manifest_source_key(manifest)
        async with session_scope() as session:
            await session.execute(
                delete(AttributionManifestRecord).where(
                    AttributionManifestRecord.source_key == source_key
                )
            )

        store = PostgresAttributionStore()
        results = await asyncio.gather(*(store.create_or_get_manifest(manifest) for _ in range(12)))

        assert sum(created for _, created in results) == 1
        assert {stored.manifest_content_hash for stored, _ in results} == {
            manifest.manifest_content_hash
        }

        async with session_scope() as session:
            await session.execute(
                delete(AttributionManifestRecord).where(
                    AttributionManifestRecord.source_key == source_key
                )
            )

    asyncio.run(scenario())


def test_postgres_manifest_row_bindings_guard_create_and_get(
    manifest_payload: dict,
) -> None:
    async def scenario() -> None:
        from sqlalchemy import delete, update

        from app.attribution.postgres import PostgresAttributionStore
        from app.database import session_scope
        from app.models.attribution import AttributionManifestRecord

        payload = deepcopy(manifest_payload)
        payload["project_identity"] = "integration/manifest-row-binding"
        payload["source_commit_sha"] = "5" * 40
        manifest = build_manifest(CreateManifestRequest.model_validate(payload))
        changed_payload = deepcopy(payload)
        changed_payload["records"][0]["files"][0]["additions"] += 1
        another = build_manifest(CreateManifestRequest.model_validate(changed_payload))
        source_key = manifest_source_key(manifest)
        store = PostgresAttributionStore()

        async with session_scope() as session:
            await session.execute(
                delete(AttributionManifestRecord).where(
                    AttributionManifestRecord.source_key == source_key
                )
            )
        try:
            await store.create_or_get_manifest(manifest)
            async with session_scope() as session:
                await session.execute(
                    update(AttributionManifestRecord)
                    .where(
                        AttributionManifestRecord.manifest_content_hash
                        == manifest.manifest_content_hash
                    )
                    .values(manifest_json=another.model_dump(mode="json"))
                )

            with pytest.raises(AttributionIntegrityError):
                await store.get_manifest(manifest.manifest_content_hash)
            with pytest.raises(AttributionIntegrityError):
                await store.create_or_get_manifest(manifest)
        finally:
            async with session_scope() as session:
                await session.execute(
                    delete(AttributionManifestRecord).where(
                        AttributionManifestRecord.source_key == source_key
                    )
                )

    asyncio.run(scenario())


def test_postgres_overlay_row_binding_guards_create_and_get(
    manifest_payload: dict,
) -> None:
    async def scenario() -> None:
        from sqlalchemy import delete, update

        from app.attribution.postgres import PostgresAttributionStore
        from app.database import session_scope
        from app.models.attribution import (
            AttributionManifestRecord,
            AttributionReviewOverlayRecord,
        )

        first_payload = deepcopy(manifest_payload)
        first_payload["project_identity"] = "integration/overlay-row-binding-base"
        first_payload["source_commit_sha"] = "4" * 40
        second_payload = deepcopy(manifest_payload)
        second_payload["project_identity"] = "integration/overlay-row-binding-other"
        second_payload["source_commit_sha"] = "3" * 40
        first = build_manifest(CreateManifestRequest.model_validate(first_payload))
        second = build_manifest(CreateManifestRequest.model_validate(second_payload))
        contributors = first.content.canonical_contributors
        overlay = build_overlay(
            first,
            CreateOverlayRequest.model_validate(
                {
                    "review_reference": "review:postgres-row-binding",
                    "adjustments": [
                        {
                            "contributor_id": contributors[0].contributor_id,
                            "delta_weight_units": -1,
                            "reason_code": "evidence_correction",
                            "explanation": "Synthetic PostgreSQL binding fixture.",
                        },
                        {
                            "contributor_id": contributors[1].contributor_id,
                            "delta_weight_units": 1,
                            "reason_code": "evidence_correction",
                            "explanation": "Synthetic PostgreSQL binding fixture.",
                        },
                    ],
                }
            ),
        )
        source_keys = [manifest_source_key(first), manifest_source_key(second)]
        store = PostgresAttributionStore()

        async with session_scope() as session:
            await session.execute(
                delete(AttributionReviewOverlayRecord).where(
                    AttributionReviewOverlayRecord.overlay_hash == overlay.overlay_hash
                )
            )
            await session.execute(
                delete(AttributionManifestRecord).where(
                    AttributionManifestRecord.source_key.in_(source_keys)
                )
            )
        try:
            await store.create_or_get_manifest(first)
            await store.create_or_get_manifest(second)
            await store.create_or_get_overlay(overlay)
            async with session_scope() as session:
                await session.execute(
                    update(AttributionReviewOverlayRecord)
                    .where(AttributionReviewOverlayRecord.overlay_hash == overlay.overlay_hash)
                    .values(manifest_content_hash=second.manifest_content_hash)
                )

            with pytest.raises(AttributionIntegrityError):
                await store.get_overlay(overlay.overlay_hash)
            with pytest.raises(AttributionIntegrityError):
                await store.create_or_get_overlay(overlay)
        finally:
            async with session_scope() as session:
                await session.execute(
                    delete(AttributionReviewOverlayRecord).where(
                        AttributionReviewOverlayRecord.overlay_hash == overlay.overlay_hash
                    )
                )
                await session.execute(
                    delete(AttributionManifestRecord).where(
                        AttributionManifestRecord.source_key.in_(source_keys)
                    )
                )

    asyncio.run(scenario())
