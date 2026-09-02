"""API validation and authorization tests using the in-memory store."""

from __future__ import annotations

from copy import deepcopy

from fastapi import FastAPI
from fastapi.testclient import TestClient
from pydantic import SecretStr

from app.attribution.service import AttributionService
from app.attribution.store import InMemoryAttributionStore
from app.config import settings
from app.routes import attribution


def _client() -> TestClient:
    app = FastAPI()
    service = AttributionService(InMemoryAttributionStore())
    app.include_router(attribution.router)
    app.dependency_overrides[attribution.get_attribution_service] = lambda: service
    return TestClient(app)


def test_write_api_fails_closed_without_configured_admin_token(
    monkeypatch,
    manifest_payload: dict,
) -> None:
    monkeypatch.setattr(settings, "attribution_admin_token", None)
    with _client() as client:
        response = client.post("/attribution/manifests", json=manifest_payload)

    assert response.status_code == 503
    assert response.json()["detail"]["code"] == "attribution_admin_not_configured"


def test_write_api_rejects_missing_or_wrong_admin_token(
    monkeypatch,
    manifest_payload: dict,
) -> None:
    monkeypatch.setattr(
        settings,
        "attribution_admin_token",
        SecretStr("synthetic-test-token"),
    )
    with _client() as client:
        missing = client.post("/attribution/manifests", json=manifest_payload)
        wrong = client.post(
            "/attribution/manifests",
            headers={"X-Attribution-Admin-Token": "wrong"},
            json=manifest_payload,
        )

    assert missing.status_code == 401
    assert wrong.status_code == 401
    assert missing.json()["detail"]["code"] == "invalid_attribution_admin_token"


def test_manifest_create_is_idempotent_and_payout_is_read_only(
    monkeypatch,
    manifest_payload: dict,
) -> None:
    monkeypatch.setattr(
        settings,
        "attribution_admin_token",
        SecretStr("synthetic-test-token"),
    )
    headers = {"X-Attribution-Admin-Token": "synthetic-test-token"}
    with _client() as client:
        first = client.post(
            "/attribution/manifests",
            headers=headers,
            json=manifest_payload,
        )
        second = client.post(
            "/attribution/manifests",
            headers=headers,
            json=manifest_payload,
        )
        manifest_hash = first.json()["manifest"]["manifest_content_hash"]
        read = client.get(
            "/attribution/manifests/{}".format(manifest_hash),
            headers=headers,
        )
        preview = client.post(
            "/attribution/manifests/{}/payout-previews".format(manifest_hash),
            headers=headers,
            json={"amount_minor_units": 12_345, "currency": "usd"},
        )

    assert first.status_code == 201
    assert "alex@example.invalid" not in first.text
    assert first.json()["created"] is True
    assert second.status_code == 200
    assert second.json()["created"] is False
    assert read.status_code == 200
    assert preview.status_code == 200
    assert preview.json()["currency"] == "USD"
    assert (
        sum(allocation["amount_minor_units"] for allocation in preview.json()["allocations"])
        == 12_345
    )


def test_divergent_evidence_for_same_source_returns_conflict(
    monkeypatch,
    manifest_payload: dict,
) -> None:
    monkeypatch.setattr(
        settings,
        "attribution_admin_token",
        SecretStr("synthetic-test-token"),
    )
    headers = {"X-Attribution-Admin-Token": "synthetic-test-token"}
    changed_payload = deepcopy(manifest_payload)
    changed_payload["records"][0]["files"][0]["additions"] += 1
    with _client() as client:
        created = client.post(
            "/attribution/manifests",
            headers=headers,
            json=manifest_payload,
        )
        conflict = client.post(
            "/attribution/manifests",
            headers=headers,
            json=changed_payload,
        )

    assert created.status_code == 201
    assert conflict.status_code == 409
    assert conflict.json()["detail"]["code"] == "manifest_source_conflict"


def test_invalid_overlay_returns_structured_domain_error(
    monkeypatch,
    manifest_payload: dict,
) -> None:
    monkeypatch.setattr(
        settings,
        "attribution_admin_token",
        SecretStr("synthetic-test-token"),
    )
    headers = {"X-Attribution-Admin-Token": "synthetic-test-token"}
    with _client() as client:
        created = client.post(
            "/attribution/manifests",
            headers=headers,
            json=manifest_payload,
        )
        manifest = created.json()["manifest"]
        contributors = manifest["content"]["canonical_contributors"]
        response = client.post(
            "/attribution/manifests/{}/overlays".format(manifest["manifest_content_hash"]),
            headers=headers,
            json={
                "review_reference": "review:invalid-nonzero-sum",
                "adjustments": [
                    {
                        "contributor_id": contributors[0]["contributor_id"],
                        "delta_weight_units": -10,
                        "reason_code": "evidence_correction",
                        "explanation": "Synthetic test adjustment.",
                    },
                    {
                        "contributor_id": contributors[1]["contributor_id"],
                        "delta_weight_units": 9,
                        "reason_code": "evidence_correction",
                        "explanation": "Synthetic test adjustment.",
                    },
                ],
            },
        )

    assert response.status_code == 422
    assert response.json()["detail"]["code"] == "overlay_not_zero_sum"


def test_unknown_manifest_returns_structured_not_found(monkeypatch) -> None:
    monkeypatch.setattr(
        settings,
        "attribution_admin_token",
        SecretStr("synthetic-test-token"),
    )
    with _client() as client:
        response = client.get(
            "/attribution/manifests/{}".format("0" * 64),
            headers={"X-Attribution-Admin-Token": "synthetic-test-token"},
        )

    assert response.status_code == 404
    assert response.json()["detail"]["code"] == "manifest_not_found"


def test_api_rejects_unknown_request_fields(
    monkeypatch,
    manifest_payload: dict,
) -> None:
    monkeypatch.setattr(
        settings,
        "attribution_admin_token",
        SecretStr("synthetic-test-token"),
    )
    payload = {**manifest_payload, "model_generated_weight": 0.99}
    with _client() as client:
        response = client.post(
            "/attribution/manifests",
            headers={"X-Attribution-Admin-Token": "synthetic-test-token"},
            json=payload,
        )

    assert response.status_code == 422


def test_api_rejects_coerced_money_types(
    monkeypatch,
    manifest_payload: dict,
) -> None:
    monkeypatch.setattr(
        settings,
        "attribution_admin_token",
        SecretStr("synthetic-test-token"),
    )
    headers = {"X-Attribution-Admin-Token": "synthetic-test-token"}
    with _client() as client:
        created = client.post(
            "/attribution/manifests",
            headers=headers,
            json=manifest_payload,
        )
        manifest_hash = created.json()["manifest"]["manifest_content_hash"]
        response = client.post(
            f"/attribution/manifests/{manifest_hash}/payout-previews",
            headers=headers,
            json={"amount_minor_units": "1000", "currency": "USD"},
        )

    assert response.status_code == 422


def test_read_api_requires_admin_token(monkeypatch) -> None:
    monkeypatch.setattr(
        settings,
        "attribution_admin_token",
        SecretStr("synthetic-test-token"),
    )
    with _client() as client:
        response = client.get("/attribution/manifests/{}".format("0" * 64))

    assert response.status_code == 401
