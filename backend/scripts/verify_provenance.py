"""Offline Honeyflow provenance verifier.

Run from ``backend`` with ``python -m scripts.verify_provenance manifest.json``.
The command performs no database or network access.
"""

from __future__ import annotations

import argparse
import json
from pathlib import Path
from typing import Any, Sequence

from app.services.provenance import verify_manifest

MAX_MANIFEST_BYTES = 5_000_000


def _object_without_duplicate_keys(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for key, value in pairs:
        if key in result:
            raise ValueError("duplicate JSON key: {}".format(key))
        result[key] = value
    return result


def load_manifest(path: Path) -> dict[str, Any]:
    size = path.stat().st_size
    if size > MAX_MANIFEST_BYTES:
        raise ValueError("manifest exceeds 5 MB input bound")
    raw = path.read_bytes()
    if len(raw) != size:
        raise ValueError("manifest changed while being read")
    value = json.loads(
        raw.decode("utf-8"),
        object_pairs_hook=_object_without_duplicate_keys,
        parse_constant=lambda constant: (_ for _ in ()).throw(
            ValueError("non-finite JSON number: {}".format(constant))
        ),
    )
    if not isinstance(value, dict):
        raise ValueError("manifest root must be a JSON object")
    return value


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Verify a Honeyflow provenance manifest")
    parser.add_argument("manifest", type=Path)
    args = parser.parse_args(argv)
    try:
        result = verify_manifest(load_manifest(args.manifest))
    except (OSError, UnicodeError, ValueError, json.JSONDecodeError) as exc:
        print(
            json.dumps(
                {
                    "valid": False,
                    "errors": ["manifest_parse_error"],
                    "error_type": type(exc).__name__,
                },
                sort_keys=True,
            )
        )
        return 2
    print(json.dumps(result.model_dump(mode="json"), sort_keys=True))
    return 0 if result.valid else 1


if __name__ == "__main__":
    raise SystemExit(main())
