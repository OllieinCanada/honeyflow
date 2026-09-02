import asyncio
import copy
import os
from contextlib import asynccontextmanager
from types import SimpleNamespace

import pytest

from app.services.provenance import build_manifest


def _require_runtime():
    for package in ("sqlalchemy", "fastapi", "pydantic_settings", "psycopg", "playwright"):
        pytest.importorskip(package)
    os.environ.setdefault(
        "DATABASE_URL", "postgresql://provenance:unused@127.0.0.1:9/provenance"
    )


def _manifest():
    return build_manifest(
        repository_url="https://github.com/example/project",
        source_commit_sha="a" * 40,
        attribution={"alice": 1.0},
        graph={
            "nodes": [{"id": "repo"}, {"id": "alice"}],
            "edges": [{"source": "repo", "target": "alice", "weight": 1.0}],
        },
    )


def test_public_route_returns_manifest_and_shared_verification(monkeypatch):
    _require_runtime()
    from app.routes import provenance as route

    manifest = _manifest()
    snapshot = SimpleNamespace(
        sequence=1,
        digest=manifest.manifest_digest,
        previous_digest=None,
        source_commit_sha=manifest.source_commit_sha,
        manifest=manifest.model_dump(mode="json"),
    )

    @asynccontextmanager
    async def fake_session():
        yield object()

    async def fake_load(_session, slug, digest):
        assert slug == "project"
        assert digest is None
        return snapshot

    async def fake_public_repository(owner, name):
        assert (owner, name) == ("example", "project")

    monkeypatch.setattr(route, "get_session", fake_session)
    monkeypatch.setattr(route, "load_snapshot", fake_load)
    monkeypatch.setattr(route, "require_public_repository", fake_public_repository)
    response = asyncio.run(route.get_project_provenance("project", None))
    assert response["verification"]["valid"] is True
    assert response["storage_consistent"] is True
    assert response["manifest"]["manifest_digest"] == manifest.manifest_digest


def test_disabled_sidecar_does_not_mutate_or_store(monkeypatch):
    _require_runtime()
    from app.routes import stream
    from app.services import provenance_store

    calls = 0

    async def forbidden_store(*_args, **_kwargs):
        nonlocal calls
        calls += 1

    monkeypatch.setattr(provenance_store, "save_snapshot", forbidden_store)
    monkeypatch.setattr(stream.settings, "provenance_enabled", False)
    result = {
        "attribution": {"alice": 0.75, "bob": 0.25},
        "top_contributors": [
            {"name": "alice", "percentage": "75.0%"},
            {"name": "bob", "percentage": "25.0%"},
        ],
        "_provenance": {"must": "not be inspected"},
    }
    project = SimpleNamespace(
        id=1,
        attribution=copy.deepcopy(result["attribution"]),
        top_contributors=copy.deepcopy(result["top_contributors"]),
    )
    before_result = copy.deepcopy(result)
    before_project = copy.deepcopy(project.__dict__)
    asyncio.run(stream._store_provenance_sidecar(object(), project, result))
    assert calls == 0
    assert result == before_result
    assert project.__dict__ == before_project
