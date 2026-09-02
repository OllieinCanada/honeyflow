"""Content-address binding tests for PostgreSQL attribution rows."""

from __future__ import annotations

from copy import deepcopy
from types import SimpleNamespace

import pytest

from app.attribution.domain import build_manifest
from app.attribution.postgres import _manifest_from_record, _overlay_from_record
from app.attribution.review import build_overlay
from app.attribution.store import AttributionIntegrityError, manifest_source_key
from app.schemas.attribution import (
    AttributionManifest,
    CreateManifestRequest,
    CreateOverlayRequest,
    ReviewOverlay,
)


def _manifest_row(manifest: AttributionManifest) -> SimpleNamespace:
    content = manifest.content
    return SimpleNamespace(
        manifest_content_hash=manifest.manifest_content_hash,
        source_key=manifest_source_key(manifest),
        project_identity=content.project_identity,
        source_repository_url=content.source_repository_url,
        source_commit_sha=content.source_commit_sha,
        algorithm_version=content.algorithm_version,
        configuration_fingerprint=content.configuration_fingerprint,
        manifest_json=manifest.model_dump(mode="json"),
    )


def _review_overlay(manifest: AttributionManifest) -> ReviewOverlay:
    contributors = manifest.content.canonical_contributors
    return build_overlay(
        manifest,
        CreateOverlayRequest.model_validate(
            {
                "review_reference": "review:row-binding",
                "adjustments": [
                    {
                        "contributor_id": contributors[0].contributor_id,
                        "delta_weight_units": -1,
                        "reason_code": "evidence_correction",
                        "explanation": "Synthetic row-binding fixture.",
                    },
                    {
                        "contributor_id": contributors[1].contributor_id,
                        "delta_weight_units": 1,
                        "reason_code": "evidence_correction",
                        "explanation": "Synthetic row-binding fixture.",
                    },
                ],
            }
        ),
    )


def test_manifest_row_rejects_self_consistent_json_under_another_row_hash(
    manifest_payload: dict,
) -> None:
    original = build_manifest(CreateManifestRequest.model_validate(manifest_payload))
    changed_payload = deepcopy(manifest_payload)
    changed_payload["source_commit_sha"] = "9" * 40
    another = build_manifest(CreateManifestRequest.model_validate(changed_payload))
    row = _manifest_row(original)
    row.manifest_json = another.model_dump(mode="json")

    with pytest.raises(AttributionIntegrityError) as caught:
        _manifest_from_record(row)

    assert caught.value.code == "manifest_row_binding_error"


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("source_key", "0" * 64),
        ("project_identity", "corrupt/project"),
        ("source_repository_url", "https://github.com/corrupt/project"),
        ("source_commit_sha", "0" * 40),
        ("algorithm_version", "corrupt-algorithm/v1"),
        ("configuration_fingerprint", "0" * 64),
    ],
)
def test_manifest_row_rejects_corrupt_denormalized_binding(
    manifest_request,
    field: str,
    value: str,
) -> None:
    manifest = build_manifest(manifest_request)
    row = _manifest_row(manifest)
    setattr(row, field, value)

    with pytest.raises(AttributionIntegrityError) as caught:
        _manifest_from_record(row)

    assert caught.value.code == "manifest_row_binding_error"


def test_manifest_row_rejects_wrong_requested_hash(manifest_request) -> None:
    manifest = build_manifest(manifest_request)
    row = _manifest_row(manifest)

    with pytest.raises(AttributionIntegrityError) as caught:
        _manifest_from_record(row, expected_manifest_hash="0" * 64)

    assert caught.value.code == "manifest_row_binding_error"


def test_overlay_row_rejects_json_under_another_row_hash(manifest_request) -> None:
    manifest = build_manifest(manifest_request)
    overlay = _review_overlay(manifest)
    row = SimpleNamespace(
        overlay_hash="0" * 64,
        manifest_content_hash=manifest.manifest_content_hash,
        overlay_json=overlay.model_dump(mode="json"),
    )

    with pytest.raises(AttributionIntegrityError) as caught:
        _overlay_from_record(row)

    assert caught.value.code == "overlay_row_binding_error"


def test_overlay_row_rejects_foreign_key_different_from_json_base(
    manifest_request,
) -> None:
    manifest = build_manifest(manifest_request)
    overlay = _review_overlay(manifest)
    row = SimpleNamespace(
        overlay_hash=overlay.overlay_hash,
        manifest_content_hash="0" * 64,
        overlay_json=overlay.model_dump(mode="json"),
    )

    with pytest.raises(AttributionIntegrityError) as caught:
        _overlay_from_record(row, expected_overlay_hash=overlay.overlay_hash)

    assert caught.value.code == "overlay_row_binding_error"
