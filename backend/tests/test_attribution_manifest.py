"""Determinism, identity, and evidence tests for Attribution Manifest V1."""

from __future__ import annotations

import hashlib
import json
from copy import deepcopy

import pytest

from app.attribution.canonical import canonical_json_bytes, content_hash
from app.attribution.domain import AttributionDomainError, build_manifest
from app.schemas.attribution import (
    DECLARED_WEIGHT_TOTAL_UNITS,
    CreateManifestRequest,
    EvidenceReason,
    ExclusionReason,
)


def test_canonical_json_golden_fixture() -> None:
    payload = {"z": [3, 2, 1], "a": {"unicode": "café", "enabled": True}}
    expected = b'{"a":{"enabled":true,"unicode":"caf\xc3\xa9"},"z":[3,2,1]}'

    assert canonical_json_bytes(payload) == expected
    assert content_hash(payload) == hashlib.sha256(expected).hexdigest()


def test_canonical_json_rejects_binary_float() -> None:
    with pytest.raises(TypeError, match="floating-point"):
        canonical_json_bytes({"weight": 0.1})


def test_manifest_is_stable_under_input_ordering(manifest_payload: dict) -> None:
    shuffled = deepcopy(manifest_payload)
    shuffled["records"].reverse()
    shuffled["dependencies"].reverse()
    for record in shuffled["records"]:
        record["files"].reverse()
        record.get("coauthors", []).reverse()

    first = build_manifest(CreateManifestRequest.model_validate(manifest_payload))
    second = build_manifest(CreateManifestRequest.model_validate(shuffled))

    assert first == second
    assert first.manifest_content_hash == second.manifest_content_hash


def test_weights_are_exact_nonnegative_and_repeatable(manifest_request) -> None:
    first = build_manifest(manifest_request)
    second = build_manifest(manifest_request)
    weights = [item.weight_units for item in first.content.canonical_contributors]

    assert first == second
    assert all(weight >= 0 for weight in weights)
    assert sum(weights) == DECLARED_WEIGHT_TOTAL_UNITS


def test_attribution_relevant_change_changes_hash(manifest_payload: dict) -> None:
    original = build_manifest(CreateManifestRequest.model_validate(manifest_payload))
    changed_payload = deepcopy(manifest_payload)
    changed_payload["records"][0]["files"][0]["additions"] += 1
    changed = build_manifest(CreateManifestRequest.model_validate(changed_payload))

    assert changed.manifest_content_hash != original.manifest_content_hash


def test_per_file_evidence_change_changes_hash_when_aggregate_is_unchanged(
    manifest_payload: dict,
) -> None:
    original = build_manifest(CreateManifestRequest.model_validate(manifest_payload))
    changed_payload = deepcopy(manifest_payload)
    changed_payload["records"][0]["files"][0]["additions"] += 1
    changed_payload["records"][0]["files"][1]["deletions"] -= 1
    changed = build_manifest(CreateManifestRequest.model_validate(changed_payload))

    assert changed.manifest_content_hash != original.manifest_content_hash


def test_capped_file_change_has_an_explicit_reason(manifest_payload: dict) -> None:
    payload = deepcopy(manifest_payload)
    payload["rules"] = {"max_lines_per_file": 2}

    manifest = build_manifest(CreateManifestRequest.model_validate(payload))
    capped_evidence = [
        item
        for item in manifest.content.evidence_records
        if EvidenceReason.FILE_CHANGE_CAPPED in item.reason_codes
    ]

    assert capped_evidence
    assert all(
        file_record.counted_lines <= 2
        for evidence in capped_evidence
        for file_record in evidence.included_files
    )


def test_changed_source_revision_changes_manifest_hash(manifest_payload: dict) -> None:
    original = build_manifest(CreateManifestRequest.model_validate(manifest_payload))
    changed_payload = deepcopy(manifest_payload)
    changed_payload["source_commit_sha"] = "9" * 40
    changed = build_manifest(CreateManifestRequest.model_validate(changed_payload))

    assert changed.manifest_content_hash != original.manifest_content_hash


def test_raw_email_is_not_exposed_in_manifest(manifest_request) -> None:
    manifest = build_manifest(manifest_request)
    serialized = json.dumps(manifest.model_dump(mode="json"), sort_keys=True)

    assert "alex@example.invalid" not in serialized
    assert "email-sha256:" not in serialized  # the verified GitHub login is canonical


def test_applied_configuration_is_replayable_and_matches_fingerprint(
    manifest_request,
) -> None:
    manifest = build_manifest(manifest_request)

    assert manifest.content.attribution_configuration.rules == manifest_request.rules
    assert manifest.content.configuration_fingerprint == content_hash(
        manifest.content.attribution_configuration
    )


def test_generated_vendored_binary_bot_merge_and_duplicate_are_explained(
    manifest_payload: dict,
) -> None:
    payload = deepcopy(manifest_payload)
    payload["records"].extend(
        [
            {
                "record_id": "all-exclusions",
                "commit_sha": "1" * 40,
                "author": {
                    "display_name": "Casey Example",
                    "github_login": "casey-example",
                },
                "files": [
                    {"path": "vendor/library.py", "additions": 10},
                    {"path": "assets/logo.png", "binary": True},
                    {"path": "public/app.min.js", "additions": 100},
                    {"path": "src/kept.py", "additions": 1},
                ],
            },
            {
                "record_id": "merge-record",
                "commit_sha": "2" * 40,
                "author": {
                    "display_name": "Casey Example",
                    "github_login": "casey-example",
                },
                "is_merge": True,
                "files": [{"path": "src/merge.py", "additions": 1}],
            },
            {
                "record_id": "duplicate-record",
                "commit_sha": "a" * 40,
                "author": {
                    "display_name": "Casey Example",
                    "github_login": "casey-example",
                },
                "files": [{"path": "src/duplicate.py", "additions": 1}],
            },
        ]
    )

    manifest = build_manifest(CreateManifestRequest.model_validate(payload))
    reasons = {item.reason_code for item in manifest.content.exclusions}

    assert {
        ExclusionReason.BINARY_FILE,
        ExclusionReason.BOT_IDENTITY,
        ExclusionReason.DUPLICATE_COMMIT,
        ExclusionReason.GENERATED_FILE,
        ExclusionReason.MERGE_COMMIT,
        ExclusionReason.VENDORED_FILE,
    } <= reasons


def test_same_display_name_is_not_silently_merged(manifest_payload: dict) -> None:
    payload = deepcopy(manifest_payload)
    payload["records"] = [
        {
            "record_id": "one",
            "commit_sha": "1" * 40,
            "author": {"display_name": "Shared Name", "github_login": "identity-one"},
            "files": [{"path": "one.py", "additions": 1}],
        },
        {
            "record_id": "two",
            "commit_sha": "2" * 40,
            "author": {"display_name": "Shared Name", "github_login": "identity-two"},
            "files": [{"path": "two.py", "additions": 1}],
        },
    ]

    manifest = build_manifest(CreateManifestRequest.model_validate(payload))

    assert len(manifest.content.canonical_contributors) == 2
    assert all(item.uncertain_aliases for item in manifest.content.canonical_contributors)
    assert all(
        EvidenceReason.POTENTIAL_ALIAS in evidence.reason_codes
        for evidence in manifest.content.evidence_records
    )


def test_explicit_strong_aliases_merge_with_evidence(manifest_payload: dict) -> None:
    payload = deepcopy(manifest_payload)
    payload["records"] = [
        {
            "record_id": "one",
            "commit_sha": "1" * 40,
            "author": {"display_name": "Alex", "github_login": "alpha-alias"},
            "files": [{"path": "one.py", "additions": 1}],
        },
        {
            "record_id": "two",
            "commit_sha": "2" * 40,
            "author": {"display_name": "Alex", "github_login": "zeta-canonical"},
            "files": [{"path": "two.py", "additions": 1}],
        },
    ]
    payload["aliases"] = [
        {
            "canonical": {
                "display_name": "Alex",
                "github_login": "zeta-canonical",
            },
            "aliases": [{"display_name": "Alex", "github_login": "alpha-alias"}],
        }
    ]

    manifest = build_manifest(CreateManifestRequest.model_validate(payload))

    assert len(manifest.content.canonical_contributors) == 1
    assert manifest.content.canonical_contributors[0].contributor_id == ("github:zeta-canonical")
    assert manifest.content.canonical_contributors[0].github_logins == [
        "alpha-alias",
        "zeta-canonical",
    ]
    assert all(
        EvidenceReason.EXPLICIT_ALIAS in evidence.reason_codes
        for evidence in manifest.content.evidence_records
    )


def test_alias_canonical_choice_is_part_of_applied_configuration(
    manifest_payload: dict,
) -> None:
    payload = deepcopy(manifest_payload)
    payload["records"] = [
        {
            "record_id": "one",
            "commit_sha": "1" * 40,
            "author": {"display_name": "Alex", "github_login": "alpha-alias"},
            "files": [{"path": "one.py", "additions": 1}],
        },
        {
            "record_id": "two",
            "commit_sha": "2" * 40,
            "author": {"display_name": "Alex", "github_login": "zeta-canonical"},
            "files": [{"path": "two.py", "additions": 1}],
        },
    ]
    payload["aliases"] = [
        {
            "canonical": {"display_name": "Alex", "github_login": "zeta-canonical"},
            "aliases": [{"display_name": "Alex", "github_login": "alpha-alias"}],
        }
    ]
    declared = build_manifest(CreateManifestRequest.model_validate(payload))

    payload["aliases"][0] = {
        "canonical": {"display_name": "Alex", "github_login": "alpha-alias"},
        "aliases": [{"display_name": "Alex", "github_login": "zeta-canonical"}],
    }
    reversed_choice = build_manifest(CreateManifestRequest.model_validate(payload))

    assert declared.content.attribution_configuration.alias_rules[0].canonical_token == (
        "github:zeta-canonical"
    )
    assert reversed_choice.content.attribution_configuration.alias_rules[0].canonical_token == (
        "github:alpha-alias"
    )
    assert (
        declared.content.configuration_fingerprint
        != reversed_choice.content.configuration_fingerprint
    )
    assert declared.manifest_content_hash != reversed_choice.manifest_content_hash


def test_alias_and_member_order_do_not_change_manifest(manifest_payload: dict) -> None:
    payload = deepcopy(manifest_payload)
    payload["records"] = [
        {
            "record_id": "canonical",
            "commit_sha": "1" * 40,
            "author": {"display_name": "Alex", "github_login": "canonical"},
            "files": [{"path": "one.py", "additions": 1}],
        },
        {
            "record_id": "alias-one",
            "commit_sha": "2" * 40,
            "author": {"display_name": "Alex", "github_login": "alias-one"},
            "files": [{"path": "two.py", "additions": 1}],
        },
        {
            "record_id": "alias-two",
            "commit_sha": "3" * 40,
            "author": {"display_name": "Alex", "github_login": "alias-two"},
            "files": [{"path": "three.py", "additions": 1}],
        },
    ]
    payload["aliases"] = [
        {
            "canonical": {"display_name": "Alex", "github_login": "canonical"},
            "aliases": [
                {"display_name": "Alex", "github_login": "alias-one"},
                {"display_name": "Alex", "github_login": "alias-two"},
            ],
        }
    ]
    reordered = deepcopy(payload)
    reordered["records"].reverse()
    reordered["aliases"][0]["aliases"].reverse()

    first = build_manifest(CreateManifestRequest.model_validate(payload))
    second = build_manifest(CreateManifestRequest.model_validate(reordered))

    assert first == second


def test_conflicting_alias_declarations_are_rejected(manifest_payload: dict) -> None:
    payload = deepcopy(manifest_payload)
    payload["aliases"] = [
        {
            "canonical": {"display_name": "One", "github_login": "canonical-one"},
            "aliases": [{"display_name": "Shared", "github_login": "shared-alias"}],
        },
        {
            "canonical": {"display_name": "Two", "github_login": "canonical-two"},
            "aliases": [{"display_name": "Shared", "github_login": "shared-alias"}],
        },
    ]

    with pytest.raises(AttributionDomainError) as caught:
        build_manifest(CreateManifestRequest.model_validate(payload))

    assert caught.value.code == "ambiguous_alias"


def test_multiple_email_aliases_are_hashed_before_manifest_storage(
    manifest_payload: dict,
) -> None:
    payload = deepcopy(manifest_payload)
    payload["records"] = [
        {
            "record_id": "old-email",
            "commit_sha": "1" * 40,
            "author": {
                "display_name": "Email Alias Fixture",
                "email": "old-address@example.invalid",
            },
            "files": [{"path": "old.py", "additions": 1}],
        },
        {
            "record_id": "new-email",
            "commit_sha": "2" * 40,
            "author": {
                "display_name": "Email Alias Fixture",
                "email": "new-address@example.invalid",
            },
            "files": [{"path": "new.py", "additions": 1}],
        },
    ]
    payload["aliases"] = [
        {
            "canonical": {
                "display_name": "Email Alias Fixture",
                "email": "new-address@example.invalid",
            },
            "aliases": [
                {
                    "display_name": "Email Alias Fixture",
                    "email": "old-address@example.invalid",
                }
            ],
        }
    ]
    manifest = build_manifest(CreateManifestRequest.model_validate(payload))
    serialized = json.dumps(manifest.model_dump(mode="json"), sort_keys=True)

    assert len(manifest.content.canonical_contributors) == 1
    assert "new-address@example.invalid" not in serialized
    assert "old-address@example.invalid" not in serialized
    assert serialized.count("email-sha256:") >= 2


def test_duplicate_record_id_is_rejected(manifest_payload: dict) -> None:
    payload = deepcopy(manifest_payload)
    payload["records"].append(deepcopy(payload["records"][0]))
    payload["records"][-1]["commit_sha"] = "0" * 40

    with pytest.raises(AttributionDomainError) as caught:
        build_manifest(CreateManifestRequest.model_validate(payload))

    assert caught.value.code == "duplicate_record_id"


def test_duplicate_file_evidence_is_rejected(manifest_payload: dict) -> None:
    payload = deepcopy(manifest_payload)
    payload["records"][0]["files"].append(deepcopy(payload["records"][0]["files"][0]))

    with pytest.raises(AttributionDomainError) as caught:
        build_manifest(CreateManifestRequest.model_validate(payload))

    assert caught.value.code == "duplicate_file_path"


def test_zero_unit_record_has_an_explicit_exclusion(manifest_payload: dict) -> None:
    payload = deepcopy(manifest_payload)
    payload["rules"] = {"commit_base_units": 0, "line_unit": 1}
    for file_record in payload["records"][0]["files"]:
        file_record["additions"] = 0
        file_record["deletions"] = 0

    manifest = build_manifest(CreateManifestRequest.model_validate(payload))

    zero_weight = [
        item
        for item in manifest.content.exclusions
        if item.reason_code is ExclusionReason.ZERO_WEIGHT_RECORD
    ]
    assert [(item.input_record_id, item.reference) for item in zero_weight] == [
        ("commit-a", "a" * 40)
    ]


@pytest.mark.parametrize(
    "path",
    ["/etc/passwd", "C:\\secrets.txt", "src/../secrets.txt", "src//module.py"],
)
def test_repository_paths_reject_escape_and_ambiguous_segments(
    manifest_payload: dict,
    path: str,
) -> None:
    payload = deepcopy(manifest_payload)
    payload["records"][0]["files"][0]["path"] = path

    with pytest.raises(ValueError, match="path"):
        CreateManifestRequest.model_validate(payload)
