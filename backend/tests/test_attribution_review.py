"""Immutable overlay and exact payout-preview tests."""

from __future__ import annotations

from copy import deepcopy

import pytest

from app.attribution.domain import AttributionDomainError, build_manifest
from app.attribution.review import apply_overlay, build_overlay, build_payout_preview
from app.schemas.attribution import (
    CreateManifestRequest,
    CreateOverlayRequest,
    PayoutPreviewRequest,
)


def _overlay_request(manifest) -> CreateOverlayRequest:
    contributors = manifest.content.canonical_contributors
    return CreateOverlayRequest.model_validate(
        {
            "review_reference": "review:synthetic-001",
            "adjustments": [
                {
                    "contributor_id": contributors[0].contributor_id,
                    "delta_weight_units": -100,
                    "reason_code": "evidence_correction",
                    "explanation": "Synthetic correction removes duplicated evidence units.",
                },
                {
                    "contributor_id": contributors[1].contributor_id,
                    "delta_weight_units": 100,
                    "reason_code": "evidence_correction",
                    "explanation": "Synthetic correction assigns the evidence units once.",
                },
            ],
        }
    )


def test_overlay_is_zero_sum_idempotent_and_does_not_mutate_base(manifest_request) -> None:
    manifest = build_manifest(manifest_request)
    original = manifest.model_copy(deep=True)
    overlay = build_overlay(manifest, _overlay_request(manifest))

    first = apply_overlay(manifest, overlay)
    second = apply_overlay(manifest, overlay)

    assert first == second
    assert manifest == original
    assert sum(item.weight_units for item in first.contributors) == 1_000_000


def test_invalid_nonzero_sum_overlay_is_rejected(manifest_request) -> None:
    manifest = build_manifest(manifest_request)
    request = _overlay_request(manifest)
    payload = request.model_dump(mode="json")
    payload["adjustments"][1]["delta_weight_units"] = 99

    with pytest.raises(AttributionDomainError) as caught:
        build_overlay(manifest, CreateOverlayRequest.model_validate(payload))

    assert caught.value.code == "overlay_not_zero_sum"


def test_overlay_cannot_make_a_contributor_negative(manifest_request) -> None:
    manifest = build_manifest(manifest_request)
    request = _overlay_request(manifest)
    payload = request.model_dump(mode="json")
    first_weight = manifest.content.canonical_contributors[0].weight_units
    payload["adjustments"][0]["delta_weight_units"] = -(first_weight + 1)
    payload["adjustments"][1]["delta_weight_units"] = first_weight + 1

    with pytest.raises(AttributionDomainError) as caught:
        build_overlay(manifest, CreateOverlayRequest.model_validate(payload))

    assert caught.value.code == "negative_overlay_weight"


def test_payout_preview_is_exact_nonnegative_and_deterministic(manifest_request) -> None:
    manifest = build_manifest(manifest_request)
    request = PayoutPreviewRequest(
        amount_minor_units=10_001,
        currency="usd",
        minimum_payout_minor_units=0,
    )

    first = build_payout_preview(manifest, request)
    second = build_payout_preview(manifest, request)

    assert first == second
    assert first.currency == "USD"
    assert all(item.amount_minor_units >= 0 for item in first.allocations)
    assert sum(item.amount_minor_units for item in first.allocations) == 10_001


def test_dust_tie_break_is_lexically_stable(manifest_payload: dict) -> None:
    payload = deepcopy(manifest_payload)
    payload["records"] = [
        {
            "record_id": "one",
            "commit_sha": "1" * 40,
            "author": {"display_name": "One", "github_login": "one"},
            "files": [{"path": "one.py"}],
        },
        {
            "record_id": "two",
            "commit_sha": "2" * 40,
            "author": {"display_name": "Two", "github_login": "two"},
            "files": [{"path": "two.py"}],
        },
    ]
    manifest = build_manifest(CreateManifestRequest.model_validate(payload))
    preview = build_payout_preview(
        manifest,
        PayoutPreviewRequest(amount_minor_units=1, currency="USD"),
    )

    amounts = {item.contributor_id: item.amount_minor_units for item in preview.allocations}
    assert amounts == {"github:one": 1, "github:two": 0}


def test_threshold_is_explicit_and_preserves_total(manifest_request) -> None:
    manifest = build_manifest(manifest_request)
    preview = build_payout_preview(
        manifest,
        PayoutPreviewRequest(
            amount_minor_units=100,
            currency="USD",
            minimum_payout_minor_units=60,
        ),
    )

    assert len(preview.allocations) == 1
    assert preview.allocations[0].amount_minor_units == 100
