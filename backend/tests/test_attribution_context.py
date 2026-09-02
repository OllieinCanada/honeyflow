import json

from app.services.attribution_context import (
    MAX_PRIORS,
    capture_attribution_inputs,
    record_contributing_inference,
    record_human_priors,
    stage_inference_metadata,
)


def test_capture_records_only_contributing_allowlisted_metadata():
    with capture_attribution_inputs() as capture:
        stage_inference_metadata(
            "analyze_repo",
            {
                "action": "analyze_repo",
                "provider": "gemini",
                "model_id": "gemini-test",
                "prompt_template_id": "prompts-v1/analyze_repo",
                "ignored": "raw response must not be copied",
            },
        )
        record_contributing_inference("analyze_repo")
        stage_inference_metadata("split_direct_vs_deps", None)
        record_contributing_inference("split_direct_vs_deps")
        stage_inference_metadata("rank_dependency_importance", {"action": "wrong"})
        record_human_priors(
            {"contributor:alice": {"correction": 1.2, "vote_count": 3}},
            "contributor",
        )

    snapshot = capture.snapshot()
    assert len(snapshot["inference_events"]) == 2
    observed = next(
        item for item in snapshot["inference_events"] if item["action"] == "analyze_repo"
    )
    assert observed["metadata_status"] == "observed"
    unavailable = next(
        item
        for item in snapshot["inference_events"]
        if item["action"] == "split_direct_vs_deps"
    )
    assert unavailable["metadata_status"] == "unavailable"
    assert unavailable["provider"] is None
    assert len(snapshot["human_prior_inputs"][0]["digest"]) == 64
    serialized = json.dumps(snapshot)
    assert "alice" not in serialized
    assert "raw response" not in serialized
    assert "rank_dependency_importance" not in serialized


def test_capture_aggregates_concurrent_equivalent_observations():
    metadata = {
        "action": "analyze_repo",
        "provider": "0g",
        "model_id": "model-a",
        "prompt_template_id": "prompts-v1/analyze_repo",
    }
    with capture_attribution_inputs() as capture:
        for _ in range(2):
            stage_inference_metadata("analyze_repo", metadata)
            record_contributing_inference("analyze_repo")
    assert capture.snapshot()["inference_events"][0]["occurrences"] == 2


def test_oversized_prior_capture_warns_without_raising():
    priors = {
        "dependency:item-{}".format(index): {
            "correction": 1.0,
            "vote_count": 2,
        }
        for index in range(MAX_PRIORS + 1)
    }
    with capture_attribution_inputs() as capture:
        record_human_priors(priors, "dependency")

    snapshot = capture.snapshot()
    assert snapshot["human_prior_inputs"] == []
    assert snapshot["capture_warnings"] == [
        "human_prior_inventory_exceeds_capture_bound"
    ]
