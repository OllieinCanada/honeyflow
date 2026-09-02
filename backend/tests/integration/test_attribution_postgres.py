"""PostgreSQL concurrency test; enabled when TEST_DATABASE_URL is configured."""

from __future__ import annotations

import asyncio
import os

import pytest

from app.attribution.domain import build_manifest
from app.schemas.attribution import CreateManifestRequest

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
