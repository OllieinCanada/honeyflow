"""Deterministic construction and verification of provenance manifests.

The digest is tamper evidence, not a signature or proof that the recorded
claims are true. ``honeyflow-canonical-json-v1`` is deliberately narrower than generic
JSON and does not claim RFC 8785 conformance.
"""

from __future__ import annotations

import hashlib
import hmac
import json
import math
import re
import subprocess
import unicodedata
from collections import Counter, defaultdict, deque
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterable, Mapping, Sequence
from urllib.parse import urlparse

from app.schemas.provenance import (
    EvidenceReferenceV1,
    GraphEdgeV1,
    HumanPriorInputV1,
    IdentityDecisionV1,
    IntegrityDiagnosticV1,
    InferenceEventV1,
    JuryEventV1,
    LockfileDigestV1,
    ModelReferenceV1,
    PathClassificationV1,
    ProvenanceManifestV1,
    RepositoryIdentityV1,
    VerificationResultV1,
)

RULE_VERSION = "honeyflow-attribution-envelope/v1"
MAX_CANONICAL_NODES = 50_000
MAX_GRAPH_NODES = 1_000
MAX_GRAPH_EDGES = 5_000
MAX_GRAPH_DEPTH = 32
DIGEST_FIELDS = (
    "schema_version",
    "canonicalization_version",
    "repository",
    "source_commit_sha",
    "lockfiles",
    "attribution_config",
    "deterministic_rule_version",
    "model",
    "inference_events",
    "human_prior_inputs",
    "identity_decisions",
    "path_classifications",
    "excluded_paths",
    "evidence",
    "graph_edges",
    "original_attribution",
    "jury_events",
    "warnings",
    "diagnostics",
    "previous_digest",
)


class CanonicalizationError(ValueError):
    pass


def _validate_json(value: Any, *, depth: int = 0, counter: list[int] | None = None) -> None:
    if counter is None:
        counter = [0]
    counter[0] += 1
    if counter[0] > MAX_CANONICAL_NODES or depth > 40:
        raise CanonicalizationError("canonical JSON exceeds structural bounds")
    if value is None or isinstance(value, bool):
        return
    if isinstance(value, str):
        if len(value) > 1_000_000:
            raise CanonicalizationError("string exceeds canonicalization bound")
        if unicodedata.normalize("NFC", value) != value:
            raise CanonicalizationError("strings must use NFC Unicode normalization")
        if any(0xD800 <= ord(char) <= 0xDFFF for char in value):
            raise CanonicalizationError("lone Unicode surrogates are not supported")
        return
    if isinstance(value, int) and not isinstance(value, bool):
        if abs(value) > 9_007_199_254_740_991:
            raise CanonicalizationError("integer exceeds interoperable JSON range")
        return
    if isinstance(value, float):
        if not math.isfinite(value):
            raise CanonicalizationError("non-finite numbers are not supported")
        if value == 0 and math.copysign(1.0, value) < 0:
            raise CanonicalizationError("negative zero is not supported")
        return
    if isinstance(value, list):
        for item in value:
            _validate_json(item, depth=depth + 1, counter=counter)
        return
    if isinstance(value, dict):
        for key, item in value.items():
            if not isinstance(key, str):
                raise CanonicalizationError("JSON object keys must be strings")
            _validate_json(key, depth=depth + 1, counter=counter)
            _validate_json(item, depth=depth + 1, counter=counter)
        return
    raise CanonicalizationError("unsupported canonical JSON type: {}".format(type(value).__name__))


def canonical_json(value: Any) -> bytes:
    """Serialize the documented Honeyflow canonical JSON profile."""
    _validate_json(value)
    return json.dumps(
        value,
        ensure_ascii=False,
        allow_nan=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")


def _digest_payload(data: Mapping[str, Any]) -> dict[str, Any]:
    return {field: data[field] for field in DIGEST_FIELDS}


def compute_manifest_digest(data: Mapping[str, Any] | ProvenanceManifestV1) -> str:
    dumped = data.model_dump(mode="json") if isinstance(data, ProvenanceManifestV1) else dict(data)
    return hashlib.sha256(canonical_json(_digest_payload(dumped))).hexdigest()


def classify_path(path: str, overrides: Mapping[str, str] | None = None) -> PathClassificationV1:
    normalized = path.replace("\\", "/").strip("/")
    if (
        not normalized
        or len(normalized) > 512
        or "\x00" in normalized
        or any(part in {"", ".", ".."} for part in normalized.split("/"))
    ):
        raise ValueError("invalid repository path")
    override = (overrides or {}).get(normalized)
    allowed = {"first_party", "generated", "vendored", "documentation", "test"}
    if override in allowed:
        return PathClassificationV1(
            path=normalized,
            classification=override,
            basis="explicit override",
        )
    lower = normalized.lower()
    parts = lower.split("/")
    name = parts[-1]
    if any(
        part in {"vendor", "vendored", "third_party", "node_modules"}
        for part in parts[:-1]
    ):
        kind, basis = "vendored", "conservative vendor directory rule"
    elif any(part in {"generated", "dist", "build"} for part in parts[:-1]) or (
        name.endswith((".min.js", ".min.css"))
    ):
        kind, basis = "generated", "conservative generated-output rule"
    elif (
        parts[0] in {"docs", "doc"}
        or name.startswith("readme")
        or name.endswith((".md", ".rst"))
    ):
        kind, basis = "documentation", "documentation path rule"
    elif any(
        part in {"test", "tests", "spec", "specs", "__tests__"}
        for part in parts[:-1]
    ) or re.search(r"(^|[._-])(test|spec)([._-]|$)", name):
        kind, basis = "test", "test path rule"
    else:
        kind = "first_party"
        basis = "no conservative generated/vendor/docs/test rule matched"
    return PathClassificationV1(path=normalized, classification=kind, basis=basis)


def _is_bot(name: str) -> bool:
    lowered = name.casefold().strip()
    return bool(re.search(r"(?:\[bot\]|[-_ ]bot)$", lowered))


def normalize_identities(
    repository: Path,
    identities: Sequence[tuple[str, str]],
    *,
    timeout_seconds: float = 5.0,
) -> tuple[IdentityDecisionV1, ...]:
    """Resolve aliases with Git's own mailmap implementation.

    Emails are passed to the local Git process only and are never returned.
    """
    if len(identities) > 5_000:
        raise ValueError("identity input exceeds 5000 entries")
    lines = []
    for name, email in identities:
        if not name.strip() or len(name) > 200 or not email.strip() or len(email) > 320:
            raise ValueError("invalid contributor identity")
        if any(char in name + email for char in "\r\n<>" ):
            raise ValueError("identity contains control or delimiter characters")
        lines.append("{} <{}>".format(name.strip(), email.strip()))
    if not lines:
        return ()
    result = subprocess.run(
        ["git", "-C", str(repository), "check-mailmap", "--stdin"],
        input="\n".join(lines) + "\n",
        text=True,
        capture_output=True,
        timeout=timeout_seconds,
        check=False,
    )
    if result.returncode != 0:
        raise ValueError("git check-mailmap failed")
    output = result.stdout.splitlines()
    if len(output) != len(identities):
        raise ValueError("git check-mailmap returned an unexpected result count")
    decisions = []
    for (alias, _), mapped in zip(identities, output):
        canonical = mapped.rsplit(" <", 1)[0].strip()
        if not canonical:
            raise ValueError("git check-mailmap returned an empty identity")
        decisions.append(
            IdentityDecisionV1(
                alias=alias.strip(),
                canonical=canonical,
                source="mailmap" if canonical != alias.strip() else "unchanged",
                is_bot=_is_bot(canonical),
            )
        )
    return tuple(
        sorted(
            decisions,
            key=lambda item: (item.canonical.casefold(), item.alias.casefold()),
        )
    )


def public_identity_decisions(names: Iterable[str]) -> tuple[IdentityDecisionV1, ...]:
    """Record public attribution labels without inferring email/account ownership."""
    raw_names = list(names)
    if any(not isinstance(name, str) for name in raw_names):
        raise ValueError("public identity labels must be strings")
    unique = sorted(set(raw_names), key=str.casefold)
    if len(unique) > 5_000:
        raise ValueError("identity input exceeds 5000 entries")
    return tuple(
        IdentityDecisionV1(
            alias=name,
            canonical=name,
            source="unchanged",
            is_bot=_is_bot(name),
        )
        for name in unique
        if name
    )


def _graph_evidence(graph: Mapping[str, Any]) -> tuple[tuple[GraphEdgeV1, ...], tuple[str, ...]]:
    raw_nodes = graph.get("nodes") or []
    raw_edges = graph.get("edges") or []
    if not isinstance(raw_nodes, list) or not isinstance(raw_edges, list):
        raise ValueError("graph nodes and edges must be lists")
    if len(raw_nodes) > MAX_GRAPH_NODES or len(raw_edges) > MAX_GRAPH_EDGES:
        raise ValueError("graph exceeds provenance bounds")
    node_ids: set[str] = set()
    for node in raw_nodes:
        if not isinstance(node, dict):
            raise ValueError("graph node must be an object")
        node_id = node.get("id")
        if not isinstance(node_id, str) or not node_id or len(node_id) > 300:
            raise ValueError("graph nodes require bounded string IDs")
        if node_id in node_ids:
            raise ValueError("graph node IDs must be unique")
        node_ids.add(node_id)
    adjacency: dict[str, list[str]] = defaultdict(list)
    indegree = Counter({node: 0 for node in node_ids})
    edges: list[GraphEdgeV1] = []
    edge_pairs: set[tuple[str, str]] = set()
    for raw in raw_edges:
        if not isinstance(raw, dict):
            raise ValueError("graph edge must be an object")
        source, target = raw.get("source"), raw.get("target")
        if not isinstance(source, str) or not isinstance(target, str):
            raise ValueError("graph edge endpoints must be strings")
        if source not in node_ids or target not in node_ids:
            raise ValueError("graph edge references an unknown node")
        if (source, target) in edge_pairs:
            raise ValueError("graph edges must be unique by source and target")
        edge_pairs.add((source, target))
        raw_weight = raw.get("weight", 0.0)
        if isinstance(raw_weight, bool) or not isinstance(raw_weight, (int, float)):
            raise ValueError("graph edge weight must be numeric")
        weight = float(raw_weight)
        metadata = raw.get("metadata") or {}
        if not isinstance(metadata, dict):
            raise ValueError("graph edge metadata must be an object")
        refs = raw.get("evidence_refs") or metadata.get("evidence_refs") or []
        if not isinstance(refs, list) or len(refs) > 50:
            raise ValueError("edge evidence references exceed bounds")
        if any(not isinstance(ref, str) or not ref or len(ref) > 128 for ref in refs):
            raise ValueError("edge evidence reference must be a bounded string")
        clean_refs = tuple(sorted(set(refs)))
        edges.append(
            GraphEdgeV1(
                source=source,
                target=target,
                weight=weight,
                evidence_refs=clean_refs,
            )
        )
        adjacency[source].append(target)
        indegree[target] += 1
    queue = deque(sorted(node for node, degree in indegree.items() if degree == 0))
    visited = 0
    depth = {node: 0 for node in queue}
    exceeded_depth = False
    while queue:
        node = queue.popleft()
        visited += 1
        for target in sorted(adjacency[node]):
            candidate = depth[node] + 1
            if candidate > MAX_GRAPH_DEPTH:
                exceeded_depth = True
            else:
                depth[target] = max(depth.get(target, 0), candidate)
            indegree[target] -= 1
            if indegree[target] == 0:
                queue.append(target)
    warnings = []
    if visited != len(node_ids):
        warnings.append("dependency_cycle_detected")
    if exceeded_depth:
        warnings.append("dependency_depth_bound_reached")
    sorted_edges = tuple(
        sorted(edges, key=lambda edge: (edge.source, edge.target, edge.weight))
    )
    return sorted_edges, tuple(warnings)


def _diagnostics(
    attribution: Mapping[str, float],
    identities: Sequence[IdentityDecisionV1],
    paths: Sequence[PathClassificationV1],
    model: ModelReferenceV1 | None,
    inference_events: Sequence[InferenceEventV1],
    human_prior_inputs: Sequence[HumanPriorInputV1],
) -> tuple[IntegrityDiagnosticV1, ...]:
    result: list[IntegrityDiagnosticV1] = []
    total = sum(attribution.values())
    if total and max(attribution.values(), default=0.0) / total >= 0.8:
        result.append(
            IntegrityDiagnosticV1(
                code="attribution_concentration",
                severity="warning",
                message="At least 80% of recorded attribution is assigned to one identity.",
            )
        )
    aliases = Counter(item.canonical.casefold() for item in identities)
    if any(count > 1 for count in aliases.values()):
        result.append(
            IntegrityDiagnosticV1(
                code="aliases_converge",
                severity="info",
                message="Multiple recorded aliases normalize to one contributor identity.",
            )
        )
    split: dict[str, set[str]] = defaultdict(set)
    for item in identities:
        split[item.alias.casefold()].add(item.canonical.casefold())
    if any(len(values) > 1 for values in split.values()):
        result.append(
            IntegrityDiagnosticV1(
                code="suspicious_alias_split",
                severity="warning",
                message="One alias resolves to multiple canonical identities.",
            )
        )
    classified = Counter(item.classification for item in paths)
    generated_share = classified["generated"] + classified["vendored"]
    if paths and generated_share / len(paths) >= 0.5:
        result.append(
            IntegrityDiagnosticV1(
                code="generated_vendor_dominance",
                severity="warning",
                message=(
                    "Generated or vendored paths make up at least half of "
                    "classified evidence."
                ),
            )
        )
    if any(event.metadata_status == "unavailable" for event in inference_events):
        result.append(
            IntegrityDiagnosticV1(
                code="model_metadata_missing",
                severity="warning",
                message=(
                    "At least one contributing inference lacked model and "
                    "prompt identifiers."
                ),
            )
        )
    elif model is None and not inference_events:
        result.append(
            IntegrityDiagnosticV1(
                code="llm_contribution_not_observed",
                severity="info",
                message="No model response was observed contributing to this run.",
            )
        )
    if not human_prior_inputs:
        result.append(
            IntegrityDiagnosticV1(
                code="human_prior_input_unobserved",
                severity="warning",
                message="No human-prior input digest was captured for this run.",
            )
        )
    return tuple(result)


def build_manifest(
    *,
    repository_url: str,
    source_commit_sha: str,
    attribution: Mapping[str, float],
    graph: Mapping[str, Any],
    lockfile_digests: Mapping[str, str] | None = None,
    attribution_config: Mapping[str, Any] | None = None,
    identities: Sequence[IdentityDecisionV1] = (),
    paths: Iterable[str] = (),
    evidence: Sequence[EvidenceReferenceV1] = (),
    jury_events: Sequence[JuryEventV1] = (),
    excluded_paths: Iterable[str] = (),
    model: ModelReferenceV1 | None = None,
    inference_events: Sequence[InferenceEventV1] = (),
    human_prior_inputs: Sequence[HumanPriorInputV1] = (),
    warnings: Iterable[str] = (),
    created_at: datetime | None = None,
) -> ProvenanceManifestV1:
    parsed = urlparse(repository_url)
    parts = [part for part in parsed.path.split("/") if part]
    if (
        parsed.scheme != "https"
        or parsed.hostname not in {"github.com", "www.github.com"}
        or len(parts) < 2
    ):
        raise ValueError("repository_url must identify a GitHub repository")
    owner, name = parts[0].casefold(), parts[1].removesuffix(".git").casefold()
    raw_exclusions = list(excluded_paths)
    if any(not isinstance(path, str) for path in raw_exclusions):
        raise ValueError("excluded repository paths must be strings")
    exclusions = {path.replace("\\", "/").strip("/") for path in raw_exclusions}
    if any(not path or len(path) > 512 or "\x00" in path for path in exclusions):
        raise ValueError("invalid excluded repository path")
    if any(
        any(part in {"", ".", ".."} for part in path.split("/"))
        for path in exclusions
    ):
        raise ValueError("excluded repository paths may not traverse directories")
    input_paths = list(paths)
    if any(not isinstance(path, str) for path in input_paths):
        raise ValueError("repository paths must be strings")
    if len(input_paths) != len(set(input_paths)):
        raise ValueError("repository paths must be unique")
    classifications = tuple(
        sorted(
            (
                PathClassificationV1.model_validate(
                    {
                        **item.model_dump(mode="python"),
                        "excluded": item.path in exclusions,
                    }
                )
                for item in (classify_path(path) for path in input_paths)
            ),
            key=lambda item: item.path,
        )
    )
    unknown_exclusions = sorted(exclusions - {item.path for item in classifications})
    graph_edges, graph_warnings = _graph_evidence(graph)
    manifest_warnings = list(graph_warnings)
    if unknown_exclusions:
        manifest_warnings.append("excluded_paths_without_classification")
    for warning in warnings:
        if not isinstance(warning, str) or not re.fullmatch(
            r"[a-z0-9_]{1,100}", warning
        ):
            raise ValueError("manifest warning must be a bounded identifier")
        manifest_warnings.append(warning)
    lockfiles = tuple(
        LockfileDigestV1(path=path.replace("\\", "/"), digest=digest)
        for path, digest in sorted((lockfile_digests or {}).items())
    )
    if len(attribution) > 5_000:
        raise ValueError("attribution exceeds 5000 identities")
    for identity, value in attribution.items():
        if not isinstance(identity, str):
            raise ValueError("attribution identity must be a string")
        if isinstance(value, bool) or not isinstance(value, (int, float)):
            raise ValueError("attribution weight must be numeric")
        if not math.isfinite(float(value)) or not 0.0 <= float(value) <= 1.0:
            raise ValueError("attribution weight must be finite and between zero and one")
    original_attribution = {
        key: float(value) for key, value in sorted(attribution.items())
    }
    data = {
        "repository": RepositoryIdentityV1(
            url="https://github.com/{}/{}".format(owner, name),
            owner=owner,
            name=name,
        ),
        "source_commit_sha": source_commit_sha.lower(),
        "lockfiles": lockfiles,
        "attribution_config": dict(attribution_config or {}),
        "deterministic_rule_version": RULE_VERSION,
        "model": model,
        "inference_events": tuple(
            sorted(
                inference_events,
                key=lambda event: (
                    event.action,
                    event.metadata_status,
                    event.provider or "",
                    event.model_id or "",
                    event.prompt_template_id or "",
                ),
            )
        ),
        "human_prior_inputs": tuple(
            sorted(
                human_prior_inputs,
                key=lambda prior: (prior.entity_type, prior.digest),
            )
        ),
        "identity_decisions": tuple(
            sorted(
                identities,
                key=lambda item: (
                    item.canonical.casefold(),
                    item.alias.casefold(),
                ),
            )
        ),
        "path_classifications": classifications,
        "excluded_paths": tuple(sorted(exclusions)),
        "evidence": tuple(
            sorted(
                evidence,
                key=lambda item: (
                    item.reference_id,
                    item.path,
                    item.change_kind,
                ),
            )
        ),
        "graph_edges": graph_edges,
        "original_attribution": original_attribution,
        "jury_events": tuple(jury_events),
        "warnings": tuple(sorted(set(manifest_warnings))),
        "diagnostics": tuple(
            sorted(
                _diagnostics(
                    original_attribution,
                    identities,
                    classifications,
                    model,
                    inference_events,
                    human_prior_inputs,
                ),
                key=lambda item: item.code,
            )
        ),
        "previous_digest": None,
        "created_at": created_at or datetime.now(timezone.utc),
        "manifest_digest": "0" * 64,
    }
    provisional = ProvenanceManifestV1(**data)
    return ProvenanceManifestV1.model_validate(
        {
            **provisional.model_dump(mode="python"),
            "manifest_digest": compute_manifest_digest(provisional),
        }
    )


def append_jury_events(
    manifest: ProvenanceManifestV1,
    events: Sequence[JuryEventV1],
    *,
    created_at: datetime | None = None,
) -> ProvenanceManifestV1:
    if not verify_manifest(manifest).valid:
        raise ValueError("cannot extend an invalid provenance manifest")
    existing_ids = {event.event_id for event in manifest.jury_events}
    combined = list(manifest.jury_events)
    added = False
    for event in events:
        if event.event_id in existing_ids:
            continue
        combined.append(
            JuryEventV1.model_validate(
                {
                    **event.model_dump(mode="python"),
                    "sequence": len(combined) + 1,
                }
            )
        )
        existing_ids.add(event.event_id)
        added = True
    if not added:
        return manifest
    updated = ProvenanceManifestV1.model_validate(
        {
            **manifest.model_dump(mode="python"),
            "jury_events": tuple(combined),
            "previous_digest": manifest.manifest_digest,
            "created_at": created_at or datetime.now(timezone.utc),
            "manifest_digest": "0" * 64,
        }
    )
    return ProvenanceManifestV1.model_validate(
        {
            **updated.model_dump(mode="python"),
            "manifest_digest": compute_manifest_digest(updated),
        }
    )


def link_manifest(
    manifest: ProvenanceManifestV1, previous_digest: str
) -> ProvenanceManifestV1:
    """Link a new immutable snapshot to the current project snapshot."""
    if manifest.previous_digest not in (None, previous_digest):
        raise ValueError("manifest does not extend the current snapshot")
    linked = ProvenanceManifestV1.model_validate(
        {
            **manifest.model_dump(mode="python"),
            "previous_digest": previous_digest,
            "manifest_digest": "0" * 64,
        }
    )
    return ProvenanceManifestV1.model_validate(
        {
            **linked.model_dump(mode="python"),
            "manifest_digest": compute_manifest_digest(linked),
        }
    )


def prepare_snapshot(
    manifest: ProvenanceManifestV1,
    latest_digest: str | None,
    latest_sequence: int | None,
) -> tuple[ProvenanceManifestV1, int]:
    """Pure boundary for deterministic linking before database constraints apply."""
    if not verify_manifest(manifest).valid:
        raise ValueError("cannot store an invalid provenance manifest")
    if latest_digest is None and latest_sequence is None:
        if manifest.previous_digest is not None:
            raise ValueError("initial snapshot cannot have a previous digest")
        return manifest, 1
    if (
        latest_digest is None
        or latest_sequence is None
        or not re.fullmatch(r"[0-9a-f]{64}", latest_digest)
        or not 1 <= latest_sequence < 2_147_483_647
    ):
        raise ValueError("invalid latest snapshot state")
    return link_manifest(manifest, latest_digest), latest_sequence + 1


def verify_manifest(
    value: Mapping[str, Any] | ProvenanceManifestV1,
) -> VerificationResultV1:
    try:
        raw = (
            value.model_dump(mode="python")
            if isinstance(value, ProvenanceManifestV1)
            else value
        )
        manifest = ProvenanceManifestV1.model_validate(raw)
        computed = compute_manifest_digest(manifest)
        valid = hmac.compare_digest(manifest.manifest_digest, computed)
        return VerificationResultV1(
            valid=valid,
            expected_digest=manifest.manifest_digest,
            computed_digest=computed,
            errors=() if valid else ("manifest_digest_mismatch",),
        )
    except CanonicalizationError:
        return VerificationResultV1(valid=False, errors=("invalid_canonical_json",))
    except (ValueError, TypeError):
        return VerificationResultV1(valid=False, errors=("invalid_manifest_schema",))
