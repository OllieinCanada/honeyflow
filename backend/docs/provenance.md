# Contribution provenance manifests

Honeyflow can wrap a repository attribution result in a versioned
`honeyflow.provenance/v1` manifest. The manifest records the public repository,
an exact observed source commit, SHA-256 lockfile digests, every effective graph
weight/limit, accepted inference action/provider/model/prompt-template identifiers,
aggregate human-prior input digests, identity decisions, conservative path labels,
bounded graph edges and evidence, the original attribution, ordered jury events,
and integrity diagnostics.

This is an integrity sidecar. `PROVENANCE_ENABLED` defaults to `false`, and no
manifest field is read by attribution or payout code. Enabling it adds bounded
GitHub source-material requests for public repository traces. Missing source
metadata, a moving source HEAD, oversized trees/lockfiles, or storage failures
skip the sidecar without changing the existing trace result.

## Set up and verify

From `backend`:

```bash
python -m venv .venv
python -m pip install -r requirements-dev.txt
python -m pytest
DATABASE_URL=postgresql://... python -m alembic upgrade head
```

Enable generation only after the additive migration is deployed:

```bash
PROVENANCE_ENABLED=true uvicorn app.main:app --host 127.0.0.1 --port 8000
```

Project details are already public in this application, so provenance uses the
same public-read boundary rather than introducing an authorization claim:

```text
GET /projects/{slug}/provenance
GET /projects/{slug}/provenance?digest={64-lowercase-hex-digest}
```

The collector refuses to create manifests for repositories GitHub reports as
non-public, rechecks visibility before storage, and fails closed when visibility
cannot be reconfirmed before a read response. Responses contain only allowlisted
manifest fields: no email, wallet, token, raw prompt, patch, file content, or
contributor contact fields. Repository paths are limited to the bounded,
classified public blob inventory and observed lockfiles.

Save the `manifest` object from the response and verify it offline, with no
database, GitHub, model, or blockchain service:

```bash
python -m scripts.verify_provenance manifest.json
```

The command accepts at most 5 MB, rejects duplicate JSON keys and non-finite
numbers, and exits `0` for valid, `1` for a well-formed digest mismatch, or `2`
for unreadable/invalid JSON. API and CLI verification share the same pure
implementation.

## Integrity and privacy design

- `honeyflow-canonical-json-v1` sorts object keys and semantically unordered
  manifest collections, uses compact UTF-8 JSON, requires NFC strings and
  interoperable integers, and rejects negative zero, non-finite values,
  non-string keys, unsupported types, excessive depth, and excessive size. It
  is a project-specific profile and does **not** claim RFC 8785 conformance.
- The digest is SHA-256 over an explicit allowlisted manifest projection.
  `manifest_digest` and `created_at` are excluded. Consequently, the timestamp
  is informational and is not protected by the digest. Every other v1 field,
  including unknown excluded paths and the previous snapshot digest, is covered.
- Jury updates insert a new immutable snapshot linked by `previous_digest`.
  Events use database vote IDs, ordered sequences, weights, and confidence; they
  omit wallet addresses. Existing snapshot JSON is never updated.
- The database serializes snapshot writers on the project row and enforces
  positive sequence values plus unique `(project_id, sequence)` and
  `(project_id, digest)` pairs.
- Local repository analysis delegates `.mailmap` semantics to bounded
  `git check-mailmap --stdin` calls, then discards email addresses. Remote
  Honeyflow traces expose GitHub labels only and emit a diagnostic that mailmap
  data was unavailable; they do not infer account ownership.
- Generated, vendored, documentation, test, bot, and first-party labels are
  conservative Honeyflow heuristics. They are diagnostics, not exclusions,
  authorship proof, or a claim that GitHub Linguist ran.
- Graph collection rejects duplicate/dangling nodes, bounds nodes/edges/depth,
  and performs cycle detection in linear time. Cycles produce a warning rather
  than an unbounded traversal.
- The Next inference endpoint preserves its existing `result` field and adds
  allowlisted provenance metadata. It reports the provider and model that
  actually returned the accepted response (including Gemini fallback) plus a
  versioned prompt-template ID; raw prompts and responses are never captured.
  An older inference endpoint remains compatible and produces an explicit
  `unavailable` metadata event rather than guessed identifiers.
- Provenance capture records human-prior inputs only when the observed snapshot
  contains at most 5,000 rows. Larger existing prior sets continue through the
  unchanged attribution path, while the sidecar records an explicit overflow
  warning and omits the incomplete digest. For captured sets, the manifest stores
  only an aggregate SHA-256 digest, record count, and entity type—not prior names,
  values, votes, or wallets. All effective `Settings.graph` values and request
  depth/child limits are included in the digest.
- Public blob paths are sorted and capped at 5,000 for conservative labels.
  Lockfile collection is capped at 25 files, 2 MB per file and 10 MB total;
  blob requests run concurrently behind the existing 15-request semaphore and
  per-request timeout. Truncation is explicit. Lockfile evidence IDs are stable
  path digests and do not claim commit-level authorship.

Never put a credential or private input in a manifest. Structured runtime logs
record only project IDs and error types, not manifest payloads.

## Assurance boundaries

A valid digest detects accidental or adversarial changes relative to a digest
obtained through a trusted channel. It is not a signature, trusted timestamp,
identity proof, commit-signature check, or proof that recorded evidence is true.
This format is informed by SLSA, SPDX, and in-toto vocabulary but is not a SLSA,
SPDX, in-toto, or Sigstore attestation. The current trace fetches source files by
HEAD while guarding that HEAD did not change between checks; requests are not
fully pinned throughout the existing attribution pipeline, and the manifest
reports that limitation.

This vertical slice intentionally omits a frontend badge. The public JSON route
and offline verifier provide independently reviewable value without coupling
integrity data to the payout UI.

## Migration and rollback

The migration adds only `provenance_snapshots`. It also repairs the previously
ambiguous migration tail: redundant `0008_add_cover_image_url` is removed because
idempotent `0007` owns that column, and `0009` becomes an idempotent compatibility
repair for `cover_image_data`. No applied revision identifier is renamed.

Roll back application behavior first by setting `PROVENANCE_ENABLED=false`.
Leaving the additive table in place is safe and preserves audit history. If the
schema must be removed, export required manifests and run one reviewed downgrade
from `20260902_0010`; this deletes only provenance snapshots. Existing attribution
and payout columns are untouched.

## Primary sources

Design was informed by [SLSA](https://slsa.dev/spec/v1.2/build-provenance),
[SPDX](https://spdx.github.io/spdx-spec/v3.0.1/), [in-toto](https://github.com/in-toto/attestation/tree/main/spec/v1), [Git mailmap](https://git-scm.com/docs/gitmailmap), [Linguist](https://github.com/github-linguist/linguist/blob/master/docs/how-linguist-works.md), [Sigstore](https://docs.sigstore.dev/cosign/verifying/verify/), [RFC 8785](https://www.rfc-editor.org/rfc/rfc8785), and [Reproducible Builds](https://reproducible-builds.org/docs/stable-inputs/). The PR records each implementation consequence.
