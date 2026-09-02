# Honeyflow backend

Create a virtual environment, install `requirements.txt`, set `DATABASE_URL`,
then run migrations:

```console
alembic upgrade head
uvicorn app.main:app --host 0.0.0.0 --port 8000 --reload
```

LLM inference is handled by 0G through the Next.js API at
`http://localhost:3000/api/inference`.

## Deterministic attribution manifests

The versioned attribution pipeline creates an immutable evidence manifest,
keeps human review in a separate overlay, and can produce an exact dry-run
payout preview. It does not send funds. See
[`docs/attribution-manifests.md`](docs/attribution-manifests.md) for the data
contract, security boundary, API, migrations, benchmark, and limitations.

All attribution endpoints fail closed until `ATTRIBUTION_ADMIN_TOKEN` is
configured.
