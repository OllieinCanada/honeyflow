"""Persistence for immutable provenance snapshots."""

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.project import Project
from app.models.provenance import ProvenanceSnapshot
from app.schemas.provenance import JuryEventV1, ProvenanceManifestV1
from app.services.provenance import (
    append_jury_events,
    prepare_snapshot,
    verify_manifest,
)


async def save_snapshot(
    session: AsyncSession,
    project_id: int,
    manifest: ProvenanceManifestV1,
) -> ProvenanceSnapshot:
    """Append a verified snapshot while serializing writers on the project row."""
    if not verify_manifest(manifest).valid:
        raise ValueError("refusing to store an invalid provenance manifest")
    locked_project_id = await session.scalar(
        select(Project.id).where(Project.id == project_id).with_for_update()
    )
    if locked_project_id is None:
        raise ValueError("project does not exist")

    duplicate = await session.scalar(
        select(ProvenanceSnapshot).where(
            ProvenanceSnapshot.project_id == project_id,
            ProvenanceSnapshot.digest == manifest.manifest_digest,
        )
    )
    if duplicate is not None:
        return duplicate

    latest = await session.scalar(
        select(ProvenanceSnapshot)
        .where(ProvenanceSnapshot.project_id == project_id)
        .order_by(ProvenanceSnapshot.sequence.desc())
        .limit(1)
    )
    manifest, sequence = prepare_snapshot(
        manifest,
        latest.digest if latest is not None else None,
        latest.sequence if latest is not None else None,
    )
    stored = ProvenanceSnapshot(
        project_id=project_id,
        sequence=sequence,
        source_commit_sha=manifest.source_commit_sha,
        digest=manifest.manifest_digest,
        previous_digest=manifest.previous_digest,
        manifest=manifest.model_dump(mode="json"),
    )
    session.add(stored)
    await session.flush()
    return stored


async def append_events_snapshot(
    session: AsyncSession,
    project_id: int,
    events: list[JuryEventV1],
) -> ProvenanceSnapshot | None:
    """Append jury events by inserting a new snapshot; prior JSON is untouched."""
    if not events:
        return None
    locked_project_id = await session.scalar(
        select(Project.id).where(Project.id == project_id).with_for_update()
    )
    if locked_project_id is None:
        return None
    latest = await session.scalar(
        select(ProvenanceSnapshot)
        .where(ProvenanceSnapshot.project_id == project_id)
        .order_by(ProvenanceSnapshot.sequence.desc())
        .limit(1)
    )
    if latest is None:
        return None
    manifest = ProvenanceManifestV1.model_validate(latest.manifest)
    updated = append_jury_events(manifest, events)
    return await save_snapshot(session, project_id, updated)


async def load_snapshot(
    session: AsyncSession, project_slug: str, digest: str | None = None
) -> ProvenanceSnapshot | None:
    statement = (
        select(ProvenanceSnapshot)
        .join(Project, Project.id == ProvenanceSnapshot.project_id)
        .where(Project.slug == project_slug)
    )
    if digest is not None:
        statement = statement.where(ProvenanceSnapshot.digest == digest)
    else:
        statement = statement.order_by(ProvenanceSnapshot.sequence.desc()).limit(1)
    return await session.scalar(statement)
