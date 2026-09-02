"""Public-read provenance routes, consistent with public project detail routes."""

from fastapi import APIRouter, HTTPException, Query
from httpx import HTTPError

from app.database import get_session
from app.schemas.provenance import ProvenanceManifestV1
from app.services.github import require_public_repository
from app.services.provenance import verify_manifest
from app.services.provenance_store import load_snapshot

router = APIRouter(prefix="/projects", tags=["provenance"])


@router.get("/{slug}/provenance")
async def get_project_provenance(
    slug: str,
    digest: str | None = Query(default=None, pattern=r"^[0-9a-f]{64}$"),
):
    async with get_session() as session:
        snapshot = await load_snapshot(session, slug, digest)
    if snapshot is None:
        raise HTTPException(status_code=404, detail="Provenance manifest not found")
    verification = verify_manifest(snapshot.manifest)
    if not verification.valid:
        return {
            "sequence": snapshot.sequence,
            "manifest": None,
            "verification": verification.model_dump(mode="json"),
            "storage_consistent": False,
            "assurance": "Stored provenance failed integrity verification.",
        }
    manifest = ProvenanceManifestV1.model_validate(snapshot.manifest)
    try:
        await require_public_repository(
            manifest.repository.owner,
            manifest.repository.name,
        )
    except (HTTPError, ValueError, TypeError, KeyError) as exc:
        raise HTTPException(
            status_code=503,
            detail="Repository visibility could not be confirmed",
        ) from exc
    storage_consistent = (
        snapshot.digest == manifest.manifest_digest
        and snapshot.previous_digest == manifest.previous_digest
        and snapshot.source_commit_sha == manifest.source_commit_sha
    )
    return {
        "sequence": snapshot.sequence,
        "manifest": manifest.model_dump(mode="json"),
        "verification": verification.model_dump(mode="json"),
        "storage_consistent": storage_consistent,
        "assurance": (
            "Digest verification detects changes; it is not a signature "
            "or identity proof."
        ),
    }
