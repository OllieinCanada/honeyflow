"""Strict schemas for deterministic attribution manifests and payout previews."""

from __future__ import annotations

import re
from enum import Enum
from typing import Annotated, Final, Literal, Optional

from pydantic import BaseModel, ConfigDict, Field, HttpUrl, field_validator, model_validator

MANIFEST_SCHEMA_VERSION: Final[Literal["attribution-manifest/v1"]] = "attribution-manifest/v1"
ALGORITHM_VERSION: Final[Literal["deterministic-contribution-units/v1"]] = (
    "deterministic-contribution-units/v1"
)
OVERLAY_SCHEMA_VERSION: Final[Literal["attribution-review-overlay/v1"]] = (
    "attribution-review-overlay/v1"
)
PAYOUT_CALCULATION_VERSION: Final[Literal["payout-preview/v1"]] = "payout-preview/v1"
DECLARED_WEIGHT_TOTAL_UNITS: Final[Literal[1_000_000]] = 1_000_000

GitObjectId = Annotated[str, Field(pattern=r"^[0-9a-fA-F]{40}([0-9a-fA-F]{24})?$")]
Sha256Hex = Annotated[str, Field(pattern=r"^[0-9a-f]{64}$")]
RecordId = Annotated[str, Field(min_length=1, max_length=200, pattern=r"^[A-Za-z0-9._:/@+-]+$")]


class StrictModel(BaseModel):
    model_config = ConfigDict(
        extra="forbid",
        str_strip_whitespace=True,
        validate_default=True,
    )


class EvidenceReason(str, Enum):
    PRIMARY_AUTHOR = "primary_author"
    COAUTHOR = "coauthor"
    EXPLICIT_ALIAS = "explicit_alias"
    FILE_CHANGE_CAPPED = "file_change_capped"
    POTENTIAL_ALIAS = "potential_alias"


class ExclusionReason(str, Enum):
    BINARY_FILE = "binary_file"
    BOT_IDENTITY = "bot_identity"
    DUPLICATE_COMMIT = "duplicate_commit"
    GENERATED_FILE = "generated_file"
    MERGE_COMMIT = "merge_commit"
    MISSING_AUTHOR = "missing_author"
    NO_ELIGIBLE_FILES = "no_eligible_files"
    VENDORED_FILE = "vendored_file"
    ZERO_WEIGHT_RECORD = "zero_weight_record"


class AdjustmentReason(str, Enum):
    DUPLICATE_IDENTITY = "duplicate_identity"
    EVIDENCE_CORRECTION = "evidence_correction"
    SCOPE_CORRECTION = "scope_correction"


class IdentityInput(StrictModel):
    display_name: str = Field(min_length=1, max_length=200)
    github_login: Optional[str] = Field(
        default=None,
        min_length=1,
        max_length=100,
        pattern=(
            r"^[A-Za-z0-9](?:[A-Za-z0-9-]{0,37}[A-Za-z0-9])?"
            r"(?:\[bot\])?$"
        ),
    )
    email: Optional[str] = Field(default=None, min_length=3, max_length=254)
    is_bot: bool = Field(default=False, strict=True)

    @field_validator("email")
    @classmethod
    def validate_email_shape(cls, value: Optional[str]) -> Optional[str]:
        if value is None:
            return value
        local, separator, domain = value.partition("@")
        if not separator or not local or not domain or "." not in domain:
            raise ValueError("email must have a local part and domain")
        return value

    @model_validator(mode="after")
    def require_stable_identity(self) -> "IdentityInput":
        if not self.github_login and not self.email and not self.display_name:
            raise ValueError("identity requires a login, email, or display name")
        return self


class AliasDeclaration(StrictModel):
    canonical: IdentityInput
    aliases: list[IdentityInput] = Field(min_length=1, max_length=20)

    @model_validator(mode="after")
    def require_strong_alias_identifiers(self) -> "AliasDeclaration":
        identities = [self.canonical, *self.aliases]
        if any(not identity.github_login and not identity.email for identity in identities):
            raise ValueError("explicit aliases require a GitHub login or email")
        return self


class FileContributionInput(StrictModel):
    path: str = Field(min_length=1, max_length=1000)
    additions: int = Field(default=0, ge=0, le=10_000_000, strict=True)
    deletions: int = Field(default=0, ge=0, le=10_000_000, strict=True)
    binary: bool = Field(default=False, strict=True)

    @field_validator("path")
    @classmethod
    def require_relative_repository_path(cls, value: str) -> str:
        normalized = value.replace("\\", "/")
        if normalized.startswith("/") or re.match(r"^[A-Za-z]:/", normalized):
            raise ValueError("path must be repository-relative")
        if any(part in {"", ".", ".."} for part in normalized.split("/")):
            raise ValueError("path must not contain empty, dot, or parent segments")
        return normalized


class ContributionRecordInput(StrictModel):
    record_id: RecordId
    commit_sha: GitObjectId
    author: Optional[IdentityInput] = None
    coauthors: list[IdentityInput] = Field(default_factory=list, max_length=20)
    files: list[FileContributionInput] = Field(default_factory=list, max_length=1000)
    is_merge: bool = Field(default=False, strict=True)


class DependencyReference(StrictModel):
    identity: str = Field(min_length=1, max_length=500)
    version: Optional[str] = Field(default=None, max_length=200)
    source_commit_sha: Optional[GitObjectId] = None


class AttributionRules(StrictModel):
    commit_base_units: int = Field(default=1000, ge=0, le=1_000_000, strict=True)
    line_unit: int = Field(default=1, ge=0, le=10_000, strict=True)
    max_lines_per_file: int = Field(
        default=10_000,
        ge=1,
        le=10_000_000,
        strict=True,
    )
    exclude_merge_commits: bool = Field(default=True, strict=True)
    exclude_bots: bool = Field(default=True, strict=True)
    bot_logins: list[str] = Field(default_factory=list, max_length=1000)
    vendored_path_prefixes: list[str] = Field(
        default_factory=lambda: ["node_modules/", "third_party/", "vendor/"],
        max_length=100,
    )
    generated_path_globs: list[str] = Field(
        default_factory=lambda: ["*.min.js", "*.min.css", "*.map", "dist/*"],
        max_length=100,
    )
    declared_weight_total_units: Literal[1_000_000] = DECLARED_WEIGHT_TOTAL_UNITS

    @field_validator("bot_logins")
    @classmethod
    def normalize_configured_bot_logins(cls, values: list[str]) -> list[str]:
        return sorted({value.casefold() for value in values})

    @field_validator("vendored_path_prefixes")
    @classmethod
    def normalize_prefixes(cls, values: list[str]) -> list[str]:
        normalized = {value.replace("\\", "/").lstrip("/").casefold() for value in values}
        return sorted(prefix if prefix.endswith("/") else prefix + "/" for prefix in normalized)

    @field_validator("generated_path_globs")
    @classmethod
    def normalize_globs(cls, values: list[str]) -> list[str]:
        return sorted({value.replace("\\", "/").casefold() for value in values})


class AliasRuleConfiguration(StrictModel):
    """Privacy-safe, replayable representation of one explicit alias rule."""

    canonical_token: str = Field(min_length=1, max_length=200)
    member_tokens: list[str] = Field(min_length=1, max_length=42)


class AttributionConfiguration(StrictModel):
    """Normalized replay configuration with no raw email aliases."""

    rules: AttributionRules
    alias_rules: list[AliasRuleConfiguration]


class CreateManifestRequest(StrictModel):
    project_identity: str = Field(min_length=1, max_length=300)
    source_repository_url: HttpUrl
    source_commit_sha: GitObjectId
    dependencies: list[DependencyReference] = Field(default_factory=list, max_length=5000)
    records: list[ContributionRecordInput] = Field(min_length=1, max_length=10_000)
    aliases: list[AliasDeclaration] = Field(default_factory=list, max_length=1000)
    rules: AttributionRules = Field(default_factory=AttributionRules)


class FileEvidence(StrictModel):
    path: str = Field(min_length=1, max_length=1000)
    additions: int = Field(ge=0, le=10_000_000, strict=True)
    deletions: int = Field(ge=0, le=10_000_000, strict=True)
    counted_lines: int = Field(ge=0, le=10_000_000, strict=True)


class EvidenceRecord(StrictModel):
    evidence_id: Sha256Hex
    contributor_id: str = Field(min_length=1, max_length=200)
    input_record_id: str = Field(min_length=1, max_length=200)
    commit_sha: str
    raw_units: int = Field(ge=0, strict=True)
    included_files: list[FileEvidence]
    reason_codes: list[EvidenceReason]


class ExclusionRecord(StrictModel):
    input_record_id: str = Field(min_length=1, max_length=200)
    reference: str = Field(min_length=1, max_length=1000)
    reason_code: ExclusionReason
    explanation: str = Field(min_length=1, max_length=500)


class CanonicalContributor(StrictModel):
    contributor_id: str = Field(min_length=1, max_length=200)
    display_name: str = Field(min_length=1, max_length=200)
    github_logins: list[str]
    weight_units: int = Field(
        ge=0,
        le=DECLARED_WEIGHT_TOTAL_UNITS,
        strict=True,
    )
    evidence_ids: list[Sha256Hex]
    uncertain_aliases: list[str] = Field(default_factory=list)


class AttributionManifestContent(StrictModel):
    schema_version: Literal["attribution-manifest/v1"] = MANIFEST_SCHEMA_VERSION
    project_identity: str
    source_repository_url: str
    source_commit_sha: str
    dependency_references: list[DependencyReference]
    input_references: list[str]
    algorithm_version: Literal["deterministic-contribution-units/v1"] = ALGORITHM_VERSION
    attribution_configuration: AttributionConfiguration
    configuration_fingerprint: Sha256Hex
    declared_weight_total_units: Literal[1_000_000] = DECLARED_WEIGHT_TOTAL_UNITS
    canonical_contributors: list[CanonicalContributor]
    evidence_records: list[EvidenceRecord]
    exclusions: list[ExclusionRecord]
    limitations: list[str]


class AttributionManifest(StrictModel):
    content: AttributionManifestContent
    manifest_content_hash: Sha256Hex


class StoredManifestResponse(StrictModel):
    manifest: AttributionManifest
    created: bool


class OverlayAdjustmentInput(StrictModel):
    contributor_id: str = Field(min_length=1, max_length=200)
    delta_weight_units: int = Field(
        ge=-DECLARED_WEIGHT_TOTAL_UNITS,
        le=DECLARED_WEIGHT_TOTAL_UNITS,
        strict=True,
    )
    reason_code: AdjustmentReason
    explanation: str = Field(min_length=1, max_length=500)

    @field_validator("delta_weight_units")
    @classmethod
    def reject_noop_adjustment(cls, value: int) -> int:
        if value == 0:
            raise ValueError("delta_weight_units must not be zero")
        return value


class CreateOverlayRequest(StrictModel):
    review_reference: str = Field(min_length=1, max_length=200)
    adjustments: list[OverlayAdjustmentInput] = Field(min_length=2, max_length=1000)


class ReviewOverlayContent(StrictModel):
    schema_version: Literal["attribution-review-overlay/v1"] = OVERLAY_SCHEMA_VERSION
    base_manifest_hash: Sha256Hex
    review_reference: str
    adjustments: list[OverlayAdjustmentInput]


class ReviewOverlay(StrictModel):
    content: ReviewOverlayContent
    overlay_hash: Sha256Hex


class StoredOverlayResponse(StrictModel):
    overlay: ReviewOverlay
    created: bool


class AdjustedContributor(StrictModel):
    contributor_id: str
    display_name: str
    weight_units: int = Field(
        ge=0,
        le=DECLARED_WEIGHT_TOTAL_UNITS,
        strict=True,
    )


class AppliedAttribution(StrictModel):
    base_manifest_hash: Sha256Hex
    overlay_hash: Optional[Sha256Hex] = None
    declared_weight_total_units: Literal[1_000_000] = DECLARED_WEIGHT_TOTAL_UNITS
    contributors: list[AdjustedContributor]


class PayoutPreviewRequest(StrictModel):
    amount_minor_units: int = Field(gt=0, le=10**18, strict=True)
    currency: str = Field(min_length=3, max_length=12, pattern=r"^[A-Za-z0-9_-]+$")
    minimum_payout_minor_units: int = Field(
        default=0,
        ge=0,
        le=10**18,
        strict=True,
    )
    overlay_hash: Optional[Sha256Hex] = None

    @field_validator("currency")
    @classmethod
    def normalize_currency(cls, value: str) -> str:
        return value.upper()


class PayoutAllocation(StrictModel):
    contributor_id: str
    weight_units: int = Field(
        gt=0,
        le=DECLARED_WEIGHT_TOTAL_UNITS,
        strict=True,
    )
    amount_minor_units: int = Field(ge=0, strict=True)


class PayoutPreview(StrictModel):
    calculation_version: Literal["payout-preview/v1"] = PAYOUT_CALCULATION_VERSION
    manifest_hash: Sha256Hex
    overlay_hash: Optional[Sha256Hex] = None
    currency: str
    available_minor_units: int = Field(gt=0)
    minimum_payout_minor_units: int = Field(ge=0)
    allocations: list[PayoutAllocation]
    idempotency_key: Sha256Hex
