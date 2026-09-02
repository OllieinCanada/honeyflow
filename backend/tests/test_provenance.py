import json
import subprocess
from datetime import datetime, timezone

import pytest
from pydantic import ValidationError

from app.schemas.provenance import (
    EvidenceReferenceV1,
    IdentityDecisionV1,
    JuryEventV1,
    ProvenanceManifestV1,
)
from app.services.provenance import (
    CanonicalizationError,
    append_jury_events,
    build_manifest,
    canonical_json,
    classify_path,
    compute_manifest_digest,
    normalize_identities,
    prepare_snapshot,
    verify_manifest,
)
from scripts.verify_provenance import load_manifest, main


@pytest.fixture
def manifest_kwargs():
    return {
        "repository_url": "https://github.com/example/project",
        "source_commit_sha": "a" * 40,
        "attribution": {"alice": 0.6, "bob": 0.4},
        "graph": {
            "nodes": [{"id": "repo"}, {"id": "alice"}, {"id": "bob"}],
            "edges": [
                {
                    "source": "repo",
                    "target": "alice",
                    "weight": 0.6,
                    "evidence_refs": ["rename"],
                },
                {
                    "source": "repo",
                    "target": "bob",
                    "weight": 0.4,
                    "evidence_refs": ["squash"],
                },
            ],
        },
        "lockfile_digests": {"package-lock.json": "1" * 64},
        "attribution_config": {"max_depth": 3, "weights": {"files": 0.3, "commits": 0.7}},
        "paths": [
            "src/app.py",
            "docs/guide.md",
            "tests/test_app.py",
            "vendor/lib.js",
            "generated/client.min.js",
        ],
        "evidence": [
            EvidenceReferenceV1(reference_id="squash", path="src/app.py", change_kind="squashed"),
            EvidenceReferenceV1(reference_id="rename", path="src/renamed.py", change_kind="renamed"),
        ],
        "created_at": datetime(2026, 1, 1, tzinfo=timezone.utc),
    }


def test_canonical_digest_ignores_key_order_and_creation_time(manifest_kwargs):
    first = build_manifest(**manifest_kwargs)
    reordered = dict(manifest_kwargs)
    reordered["attribution"] = {"bob": 0.4, "alice": 0.6}
    reordered["attribution_config"] = {"weights": {"commits": 0.7, "files": 0.3}, "max_depth": 3}
    reordered["paths"] = list(reversed(manifest_kwargs["paths"]))
    reordered["created_at"] = datetime(2027, 1, 1, tzinfo=timezone.utc)
    second = build_manifest(**reordered)
    assert first.manifest_digest == second.manifest_digest


@pytest.mark.parametrize("field", ["sha", "attribution", "exclusion"])
def test_integrity_inputs_change_digest(manifest_kwargs, field):
    baseline = build_manifest(**manifest_kwargs)
    changed = dict(manifest_kwargs)
    if field == "sha":
        changed["source_commit_sha"] = "b" * 40
    elif field == "attribution":
        changed["attribution"] = {"alice": 0.5, "bob": 0.5}
    else:
        changed["excluded_paths"] = ["not-observed/private.dat"]
    assert build_manifest(**changed).manifest_digest != baseline.manifest_digest


def test_tampering_fails_and_json_round_trip_verifies(manifest_kwargs):
    manifest = build_manifest(**manifest_kwargs)
    stored = json.loads(json.dumps(manifest.model_dump(mode="json")))
    assert verify_manifest(stored).valid
    stored["original_attribution"]["alice"] = 0.5
    result = verify_manifest(stored)
    assert not result.valid
    assert result.errors == ("manifest_digest_mismatch",)


def test_canonical_json_rejects_ambiguous_or_unsupported_values():
    with pytest.raises(CanonicalizationError):
        canonical_json({"value": float("nan")})
    with pytest.raises(CanonicalizationError):
        canonical_json({"value": -0.0})
    with pytest.raises(CanonicalizationError):
        canonical_json({"value": object()})
    with pytest.raises(CanonicalizationError):
        canonical_json({1: "non-string key"})


def test_classification_is_conservative_and_never_implicitly_excludes():
    expected = {
        "src/app.py": "first_party",
        "docs/guide.md": "documentation",
        "tests/test_app.py": "test",
        "vendor/lib.js": "vendored",
        "generated/client.min.js": "generated",
    }
    for path, classification in expected.items():
        result = classify_path(path)
        assert result.classification == classification
        assert result.excluded is False


def test_mailmap_uses_git_and_never_emits_email(tmp_path):
    subprocess.run(["git", "init", "-q", str(tmp_path)], check=True)
    (tmp_path / ".mailmap").write_text(
        "Canonical Name <canonical@example.test> Alias Name <alias@example.test>\n",
        encoding="utf-8",
    )
    decisions = normalize_identities(
        tmp_path,
        [("Alias Name", "alias@example.test"), ("dependabot[bot]", "bot@example.test")],
    )
    assert decisions[0].canonical == "Canonical Name"
    assert decisions[0].source == "mailmap"
    assert any(item.is_bot for item in decisions)
    serialized = json.dumps([item.model_dump() for item in decisions])
    assert "@" not in serialized


def test_alias_and_generated_content_diagnostics(manifest_kwargs):
    changed = dict(manifest_kwargs)
    changed["identities"] = [
        IdentityDecisionV1(alias="alice", canonical="actor", source="mailmap"),
        IdentityDecisionV1(alias="a-smith", canonical="actor", source="mailmap"),
    ]
    changed["paths"] = ["vendor/a.js", "generated/b.min.js", "src/c.js"]
    codes = {item.code for item in build_manifest(**changed).diagnostics}
    assert {
        "aliases_converge",
        "generated_vendor_dominance",
        "llm_contribution_not_observed",
    } <= codes


def test_cycle_terminates_and_dangling_or_duplicate_nodes_fail(manifest_kwargs):
    changed = dict(manifest_kwargs)
    changed["graph"] = {
        "nodes": [{"id": "a"}, {"id": "b"}],
        "edges": [
            {"source": "a", "target": "b", "weight": 0.5},
            {"source": "b", "target": "a", "weight": 0.5},
        ],
    }
    assert "dependency_cycle_detected" in build_manifest(**changed).warnings

    changed["graph"] = {"nodes": [{"id": "a"}, {"id": "a"}], "edges": []}
    with pytest.raises(ValueError, match="unique"):
        build_manifest(**changed)
    changed["graph"] = {
        "nodes": [{"id": "a"}],
        "edges": [{"source": "a", "target": "missing", "weight": 0.5}],
    }
    with pytest.raises(ValueError, match="unknown"):
        build_manifest(**changed)


def test_jury_adjustment_is_ordered_linked_and_auditable(manifest_kwargs):
    first = build_manifest(**manifest_kwargs)
    event = JuryEventV1(
        sequence=99,
        event_id="edge-vote:1",
        edge_source="repo",
        edge_target="alice",
        prior_weight=0.6,
        human_weight=0.5,
        confidence=0.8,
    )
    second = append_jury_events(first, [event])
    assert second.previous_digest == first.manifest_digest
    assert second.jury_events[0].sequence == 1
    assert second.original_attribution == first.original_attribution
    assert second.manifest_digest != first.manifest_digest
    assert verify_manifest(second).valid

    invalid = second.model_dump(mode="json")
    invalid["jury_events"][0]["sequence"] = 2
    with pytest.raises(ValidationError, match="contiguous"):
        ProvenanceManifestV1.model_validate(invalid)


def test_cli_and_api_verifier_use_same_result(tmp_path, capsys, manifest_kwargs):
    manifest = build_manifest(**manifest_kwargs)
    path = tmp_path / "manifest.json"
    path.write_text(json.dumps(manifest.model_dump(mode="json")), encoding="utf-8")
    expected = verify_manifest(manifest)
    assert main([str(path)]) == 0
    output = json.loads(capsys.readouterr().out)
    assert output == expected.model_dump(mode="json")


def test_cli_rejects_duplicate_keys(tmp_path):
    path = tmp_path / "duplicate.json"
    path.write_text('{"schema_version":"a","schema_version":"b"}', encoding="utf-8")
    with pytest.raises(ValueError, match="duplicate"):
        load_manifest(path)


def test_model_copy_cannot_bypass_schema_verification(manifest_kwargs):
    manifest = build_manifest(**manifest_kwargs)
    invalid = manifest.model_copy(update={"previous_digest": "not-a-digest"})
    invalid = invalid.model_copy(
        update={"manifest_digest": compute_manifest_digest(invalid)}
    )
    assert not verify_manifest(invalid).valid
    with pytest.raises(ValueError, match="invalid"):
        prepare_snapshot(invalid, None, None)


def test_snapshot_preparation_is_deterministic_under_competing_writers(
    manifest_kwargs,
):
    manifest = build_manifest(**manifest_kwargs)
    previous = "f" * 64
    first, first_sequence = prepare_snapshot(manifest, previous, 7)
    second, second_sequence = prepare_snapshot(manifest, previous, 7)
    assert first_sequence == second_sequence == 8
    assert first.manifest_digest == second.manifest_digest
    assert first.previous_digest == previous
