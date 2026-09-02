"""Pure deterministic construction of Attribution Manifest V1."""

from __future__ import annotations

import fnmatch
import hashlib
from collections import defaultdict
from dataclasses import dataclass, field
from typing import Iterable, Optional
from urllib.parse import urlsplit, urlunsplit

from app.attribution.canonical import canonical_json_bytes, content_hash
from app.schemas.attribution import (
    AliasRuleConfiguration,
    AttributionConfiguration,
    AttributionManifest,
    AttributionManifestContent,
    CanonicalContributor,
    CreateManifestRequest,
    DependencyReference,
    EvidenceReason,
    EvidenceRecord,
    ExclusionReason,
    ExclusionRecord,
    FileEvidence,
    IdentityInput,
)


class AttributionDomainError(ValueError):
    """Structured domain failure suitable for an HTTP 422 response."""

    def __init__(self, code: str, detail: str):
        super().__init__(detail)
        self.code = code
        self.detail = detail


class _UnionFind:
    def __init__(self) -> None:
        self._parent: dict[str, str] = {}

    def add(self, item: str) -> None:
        self._parent.setdefault(item, item)

    def find(self, item: str) -> str:
        self.add(item)
        parent = self._parent[item]
        if parent != item:
            self._parent[item] = self.find(parent)
        return self._parent[item]

    def union(self, left: str, right: str) -> None:
        left_root = self.find(left)
        right_root = self.find(right)
        if left_root == right_root:
            return
        winner, loser = sorted((left_root, right_root))
        self._parent[loser] = winner


def _email_token(email: str) -> str:
    digest = hashlib.sha256(email.strip().casefold().encode("utf-8")).hexdigest()
    return "email-sha256:{}".format(digest)


def _strong_tokens(identity: IdentityInput) -> list[str]:
    tokens: list[str] = []
    if identity.github_login:
        tokens.append("github:{}".format(identity.github_login.casefold()))
    if identity.email:
        tokens.append(_email_token(identity.email))
    return sorted(tokens)


def _identity_sort_key(identity: IdentityInput) -> tuple[str, str, str]:
    return (
        (identity.github_login or "").casefold(),
        _email_token(identity.email) if identity.email else "",
        identity.display_name.casefold(),
    )


@dataclass
class _Profile:
    display_names: set[str] = field(default_factory=set)
    github_logins: set[str] = field(default_factory=set)
    explicit_alias: bool = False

    @property
    def display_name(self) -> str:
        if not self.display_names:
            return "Unknown contributor"
        return sorted(self.display_names, key=lambda value: (value.casefold(), value))[0]


@dataclass(frozen=True)
class _EligibleIdentityDraft:
    identity: IdentityInput
    evidence_reason: EvidenceReason
    role_context: str


@dataclass(frozen=True)
class _EligibleRecordDraft:
    input_record_id: str
    commit_sha: str
    included_files: tuple[FileEvidence, ...]
    file_change_capped: bool
    record_units: int
    identities: tuple[_EligibleIdentityDraft, ...]


class _IdentityIndex:
    def __init__(
        self,
        request: CreateManifestRequest,
        automatic_identities: Iterable[IdentityInput],
    ):
        # Only identities that survived record/file/bot eligibility may create
        # automatic links. Explicit alias declarations are a separate trusted
        # input and remain available even when one alias has no eligible record.
        identities = list(automatic_identities)
        for declaration in request.aliases:
            identities.append(declaration.canonical)
            identities.extend(declaration.aliases)

        union_find = _UnionFind()
        for identity in identities:
            tokens = _strong_tokens(identity)
            for token in tokens:
                union_find.add(token)
            for token in tokens[1:]:
                union_find.union(tokens[0], token)

        alias_assignments: dict[str, str] = {}
        explicit_tokens: set[str] = set()
        preferred_canonical_tokens: set[str] = set()
        for declaration in sorted(
            request.aliases,
            key=lambda item: tuple(_strong_tokens(item.canonical)),
        ):
            canonical_tokens = _strong_tokens(declaration.canonical)
            canonical_github_tokens = [
                token for token in canonical_tokens if token.startswith("github:")
            ]
            preferred_canonical_tokens.add(
                canonical_github_tokens[0] if canonical_github_tokens else canonical_tokens[0]
            )
            anchor = union_find.find(canonical_tokens[0])
            declaration_tokens = list(canonical_tokens)
            for alias in declaration.aliases:
                declaration_tokens.extend(_strong_tokens(alias))
            for token in sorted(set(declaration_tokens)):
                current = alias_assignments.get(token)
                if current is not None and union_find.find(current) != union_find.find(anchor):
                    raise AttributionDomainError(
                        "ambiguous_alias",
                        "an alias is assigned to more than one canonical identity",
                    )
                alias_assignments[token] = anchor
                union_find.union(anchor, token)
                explicit_tokens.add(token)

        groups: dict[str, set[str]] = defaultdict(set)
        for token in list(union_find._parent):
            groups[union_find.find(token)].add(token)

        self._token_to_id: dict[str, str] = {}
        self._explicit_ids: set[str] = set()
        for group_tokens in groups.values():
            github_tokens = sorted(token for token in group_tokens if token.startswith("github:"))
            preferred_tokens = sorted(group_tokens & preferred_canonical_tokens)
            preferred_github_tokens = [
                token for token in preferred_tokens if token.startswith("github:")
            ]
            if preferred_github_tokens:
                canonical_id = preferred_github_tokens[0]
            elif preferred_tokens:
                canonical_id = preferred_tokens[0]
            elif github_tokens:
                canonical_id = github_tokens[0]
            else:
                canonical_id = sorted(group_tokens)[0]
            for token in group_tokens:
                self._token_to_id[token] = canonical_id
            if group_tokens & explicit_tokens:
                self._explicit_ids.add(canonical_id)

        self.profiles: dict[str, _Profile] = defaultdict(_Profile)
        for identity in identities:
            tokens = _strong_tokens(identity)
            if not tokens:
                continue
            contributor_id = self._token_to_id[tokens[0]]
            profile = self.profiles[contributor_id]
            profile.display_names.add(identity.display_name)
            if identity.github_login:
                profile.github_logins.add(identity.github_login.casefold())
            profile.explicit_alias = contributor_id in self._explicit_ids

    def resolve(
        self,
        identity: IdentityInput,
        *,
        context: str,
    ) -> tuple[str, _Profile]:
        tokens = _strong_tokens(identity)
        if tokens:
            contributor_id = self._token_to_id[tokens[0]]
            return contributor_id, self.profiles[contributor_id]

        digest = hashlib.sha256(
            "{}\x00{}".format(identity.display_name.casefold(), context).encode("utf-8")
        ).hexdigest()
        contributor_id = "unverified-name:{}".format(digest)
        profile = self.profiles[contributor_id]
        profile.display_names.add(identity.display_name)
        return contributor_id, profile


def _canonical_repository_url(value: str) -> str:
    parsed = urlsplit(value)
    scheme = parsed.scheme.lower()
    hostname = (parsed.hostname or "").lower()
    port = ":{}".format(parsed.port) if parsed.port else ""
    path = parsed.path.rstrip("/")
    if path.lower().endswith(".git"):
        path = path[:-4]
    if hostname in {"github.com", "www.github.com"}:
        hostname = "github.com"
        path = path.casefold()
    return urlunsplit((scheme, hostname + port, path, "", ""))


def _is_bot(identity: IdentityInput, configured: set[str]) -> bool:
    if identity.is_bot:
        return True
    login = (identity.github_login or "").casefold()
    return bool(login and (login.endswith("[bot]") or login in configured))


def _classify_path(path: str, request: CreateManifestRequest) -> Optional[ExclusionReason]:
    normalized = path.casefold()
    if any(normalized.startswith(prefix) for prefix in request.rules.vendored_path_prefixes):
        return ExclusionReason.VENDORED_FILE
    if any(
        fnmatch.fnmatchcase(normalized, pattern) for pattern in request.rules.generated_path_globs
    ):
        return ExclusionReason.GENERATED_FILE
    return None


def allocate_integer_units(total: int, weights: dict[str, int]) -> dict[str, int]:
    """Allocate an exact integer total using largest remainders and lexical ties."""
    if total < 0 or any(value < 0 for value in weights.values()):
        raise AttributionDomainError("negative_allocation", "allocation inputs must be nonnegative")
    denominator = sum(weights.values())
    if denominator <= 0:
        raise AttributionDomainError("empty_allocation", "at least one positive weight is required")

    allocations: dict[str, int] = {}
    remainders: list[tuple[int, str]] = []
    for key in sorted(weights):
        numerator = total * weights[key]
        quotient, remainder = divmod(numerator, denominator)
        allocations[key] = quotient
        remainders.append((remainder, key))

    remaining = total - sum(allocations.values())
    for _, key in sorted(remainders, key=lambda item: (-item[0], item[1]))[:remaining]:
        allocations[key] += 1
    return allocations


def _safe_configuration_payload(
    request: CreateManifestRequest,
) -> AttributionConfiguration:
    alias_rules: dict[bytes, AliasRuleConfiguration] = {}
    for declaration in request.aliases:
        canonical_tokens = _strong_tokens(declaration.canonical)
        canonical_github_tokens = [
            token for token in canonical_tokens if token.startswith("github:")
        ]
        canonical_token = (
            canonical_github_tokens[0] if canonical_github_tokens else canonical_tokens[0]
        )
        tokens: set[str] = set(_strong_tokens(declaration.canonical))
        for alias in declaration.aliases:
            tokens.update(_strong_tokens(alias))
        alias_rule = AliasRuleConfiguration(
            canonical_token=canonical_token,
            member_tokens=sorted(tokens),
        )
        alias_rules[canonical_json_bytes(alias_rule)] = alias_rule
    return AttributionConfiguration(
        rules=request.rules,
        alias_rules=[alias_rules[key] for key in sorted(alias_rules)],
    )


def _canonical_dependencies(
    dependencies: Iterable[DependencyReference],
) -> list[DependencyReference]:
    unique: dict[bytes, DependencyReference] = {}
    for dependency in dependencies:
        normalized = dependency.model_copy(
            update={
                "identity": dependency.identity.strip(),
                "source_commit_sha": (
                    dependency.source_commit_sha.lower() if dependency.source_commit_sha else None
                ),
            }
        )
        unique[canonical_json_bytes(normalized)] = normalized
    return [unique[key] for key in sorted(unique)]


def _identity_commitment_payload(identity: IdentityInput) -> dict[str, object]:
    """Normalize an identity for input commitment without emitting raw email."""
    return {
        "display_name": identity.display_name,
        "github_login": identity.github_login.casefold() if identity.github_login else None,
        "email_token": _email_token(identity.email) if identity.email else None,
        "is_bot": identity.is_bot,
    }


def _input_evidence_fingerprint(request: CreateManifestRequest) -> str:
    """Commit to normalized record and alias fields, including excluded evidence."""
    records: list[dict[str, object]] = []
    for record in sorted(
        request.records,
        key=lambda item: (item.commit_sha.lower(), item.record_id),
    ):
        coauthors = [_identity_commitment_payload(identity) for identity in record.coauthors]
        files = [
            {
                "path": file_record.path,
                "additions": file_record.additions,
                "deletions": file_record.deletions,
                "binary": file_record.binary,
            }
            for file_record in record.files
        ]
        records.append(
            {
                "record_id": record.record_id,
                "commit_sha": record.commit_sha.lower(),
                "author": (_identity_commitment_payload(record.author) if record.author else None),
                "coauthors": sorted(coauthors, key=canonical_json_bytes),
                "files": sorted(files, key=canonical_json_bytes),
                "is_merge": record.is_merge,
            }
        )
    commitment: dict[str, object] = {"records": records}
    if request.aliases:
        aliases: list[dict[str, object]] = []
        for declaration in request.aliases:
            aliases.append(
                {
                    "canonical": _identity_commitment_payload(declaration.canonical),
                    "aliases": sorted(
                        (
                            _identity_commitment_payload(identity)
                            for identity in declaration.aliases
                        ),
                        key=canonical_json_bytes,
                    ),
                }
            )
        commitment["aliases"] = sorted(aliases, key=canonical_json_bytes)
    return content_hash(commitment)


def build_manifest(request: CreateManifestRequest) -> AttributionManifest:
    record_ids = [record.record_id for record in request.records]
    if len(record_ids) != len(set(record_ids)):
        raise AttributionDomainError("duplicate_record_id", "record_id values must be unique")
    for record in request.records:
        paths = [file_record.path for file_record in record.files]
        if len(paths) != len(set(paths)):
            raise AttributionDomainError(
                "duplicate_file_path",
                "record {} contains the same file path more than once".format(record.record_id),
            )

    configured_bots = set(request.rules.bot_logins)
    exclusions: list[ExclusionRecord] = []
    seen_commits: set[str] = set()
    eligible_records: list[_EligibleRecordDraft] = []

    records = sorted(
        request.records,
        key=lambda record: (record.commit_sha.lower(), record.record_id),
    )
    for record in records:
        commit_sha = record.commit_sha.lower()
        if commit_sha in seen_commits:
            exclusions.append(
                ExclusionRecord(
                    input_record_id=record.record_id,
                    reference=commit_sha,
                    reason_code=ExclusionReason.DUPLICATE_COMMIT,
                    explanation="A prior canonical record already represents this commit.",
                )
            )
            continue
        seen_commits.add(commit_sha)

        if record.is_merge and request.rules.exclude_merge_commits:
            exclusions.append(
                ExclusionRecord(
                    input_record_id=record.record_id,
                    reference=commit_sha,
                    reason_code=ExclusionReason.MERGE_COMMIT,
                    explanation="Merge commits are excluded by attribution rule configuration.",
                )
            )
            continue

        included_files: list[FileEvidence] = []
        file_change_capped = False
        eligible_lines = 0
        for file_record in sorted(
            record.files,
            key=lambda item: (item.path.casefold(), item.path),
        ):
            path_reason: Optional[ExclusionReason]
            if file_record.binary:
                path_reason = ExclusionReason.BINARY_FILE
            else:
                path_reason = _classify_path(file_record.path, request)
            if path_reason:
                explanations = {
                    ExclusionReason.BINARY_FILE: (
                        "Binary content has no line-based contribution evidence."
                    ),
                    ExclusionReason.GENERATED_FILE: (
                        "The path matches a configured generated-content rule."
                    ),
                    ExclusionReason.VENDORED_FILE: (
                        "The path matches a configured vendored-content prefix."
                    ),
                }
                exclusions.append(
                    ExclusionRecord(
                        input_record_id=record.record_id,
                        reference=file_record.path,
                        reason_code=path_reason,
                        explanation=explanations[path_reason],
                    )
                )
                continue
            changed_lines = file_record.additions + file_record.deletions
            counted_lines = min(changed_lines, request.rules.max_lines_per_file)
            file_change_capped = file_change_capped or counted_lines < changed_lines
            included_files.append(
                FileEvidence(
                    path=file_record.path,
                    additions=file_record.additions,
                    deletions=file_record.deletions,
                    counted_lines=counted_lines,
                )
            )
            eligible_lines += counted_lines

        if not included_files:
            exclusions.append(
                ExclusionRecord(
                    input_record_id=record.record_id,
                    reference=commit_sha,
                    reason_code=ExclusionReason.NO_ELIGIBLE_FILES,
                    explanation="No file in the record remained after configured exclusions.",
                )
            )
            continue

        identities: list[_EligibleIdentityDraft] = []
        if record.author is None:
            exclusions.append(
                ExclusionRecord(
                    input_record_id=record.record_id,
                    reference=commit_sha,
                    reason_code=ExclusionReason.MISSING_AUTHOR,
                    explanation="The record does not contain a primary author identity.",
                )
            )
        elif request.rules.exclude_bots and _is_bot(record.author, configured_bots):
            exclusions.append(
                ExclusionRecord(
                    input_record_id=record.record_id,
                    reference=record.author.github_login or record.author.display_name,
                    reason_code=ExclusionReason.BOT_IDENTITY,
                    explanation="The identity matches a configured bot rule.",
                )
            )
        else:
            identities.append(
                _EligibleIdentityDraft(
                    identity=record.author,
                    evidence_reason=EvidenceReason.PRIMARY_AUTHOR,
                    role_context="author",
                )
            )
        for coauthor in sorted(record.coauthors, key=_identity_sort_key):
            if request.rules.exclude_bots and _is_bot(coauthor, configured_bots):
                exclusions.append(
                    ExclusionRecord(
                        input_record_id=record.record_id,
                        reference=coauthor.github_login or coauthor.display_name,
                        reason_code=ExclusionReason.BOT_IDENTITY,
                        explanation="The identity matches a configured bot rule.",
                    )
                )
                continue
            context_digest = hashlib.sha256(
                "|".join(_identity_sort_key(coauthor)).encode("utf-8")
            ).hexdigest()
            identities.append(
                _EligibleIdentityDraft(
                    identity=coauthor,
                    evidence_reason=EvidenceReason.COAUTHOR,
                    role_context="coauthor:{}".format(context_digest),
                )
            )

        weak_display_names: set[str] = set()
        for identity_draft in identities:
            identity = identity_draft.identity
            if _strong_tokens(identity):
                continue
            normalized_display_name = identity.display_name.casefold()
            if normalized_display_name in weak_display_names:
                raise AttributionDomainError(
                    "ambiguous_weak_identity",
                    "a record contains repeated unverified identity names",
                )
            weak_display_names.add(normalized_display_name)

        if not identities:
            if record.author is not None:
                exclusions.append(
                    ExclusionRecord(
                        input_record_id=record.record_id,
                        reference=commit_sha,
                        reason_code=ExclusionReason.MISSING_AUTHOR,
                        explanation="No non-bot author or co-author identity remained.",
                    )
                )
            continue

        record_units = request.rules.commit_base_units + (eligible_lines * request.rules.line_unit)
        if record_units <= 0:
            exclusions.append(
                ExclusionRecord(
                    input_record_id=record.record_id,
                    reference=commit_sha,
                    reason_code=ExclusionReason.ZERO_WEIGHT_RECORD,
                    explanation=(
                        "Configured base and eligible line units assign this record zero units."
                    ),
                )
            )
            continue
        eligible_records.append(
            _EligibleRecordDraft(
                input_record_id=record.record_id,
                commit_sha=commit_sha,
                included_files=tuple(included_files),
                file_change_capped=file_change_capped,
                record_units=record_units,
                identities=tuple(identities),
            )
        )

    # Identity linking is intentionally a second pass. An identity from an
    # excluded record (or an excluded bot inside an otherwise eligible record)
    # must not bridge two contributors that actually receive weight.
    identity_index = _IdentityIndex(
        request,
        (identity.identity for record in eligible_records for identity in record.identities),
    )
    raw_weights: dict[str, int] = defaultdict(int)
    evidence_drafts: list[dict] = []
    for eligible_record in eligible_records:
        resolved: dict[str, tuple[_Profile, set[EvidenceReason]]] = {}
        for identity_draft in eligible_record.identities:
            contributor_id, profile = identity_index.resolve(
                identity_draft.identity,
                context="{}:{}:{}".format(
                    eligible_record.commit_sha,
                    eligible_record.input_record_id,
                    identity_draft.role_context,
                ),
            )
            reason_codes = resolved.setdefault(contributor_id, (profile, set()))[1]
            reason_codes.add(identity_draft.evidence_reason)
            if profile.explicit_alias:
                reason_codes.add(EvidenceReason.EXPLICIT_ALIAS)
            if eligible_record.file_change_capped:
                reason_codes.add(EvidenceReason.FILE_CHANGE_CAPPED)

        shares = allocate_integer_units(
            eligible_record.record_units,
            {contributor_id: 1 for contributor_id in resolved},
        )
        for contributor_id in sorted(resolved):
            profile, reason_codes = resolved[contributor_id]
            share = shares[contributor_id]
            raw_weights[contributor_id] += share
            evidence_drafts.append(
                {
                    "contributor_id": contributor_id,
                    "input_record_id": eligible_record.input_record_id,
                    "commit_sha": eligible_record.commit_sha,
                    "raw_units": share,
                    "included_files": list(eligible_record.included_files),
                    "reason_codes": set(reason_codes),
                    "profile": profile,
                }
            )

    if not raw_weights:
        raise AttributionDomainError(
            "no_eligible_contributions",
            "no contribution evidence remained after applying attribution rules",
        )

    normalized_weights = allocate_integer_units(
        request.rules.declared_weight_total_units,
        dict(raw_weights),
    )

    display_groups: dict[str, list[str]] = defaultdict(list)
    for contributor_id in raw_weights:
        display_groups[identity_index.profiles[contributor_id].display_name.casefold()].append(
            contributor_id
        )
    uncertain_aliases: dict[str, list[str]] = {}
    for contributor_ids in display_groups.values():
        if len(contributor_ids) > 1:
            ordered = sorted(contributor_ids)
            for contributor_id in ordered:
                uncertain_aliases[contributor_id] = [
                    other for other in ordered if other != contributor_id
                ]

    evidence_records: list[EvidenceRecord] = []
    evidence_ids_by_contributor: dict[str, list[str]] = defaultdict(list)
    for draft in sorted(
        evidence_drafts,
        key=lambda item: (
            item["contributor_id"],
            item["commit_sha"],
            item["input_record_id"],
        ),
    ):
        reason_codes = set(draft["reason_codes"])
        if draft["contributor_id"] in uncertain_aliases:
            reason_codes.add(EvidenceReason.POTENTIAL_ALIAS)
        evidence_payload = {
            "contributor_id": draft["contributor_id"],
            "input_record_id": draft["input_record_id"],
            "commit_sha": draft["commit_sha"],
            "raw_units": draft["raw_units"],
            "included_files": draft["included_files"],
            "reason_codes": sorted(reason_codes, key=lambda reason: reason.value),
        }
        evidence_id = content_hash(evidence_payload)
        evidence = EvidenceRecord(evidence_id=evidence_id, **evidence_payload)
        evidence_records.append(evidence)
        evidence_ids_by_contributor[evidence.contributor_id].append(evidence_id)

    contributors: list[CanonicalContributor] = []
    for contributor_id in sorted(
        normalized_weights,
        key=lambda item: (-normalized_weights[item], item),
    ):
        profile = identity_index.profiles[contributor_id]
        contributors.append(
            CanonicalContributor(
                contributor_id=contributor_id,
                display_name=profile.display_name,
                github_logins=sorted(profile.github_logins),
                weight_units=normalized_weights[contributor_id],
                evidence_ids=sorted(evidence_ids_by_contributor[contributor_id]),
                uncertain_aliases=uncertain_aliases.get(contributor_id, []),
            )
        )

    exclusions.sort(
        key=lambda item: (
            item.input_record_id,
            item.reason_code.value,
            item.reference.casefold(),
            item.reference,
        )
    )
    dependencies = _canonical_dependencies(request.dependencies)
    attribution_configuration = _safe_configuration_payload(request)
    content = AttributionManifestContent(
        project_identity=request.project_identity.strip(),
        source_repository_url=_canonical_repository_url(str(request.source_repository_url)),
        source_commit_sha=request.source_commit_sha.lower(),
        dependency_references=dependencies,
        input_references=[
            "record:{}@{}".format(record.record_id, record.commit_sha.lower())
            for record in sorted(request.records, key=lambda item: item.record_id)
        ],
        input_evidence_fingerprint=_input_evidence_fingerprint(request),
        attribution_configuration=attribution_configuration,
        configuration_fingerprint=content_hash(attribution_configuration),
        canonical_contributors=contributors,
        evidence_records=evidence_records,
        exclusions=exclusions,
        limitations=[
            "Weights reflect configured repository activity evidence, not objective value.",
            (
                "Email-derived identifiers are pseudonymous hashes, not secret or "
                "anonymous identifiers."
            ),
            (
                "Generated, vendored, binary, bot, merge, and missing-author evidence "
                "may be excluded by policy."
            ),
        ],
    )
    return AttributionManifest(
        content=content,
        manifest_content_hash=content_hash(content),
    )
