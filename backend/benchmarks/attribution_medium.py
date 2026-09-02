"""Deterministic medium attribution benchmark and replay fixture."""

from __future__ import annotations

import argparse
import hashlib
import json
import time
from pathlib import Path

from app.attribution.domain import build_manifest
from app.schemas.attribution import CreateManifestRequest

SCENARIO_VERSION = "medium-synthetic/v1"
DEFAULT_SEED = 20_260_902
DEFAULT_RECORD_COUNT = 2_000
DEFAULT_CONTRIBUTOR_COUNT = 100


def _object_id(value: str) -> str:
    return hashlib.sha1(value.encode("utf-8")).hexdigest()  # noqa: S324 - Git fixture ID


def build_scenario(
    *,
    seed: int = DEFAULT_SEED,
    record_count: int = DEFAULT_RECORD_COUNT,
    contributor_count: int = DEFAULT_CONTRIBUTOR_COUNT,
) -> CreateManifestRequest:
    records = []
    for index in range(record_count):
        author_index = index % contributor_count
        coauthor_index = (index * 7 + 13) % contributor_count
        files = [
            {
                "path": "src/module_{:03d}.py".format(index % 50),
                "additions": (index % 17) + 1,
                "deletions": index % 5,
            }
        ]
        if index % 10 == 0:
            files.append(
                {
                    "path": "dist/generated_{:04d}.map".format(index),
                    "additions": 500,
                }
            )
        records.append(
            {
                "record_id": "commit-{:04d}".format(index),
                "commit_sha": _object_id("{}:commit:{}".format(seed, index)),
                "author": {
                    "display_name": "Contributor {:03d}".format(author_index),
                    "github_login": "contributor-{:03d}".format(author_index),
                },
                "coauthors": [
                    {
                        "display_name": "Contributor {:03d}".format(coauthor_index),
                        "github_login": "contributor-{:03d}".format(coauthor_index),
                    }
                ],
                "files": files,
            }
        )
    return CreateManifestRequest.model_validate(
        {
            "project_identity": "benchmark/honeyflow-medium",
            "source_repository_url": "https://github.com/example/honeyflow-benchmark",
            "source_commit_sha": _object_id("{}:source".format(seed)),
            "records": list(reversed(records)),
        }
    )


def run_benchmark() -> dict[str, int | float | str]:
    request = build_scenario()
    started = time.perf_counter()
    manifest = build_manifest(request)
    elapsed_ms = round((time.perf_counter() - started) * 1000, 3)
    return {
        "scenario_version": SCENARIO_VERSION,
        "seed": DEFAULT_SEED,
        "record_count": DEFAULT_RECORD_COUNT,
        "contributor_count": len(manifest.content.canonical_contributors),
        "evidence_count": len(manifest.content.evidence_records),
        "exclusion_count": len(manifest.content.exclusions),
        "manifest_content_hash": manifest.manifest_content_hash,
        "elapsed_ms": elapsed_ms,
    }


def _stable_result(result: dict[str, int | float | str]) -> dict[str, int | str]:
    return {
        key: value
        for key, value in result.items()
        if key != "elapsed_ms" and isinstance(value, (int, str))
    }


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--verify",
        type=Path,
        help="compare deterministic fields with a recorded JSON result",
    )
    args = parser.parse_args()
    result = run_benchmark()
    print(json.dumps(result, indent=2, sort_keys=True))
    if args.verify is not None:
        expected = json.loads(args.verify.read_text(encoding="utf-8"))
        if _stable_result(result) != expected:
            return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
