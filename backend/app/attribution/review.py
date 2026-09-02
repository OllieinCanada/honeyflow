"""Immutable review overlays and exact dry-run payout planning."""

from __future__ import annotations

from collections import defaultdict
from typing import Optional

from app.attribution.canonical import content_hash
from app.attribution.domain import AttributionDomainError, allocate_integer_units
from app.schemas.attribution import (
    AdjustedContributor,
    AppliedAttribution,
    AttributionManifest,
    CreateOverlayRequest,
    PayoutAllocation,
    PayoutPreview,
    PayoutPreviewRequest,
    ReviewOverlay,
    ReviewOverlayContent,
)


def verify_manifest(manifest: AttributionManifest) -> None:
    if content_hash(manifest.content) != manifest.manifest_content_hash:
        raise AttributionDomainError(
            "manifest_hash_mismatch",
            "manifest content does not match manifest_content_hash",
        )
    if (
        content_hash(manifest.content.attribution_configuration)
        != manifest.content.configuration_fingerprint
    ):
        raise AttributionDomainError(
            "configuration_fingerprint_mismatch",
            "manifest configuration does not match configuration_fingerprint",
        )

    contributor_ids = [
        contributor.contributor_id for contributor in manifest.content.canonical_contributors
    ]
    if len(contributor_ids) != len(set(contributor_ids)):
        raise AttributionDomainError(
            "duplicate_manifest_contributor",
            "manifest contains a duplicate canonical contributor",
        )
    contributor_id_set = set(contributor_ids)

    evidence_ids: set[str] = set()
    evidence_by_contributor: dict[str, list[str]] = defaultdict(list)
    for evidence in manifest.content.evidence_records:
        if evidence.evidence_id in evidence_ids:
            raise AttributionDomainError(
                "duplicate_manifest_evidence",
                "manifest contains a duplicate evidence identifier",
            )
        evidence_ids.add(evidence.evidence_id)
        expected_evidence_id = content_hash(
            evidence.model_dump(mode="json", exclude={"evidence_id"})
        )
        if evidence.evidence_id != expected_evidence_id:
            raise AttributionDomainError(
                "evidence_hash_mismatch",
                "manifest evidence does not match its evidence identifier",
            )
        if evidence.contributor_id not in contributor_id_set:
            raise AttributionDomainError(
                "unknown_evidence_contributor",
                "manifest evidence references an unknown contributor",
            )
        evidence_by_contributor[evidence.contributor_id].append(evidence.evidence_id)

    for contributor in manifest.content.canonical_contributors:
        if len(contributor.evidence_ids) != len(set(contributor.evidence_ids)):
            raise AttributionDomainError(
                "duplicate_contributor_evidence",
                "a contributor contains a duplicate evidence reference",
            )
        if sorted(contributor.evidence_ids) != sorted(
            evidence_by_contributor[contributor.contributor_id]
        ):
            raise AttributionDomainError(
                "contributor_evidence_mismatch",
                "contributor evidence references do not match manifest evidence",
            )

    total = sum(contributor.weight_units for contributor in manifest.content.canonical_contributors)
    if total != manifest.content.declared_weight_total_units:
        raise AttributionDomainError(
            "manifest_weight_total",
            "manifest contributor weights do not equal the declared total",
        )


def build_overlay(
    manifest: AttributionManifest,
    request: CreateOverlayRequest,
) -> ReviewOverlay:
    verify_manifest(manifest)
    contributor_weights = {
        contributor.contributor_id: contributor.weight_units
        for contributor in manifest.content.canonical_contributors
    }
    adjustment_ids = [adjustment.contributor_id for adjustment in request.adjustments]
    if len(adjustment_ids) != len(set(adjustment_ids)):
        raise AttributionDomainError(
            "duplicate_overlay_contributor",
            "an overlay may adjust each contributor at most once",
        )
    unknown = sorted(set(adjustment_ids) - set(contributor_weights))
    if unknown:
        raise AttributionDomainError(
            "unknown_overlay_contributor",
            "overlay references unknown contributors: {}".format(", ".join(unknown)),
        )
    if sum(adjustment.delta_weight_units for adjustment in request.adjustments) != 0:
        raise AttributionDomainError(
            "overlay_not_zero_sum",
            "overlay delta units must sum to zero",
        )
    for adjustment in request.adjustments:
        if contributor_weights[adjustment.contributor_id] + adjustment.delta_weight_units < 0:
            raise AttributionDomainError(
                "negative_overlay_weight",
                "overlay would make contributor {} negative".format(adjustment.contributor_id),
            )

    content = ReviewOverlayContent(
        base_manifest_hash=manifest.manifest_content_hash,
        review_reference=request.review_reference,
        adjustments=sorted(
            request.adjustments,
            key=lambda item: (
                item.contributor_id,
                item.reason_code.value,
                item.delta_weight_units,
            ),
        ),
    )
    return ReviewOverlay(content=content, overlay_hash=content_hash(content))


def apply_overlay(
    manifest: AttributionManifest,
    overlay: Optional[ReviewOverlay] = None,
) -> AppliedAttribution:
    verify_manifest(manifest)
    weights = {
        contributor.contributor_id: contributor.weight_units
        for contributor in manifest.content.canonical_contributors
    }
    names = {
        contributor.contributor_id: contributor.display_name
        for contributor in manifest.content.canonical_contributors
    }
    overlay_hash: Optional[str] = None
    if overlay is not None:
        if content_hash(overlay.content) != overlay.overlay_hash:
            raise AttributionDomainError(
                "overlay_hash_mismatch",
                "overlay content does not match overlay_hash",
            )
        if overlay.content.base_manifest_hash != manifest.manifest_content_hash:
            raise AttributionDomainError(
                "overlay_base_mismatch",
                "overlay belongs to a different base manifest",
            )
        overlay_hash = overlay.overlay_hash
        seen: set[str] = set()
        for adjustment in overlay.content.adjustments:
            if adjustment.contributor_id in seen:
                raise AttributionDomainError(
                    "duplicate_overlay_contributor",
                    "stored overlay adjusts a contributor more than once",
                )
            seen.add(adjustment.contributor_id)
            if adjustment.contributor_id not in weights:
                raise AttributionDomainError(
                    "unknown_overlay_contributor",
                    "stored overlay references an unknown contributor",
                )
            weights[adjustment.contributor_id] += adjustment.delta_weight_units

    if any(weight < 0 for weight in weights.values()):
        raise AttributionDomainError(
            "negative_overlay_weight",
            "overlay produces a negative contributor weight",
        )
    if sum(weights.values()) != manifest.content.declared_weight_total_units:
        raise AttributionDomainError(
            "overlay_weight_total",
            "overlay-adjusted weights do not equal the declared total",
        )

    return AppliedAttribution(
        base_manifest_hash=manifest.manifest_content_hash,
        overlay_hash=overlay_hash,
        contributors=[
            AdjustedContributor(
                contributor_id=contributor_id,
                display_name=names[contributor_id],
                weight_units=weights[contributor_id],
            )
            for contributor_id in sorted(weights, key=lambda key: (-weights[key], key))
        ],
    )


def build_payout_preview(
    manifest: AttributionManifest,
    request: PayoutPreviewRequest,
    overlay: Optional[ReviewOverlay] = None,
) -> PayoutPreview:
    adjusted = apply_overlay(manifest, overlay)
    positive_weights = {
        contributor.contributor_id: contributor.weight_units
        for contributor in adjusted.contributors
        if contributor.weight_units > 0
    }
    preliminary = allocate_integer_units(request.amount_minor_units, positive_weights)
    if request.minimum_payout_minor_units > 0:
        eligible = {
            contributor_id: weight
            for contributor_id, weight in positive_weights.items()
            if preliminary[contributor_id] >= request.minimum_payout_minor_units
        }
        if not eligible:
            raise AttributionDomainError(
                "payout_threshold_excludes_all",
                "minimum payout threshold excludes every contributor",
            )
    else:
        eligible = positive_weights

    exact_amounts = allocate_integer_units(request.amount_minor_units, eligible)
    allocations = [
        PayoutAllocation(
            contributor_id=contributor_id,
            weight_units=eligible[contributor_id],
            amount_minor_units=exact_amounts[contributor_id],
        )
        for contributor_id in sorted(eligible)
    ]
    idempotency_payload = {
        "calculation_version": "payout-preview/v1",
        "manifest_hash": manifest.manifest_content_hash,
        "overlay_hash": adjusted.overlay_hash,
        "currency": request.currency,
        "available_minor_units": request.amount_minor_units,
        "minimum_payout_minor_units": request.minimum_payout_minor_units,
    }
    return PayoutPreview(
        manifest_hash=manifest.manifest_content_hash,
        overlay_hash=adjusted.overlay_hash,
        currency=request.currency,
        available_minor_units=request.amount_minor_units,
        minimum_payout_minor_units=request.minimum_payout_minor_units,
        allocations=allocations,
        idempotency_key=content_hash(idempotency_payload),
    )
