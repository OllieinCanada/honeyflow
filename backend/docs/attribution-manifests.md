# Attribution Manifest V1

Honeyflow's attribution manifest is a replayable evidence record for one
repository revision. It answers _how did this configured algorithm allocate its
units?_ It does not claim that repository activity is an objective measure of
value or ownership.

## Lifecycle and invariants

1. `POST /attribution/manifests` validates repository evidence and applies the
   configured deterministic rules.
2. The service stores an immutable base manifest. A source key binds project,
   repository, source commit, algorithm version, and configuration fingerprint.
   Repeating an identical request returns the stored artifact; conflicting
   evidence for the same source key returns `409`.
3. `POST /attribution/manifests/{hash}/overlays` stores a separate, immutable
   human-review overlay. Adjustments must be zero-sum, reference known
   contributors, and never create a negative weight.
4. `POST /attribution/manifests/{hash}/payout-previews` calculates an exact
   integer dry run. It has no transfer or wallet side effect.

The base contributor weights total exactly `1_000_000` integer units. All
normalization and payout remainder handling use integer division with largest
remainders and contributor-ID lexical tie-breaking. Binary floating point is
rejected by the canonical serializer.

The content hash uses compact UTF-8 JSON with recursively sorted object keys.
Manifest V1 restricts hashed content to integers, strings, booleans, nulls,
arrays, and string-keyed objects. This is a deliberately narrow versioned
canonical form, not a general claim of JSON Canonicalization Scheme support.
Evaluation timestamps are excluded: logically identical evidence produces the
same bytes and hash regardless of wall-clock time.

Before use, stored manifests are revalidated against their schema, outer
content hash, configuration fingerprint, exact weight total, evidence hashes,
and contributor-to-evidence references. Stored overlays are likewise checked
for their content hash, unique contributor adjustments, and zero-sum invariant.

## Identity and evidence rules

- Normalized GitHub login tokens and normalized email hashes are strong identity
  tokens within the caller-supplied evidence set. V1 does not verify control of
  either identity; two weak display-name matches are never silently merged.
- A value supplied through the email field is normalized and SHA-256 hashed
  before it can enter manifest or configuration content. The hash is
  pseudonymous, not anonymous; source email data should still be access-controlled
  and minimized.
- Explicit alias declarations require a login or email for every identity.
  The declared canonical identity is preferred, while ambiguous display names
  remain separate and are reported as potential-alias evidence.
- Co-authors share a commit's configured integer units exactly.
- Included file evidence records additions, deletions, and the configured capped
  line count used by the scorer. Evidence has a stable reason code when a file
  change exceeds that cap, so the manifest can explain its aggregate.
- Duplicate commits, merges, bots, generated paths, vendored paths, binary
  files, missing/empty author evidence, and records assigned zero configured
  units are recorded as explicit exclusions rather than disappearing silently.
- Generated/vendor/bot behavior is configuration-driven. The configuration is
  normalized into a privacy-safe manifest field and fingerprinted into the
  source key. Each applied alias rule records its canonical strong token and
  sorted member tokens; email aliases appear only as hashes in that field.

The request contract accepts pre-collected repository evidence. Collecting Git
history or resolving provider identities is deliberately outside this slice;
callers remain responsible for supplying complete evidence for the source SHA.

## API and authorization

Set a high-entropy `ATTRIBUTION_ADMIN_TOKEN` in the backend environment. Send it
to every attribution endpoint as `X-Attribution-Admin-Token`. The value is
represented as a secret setting, compared with a constant-time primitive, and
never persisted in an artifact. If the setting is absent, endpoints return
`503`; missing or incorrect credentials return `401`.

Reads and payout previews are protected even though they have no transfer side
effect because manifests contain contributor identity and repository-path
evidence. This single deployment token is only a narrow fail-closed boundary;
the v1 schema has no tenant or public-publication model and must not be treated
as one.

Representative request:

```json
{
  "project_identity": "owner/repository",
  "source_repository_url": "https://github.com/owner/repository",
  "source_commit_sha": "0123456789012345678901234567890123456789",
  "records": [
    {
      "record_id": "commit-1",
      "commit_sha": "1111111111111111111111111111111111111111",
      "author": {
        "display_name": "Synthetic Contributor",
        "github_login": "synthetic-contributor"
      },
      "files": [{"path": "src/example.py", "additions": 4}]
    }
  ]
}
```

Strict Pydantic schemas reject unknown fields, invalid object IDs, absolute or
parent-relative paths, negative line counts, malformed overlays, and noninteger
money/weight values. API errors contain stable `code` and human-readable
`message` fields.

## Payout-preview semantics

The caller supplies an available amount in exact minor units and a currency
identifier. The preview:

- never creates a transfer;
- never produces a negative amount;
- allocates the complete available amount exactly;
- handles dust with stable lexical tie-breaking;
- applies the optional minimum threshold to the preliminary allocation, then
  redistributes the complete amount among eligible contributors;
- derives an idempotency key from immutable manifest/overlay hashes and all
  calculation inputs.

The threshold is a planning policy, not an implicit fee or discarded balance.
If it excludes everyone, the request fails explicitly.

## Migration, rollout, and rollback

Revision `20260902_0010` creates `attribution_manifests` and
`attribution_review_overlays`. It also makes the prior `0009` cover-image
migration idempotent and removes a redundant file that reused revision ID
`20260221_0008`; that duplicate previously prevented Alembic from constructing a
linear history.

```console
cd backend
alembic upgrade head
# Roll back only this feature:
alembic downgrade 20260221_0009
```

Rollback drops the overlay table before its manifest parent. Existing project,
graph, and jury tables are unchanged. Treat rollback as destructive for newly
created attribution artifacts and export them before downgrading if they must be
retained.

## Reproduction and benchmark

Install development dependencies, run the full test suite, and replay the
recorded 2,000-commit/100-contributor synthetic benchmark:

```console
python -m pip install -r requirements-dev.txt
python -m ruff format --check app/attribution app/schemas/attribution.py \
  app/routes/attribution.py app/models/attribution.py tests benchmarks
python -m ruff check app/attribution app/schemas/attribution.py \
  app/routes/attribution.py app/models/attribution.py tests benchmarks
python -m mypy --follow-imports=silent \
  app/attribution app/schemas/attribution.py \
  app/routes/attribution.py app/models/attribution.py app/config.py app/main.py \
  app/models/__init__.py
python -m pytest
python -m benchmarks.attribution_medium \
  --verify tests/fixtures/attribution_medium_expected.json
```

The recorded benchmark hash is a correctness/replay baseline, not a latency
service-level objective. Runtime is emitted for observation but excluded from
the expected artifact because it is machine-dependent. The core suite uses only
synthetic evidence and needs no GitHub, model, wallet, or payment API.

Set `TEST_DATABASE_URL` (and the same value as `DATABASE_URL`) to enable the
PostgreSQL concurrent-write integration test. CI provisions an ephemeral
PostgreSQL instance and also exercises upgrade, downgrade, and re-upgrade.

## Limitations and follow-ups

- Commit and line units are configured proxies. They do not capture design,
  support, governance, or off-repository work unless evidence is deliberately
  added in a later version.
- Email hashes can be guessed when the source address is known. V1 prevents the
  identity email field itself from being emitted but does not promise anonymity.
  Callers must separately minimize display names, repository paths, and other
  evidence fields that could themselves contain sensitive text.
- The source evidence collector and reviewer identity/audit log are adapter
  boundaries for follow-up work. The overlay's `review_reference` must point to
  the deployment's durable review record.
- The preview does not validate payout destinations, regulatory requirements,
  sanctions, tax treatment, balances, or transfer authorization. Those belong
  in a separately reviewed execution system.
- The repository currently has no declared root license. This change introduces
  original code only and does not resolve redistribution rights for the project.
