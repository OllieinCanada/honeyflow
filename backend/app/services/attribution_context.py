"""Request-local capture of attribution inputs without raw model or user data."""

from __future__ import annotations

import hashlib
import json
import math
from collections import Counter
from contextlib import contextmanager
from contextvars import ContextVar
from dataclasses import dataclass, field
from typing import Any, Iterator, Mapping

MAX_CAPTURE_EVENTS = 100
MAX_PRIORS = 5_000


@dataclass
class AttributionCapture:
    inference: Counter[tuple[str, str, str, str, str]] = field(
        default_factory=Counter
    )
    prior_inputs: set[tuple[str, int, str]] = field(default_factory=set)
    warnings: set[str] = field(default_factory=set)

    def snapshot(self) -> dict[str, object]:
        inference_events = [
            {
                "action": action,
                "metadata_status": status,
                "provider": provider or None,
                "model_id": model or None,
                "prompt_template_id": prompt or None,
                "occurrences": count,
            }
            for (action, status, provider, model, prompt), count in sorted(
                self.inference.items()
            )
        ]
        prior_inputs = [
            {"entity_type": entity_type, "count": count, "digest": digest}
            for entity_type, count, digest in sorted(self.prior_inputs)
        ]
        return {
            "inference_events": inference_events,
            "human_prior_inputs": prior_inputs,
            "capture_warnings": sorted(self.warnings),
        }


_CAPTURE: ContextVar[AttributionCapture | None] = ContextVar(
    "honeyflow_attribution_capture", default=None
)
_PENDING_INFERENCE: ContextVar[tuple[str, object] | None] = ContextVar(
    "honeyflow_pending_inference", default=None
)


@contextmanager
def capture_attribution_inputs() -> Iterator[AttributionCapture]:
    capture = AttributionCapture()
    token = _CAPTURE.set(capture)
    pending_token = _PENDING_INFERENCE.set(None)
    try:
        yield capture
    finally:
        _PENDING_INFERENCE.reset(pending_token)
        _CAPTURE.reset(token)


def stage_inference_metadata(action: str, metadata: object) -> None:
    """Stage metadata in this async context until the caller accepts the result."""
    _PENDING_INFERENCE.set((action, metadata))


def record_contributing_inference(action: str) -> None:
    """Record only allowlisted metadata for an accepted inference result."""
    pending = _PENDING_INFERENCE.get()
    _PENDING_INFERENCE.set(None)
    if pending is None or pending[0] != action:
        return
    metadata = pending[1]
    capture = _CAPTURE.get()
    if capture is None:
        return
    status = "unavailable"
    provider = model = prompt = ""
    if isinstance(metadata, Mapping) and metadata.get("action") == action:
        fields = (
            metadata.get("provider"),
            metadata.get("model_id"),
            metadata.get("prompt_template_id"),
        )
        if all(isinstance(value, str) and 0 < len(value) <= 200 for value in fields):
            provider, model, prompt = fields
            status = "observed"
    key = (action[:100], status, provider, model, prompt)
    if key not in capture.inference and len(capture.inference) >= MAX_CAPTURE_EVENTS:
        raise ValueError("inference metadata exceeds capture bound")
    if capture.inference[key] >= 100:
        raise ValueError("inference metadata occurrence count exceeds capture bound")
    capture.inference[key] += 1


def record_human_priors(
    priors: Mapping[str, Mapping[str, Any]], entity_type: str | None
) -> None:
    """Record a digest/count, never prior identity names or values themselves."""
    capture = _CAPTURE.get()
    if capture is None:
        return
    if len(priors) > MAX_PRIORS:
        capture.warnings.add("human_prior_inventory_exceeds_capture_bound")
        return
    projection = []
    for key, value in sorted(priors.items()):
        correction = value.get("correction")
        vote_count = value.get("vote_count")
        if (
            not isinstance(key, str)
            or not 0 < len(key) <= 300
            or isinstance(correction, bool)
            or not isinstance(correction, (int, float))
            or not math.isfinite(float(correction))
        ):
            raise ValueError("invalid human prior input")
        if isinstance(vote_count, bool) or not isinstance(vote_count, int):
            raise ValueError("invalid human prior vote count")
        projection.append([key, float(correction), vote_count])
    encoded = json.dumps(
        projection, ensure_ascii=False, sort_keys=True, separators=(",", ":")
    ).encode("utf-8")
    capture.prior_inputs.add(
        (entity_type or "all", len(projection), hashlib.sha256(encoded).hexdigest())
    )
