"""Versioned public schema for contribution provenance manifests."""

import math
from datetime import datetime
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator


class StrictModel(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)


def _repository_path(value: str) -> str:
    normalized = value.replace("\\", "/").strip("/")
    if (
        not normalized
        or len(normalized) > 512
        or "\x00" in normalized
        or any(part in {"", ".", ".."} for part in normalized.split("/"))
    ):
        raise ValueError("invalid repository path")
    return normalized


def _unit_number(value: object) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise ValueError("value must be numeric")
    number = float(value)
    if not math.isfinite(number) or not 0.0 <= number <= 1.0:
        raise ValueError("value must be finite and between zero and one")
    return number


class RepositoryIdentityV1(StrictModel):
    url: str = Field(min_length=1, max_length=512)
    owner: str = Field(min_length=1, max_length=100)
    name: str = Field(min_length=1, max_length=100)


class LockfileDigestV1(StrictModel):
    path: str = Field(min_length=1, max_length=512)
    algorithm: Literal["sha256"] = "sha256"
    digest: str = Field(pattern=r"^[0-9a-f]{64}$")

    @field_validator("path")
    @classmethod
    def validate_path(cls, value: str) -> str:
        return _repository_path(value)


class ModelReferenceV1(StrictModel):
    provider: str = Field(min_length=1, max_length=100)
    model_id: str = Field(min_length=1, max_length=200)
    prompt_id: str = Field(min_length=1, max_length=200)


class InferenceEventV1(StrictModel):
    action: str = Field(pattern=r"^[a-z0-9_]{1,100}$")
    metadata_status: Literal["observed", "unavailable"]
    provider: str | None = Field(default=None, max_length=200)
    model_id: str | None = Field(default=None, max_length=200)
    prompt_template_id: str | None = Field(default=None, max_length=200)
    occurrences: int = Field(ge=1, le=100)

    @model_validator(mode="after")
    def validate_observed_metadata(self) -> "InferenceEventV1":
        values = (self.provider, self.model_id, self.prompt_template_id)
        if self.metadata_status == "observed" and not all(values):
            raise ValueError("observed inference metadata must be complete")
        if self.metadata_status == "unavailable" and any(values):
            raise ValueError("unavailable inference metadata must not guess values")
        return self


class HumanPriorInputV1(StrictModel):
    entity_type: str = Field(pattern=r"^[a-z0-9_]{1,100}$")
    count: int = Field(ge=0, le=5000)
    digest: str = Field(pattern=r"^[0-9a-f]{64}$")


class IdentityDecisionV1(StrictModel):
    alias: str = Field(min_length=1, max_length=200)
    canonical: str = Field(min_length=1, max_length=200)
    source: Literal["mailmap", "unchanged"]
    is_bot: bool = False

    @field_validator("alias", "canonical")
    @classmethod
    def reject_email_identity(cls, value: str) -> str:
        if any(marker in value for marker in ("@", "<", ">")):
            raise ValueError("manifest identities must not contain email addresses")
        return value


class PathClassificationV1(StrictModel):
    path: str = Field(min_length=1, max_length=512)
    classification: Literal[
        "first_party", "generated", "vendored", "documentation", "test"
    ]
    basis: str = Field(min_length=1, max_length=200)
    excluded: bool = False

    @field_validator("path")
    @classmethod
    def validate_path(cls, value: str) -> str:
        return _repository_path(value)


class EvidenceReferenceV1(StrictModel):
    reference_id: str = Field(min_length=1, max_length=128)
    path: str = Field(min_length=1, max_length=512)
    change_kind: Literal["added", "modified", "renamed", "squashed", "unknown"] = (
        "unknown"
    )

    @field_validator("path")
    @classmethod
    def validate_path(cls, value: str) -> str:
        return _repository_path(value)


class GraphEdgeV1(StrictModel):
    source: str = Field(min_length=1, max_length=300)
    target: str = Field(min_length=1, max_length=300)
    weight: float = Field(ge=0.0, le=1.0)
    evidence_refs: tuple[str, ...] = Field(default=(), max_length=50)

    @field_validator("weight", mode="before")
    @classmethod
    def validate_weight(cls, value: object) -> float:
        return _unit_number(value)


class JuryEventV1(StrictModel):
    sequence: int = Field(ge=1)
    event_id: str = Field(min_length=1, max_length=128)
    edge_source: str = Field(min_length=1, max_length=300)
    edge_target: str = Field(min_length=1, max_length=300)
    prior_weight: float = Field(ge=0.0, le=1.0)
    human_weight: float = Field(ge=0.0, le=1.0)
    confidence: float = Field(ge=0.0, le=1.0)
    action: Literal["jury_weight_adjustment"] = "jury_weight_adjustment"

    @field_validator("prior_weight", "human_weight", "confidence", mode="before")
    @classmethod
    def validate_weight(cls, value: object) -> float:
        return _unit_number(value)

    @field_validator("sequence", mode="before")
    @classmethod
    def validate_sequence_type(cls, value: object) -> object:
        if isinstance(value, bool) or not isinstance(value, int):
            raise ValueError("jury sequence must be an integer")
        return value


class IntegrityDiagnosticV1(StrictModel):
    code: str = Field(pattern=r"^[a-z0-9_]{1,80}$")
    severity: Literal["info", "warning"]
    message: str = Field(min_length=1, max_length=500)


class ProvenanceManifestV1(StrictModel):
    schema_version: Literal["honeyflow.provenance/v1"] = "honeyflow.provenance/v1"
    canonicalization_version: Literal["honeyflow-canonical-json-v1"] = (
        "honeyflow-canonical-json-v1"
    )
    repository: RepositoryIdentityV1
    source_commit_sha: str = Field(pattern=r"^[0-9a-f]{40}([0-9a-f]{24})?$")
    lockfiles: tuple[LockfileDigestV1, ...] = Field(default=(), max_length=100)
    attribution_config: dict[str, object] = Field(default_factory=dict)
    deterministic_rule_version: str = Field(min_length=1, max_length=100)
    model: ModelReferenceV1 | None = None
    inference_events: tuple[InferenceEventV1, ...] = Field(default=(), max_length=100)
    human_prior_inputs: tuple[HumanPriorInputV1, ...] = Field(
        default=(), max_length=100
    )
    identity_decisions: tuple[IdentityDecisionV1, ...] = Field(
        default=(), max_length=5000
    )
    path_classifications: tuple[PathClassificationV1, ...] = Field(
        default=(), max_length=5000
    )
    excluded_paths: tuple[str, ...] = Field(default=(), max_length=5000)
    evidence: tuple[EvidenceReferenceV1, ...] = Field(default=(), max_length=5000)
    graph_edges: tuple[GraphEdgeV1, ...] = Field(default=(), max_length=5000)
    original_attribution: dict[str, float]
    jury_events: tuple[JuryEventV1, ...] = Field(default=(), max_length=5000)
    warnings: tuple[str, ...] = Field(default=(), max_length=100)
    diagnostics: tuple[IntegrityDiagnosticV1, ...] = Field(default=(), max_length=100)
    previous_digest: str | None = Field(default=None, pattern=r"^[0-9a-f]{64}$")
    created_at: datetime
    manifest_digest: str = Field(pattern=r"^[0-9a-f]{64}$")

    @field_validator("original_attribution", mode="before")
    @classmethod
    def validate_attribution(cls, value: object) -> dict[str, float]:
        if not isinstance(value, dict):
            raise ValueError("original_attribution must be an object")
        if len(value) > 5000:
            raise ValueError("original_attribution exceeds 5000 identities")
        validated = {}
        for identity, weight in value.items():
            if not isinstance(identity, str) or not identity or len(identity) > 200:
                raise ValueError("attribution identity must contain 1-200 characters")
            validated[identity] = _unit_number(weight)
        return validated

    @field_validator("excluded_paths")
    @classmethod
    def validate_excluded_paths(cls, value: tuple[str, ...]) -> tuple[str, ...]:
        return tuple(_repository_path(path) for path in value)

    @model_validator(mode="after")
    def validate_event_order(self) -> "ProvenanceManifestV1":
        expected = list(range(1, len(self.jury_events) + 1))
        actual = [event.sequence for event in self.jury_events]
        if actual != expected:
            raise ValueError("jury event sequences must be contiguous and ordered")
        if len({event.event_id for event in self.jury_events}) != len(self.jury_events):
            raise ValueError("jury event IDs must be unique")
        if len({item.path for item in self.lockfiles}) != len(self.lockfiles):
            raise ValueError("lockfile paths must be unique")
        if len({item.path for item in self.path_classifications}) != len(
            self.path_classifications
        ):
            raise ValueError("classified paths must be unique")
        if len(set(self.excluded_paths)) != len(self.excluded_paths):
            raise ValueError("excluded paths must be unique")
        inference_keys = {
            (
                item.action,
                item.metadata_status,
                item.provider,
                item.model_id,
                item.prompt_template_id,
            )
            for item in self.inference_events
        }
        if len(inference_keys) != len(self.inference_events):
            raise ValueError("inference metadata events must be unique")
        if sum(item.occurrences for item in self.inference_events) > 100:
            raise ValueError("inference metadata occurrences exceed the manifest bound")
        prior_keys = {
            (item.entity_type, item.digest) for item in self.human_prior_inputs
        }
        if len(prior_keys) != len(self.human_prior_inputs):
            raise ValueError("human-prior inputs must be unique")
        evidence_ids = {item.reference_id for item in self.evidence}
        if len(evidence_ids) != len(self.evidence):
            raise ValueError("evidence reference IDs must be unique")
        edge_pairs = {(item.source, item.target) for item in self.graph_edges}
        if len(edge_pairs) != len(self.graph_edges):
            raise ValueError("graph edges must be unique")
        if any(
            reference not in evidence_ids
            for edge in self.graph_edges
            for reference in edge.evidence_refs
        ):
            raise ValueError("graph edge contains a dangling evidence reference")
        return self


class VerificationResultV1(StrictModel):
    valid: bool
    expected_digest: str | None = None
    computed_digest: str | None = None
    errors: tuple[str, ...] = ()
