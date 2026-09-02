"""Shared deterministic attribution fixtures."""

from __future__ import annotations

import os
from copy import deepcopy

import pytest

from app.schemas.attribution import CreateManifestRequest

os.environ.setdefault("DATABASE_URL", "postgresql+psycopg://localhost/honeyflow_unit")


@pytest.fixture
def manifest_payload() -> dict:
    return {
        "project_identity": "example/honeyflow-fixture",
        "source_repository_url": "https://github.com/example/honeyflow-fixture.git",
        "source_commit_sha": "f" * 40,
        "dependencies": [
            {
                "identity": "package:example-core",
                "version": "1.2.3",
                "source_commit_sha": "e" * 40,
            },
            {"identity": "package:example-ui", "version": "4.5.6"},
        ],
        "records": [
            {
                "record_id": "commit-a",
                "commit_sha": "a" * 40,
                "author": {
                    "display_name": "Alex Example",
                    "github_login": "alex-example",
                    "email": "alex@example.invalid",
                },
                "files": [
                    {"path": "src/core.py", "additions": 15, "deletions": 3},
                    {"path": "src/types.py", "additions": 5, "deletions": 1},
                ],
            },
            {
                "record_id": "commit-b",
                "commit_sha": "b" * 40,
                "author": {
                    "display_name": "Blair Example",
                    "github_login": "blair-example",
                },
                "coauthors": [
                    {
                        "display_name": "Alex Example",
                        "github_login": "alex-example",
                    }
                ],
                "files": [
                    {"path": "src/api.py", "additions": 9, "deletions": 2},
                ],
            },
            {
                "record_id": "commit-c-generated",
                "commit_sha": "c" * 40,
                "author": {
                    "display_name": "Build Bot",
                    "github_login": "dependabot[bot]",
                },
                "files": [
                    {"path": "dist/bundle.js", "additions": 1000, "deletions": 0},
                    {"path": "src/dependency_version.py", "additions": 1, "deletions": 0},
                ],
            },
        ],
    }


@pytest.fixture
def manifest_request(manifest_payload: dict) -> CreateManifestRequest:
    return CreateManifestRequest.model_validate(deepcopy(manifest_payload))
