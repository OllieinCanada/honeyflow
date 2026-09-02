Create venv, install deps, then run migrations:

`alembic upgrade head`

Run API:

`uvicorn app.main:app --host 0.0.0.0 --port 8000 --reload`

LLM inference is handled by 0G via nextjs API at `http://localhost:3000/api/inference`

## Contribution provenance

An opt-in provenance sidecar can record immutable, verifiable snapshots around
the existing repository attribution output. It is disabled by default and does
not replace or modify attribution, jury, earnings, withdrawal, or payout data.
See [docs/provenance.md](docs/provenance.md) for setup, verification, migration,
privacy, and assurance boundaries.
