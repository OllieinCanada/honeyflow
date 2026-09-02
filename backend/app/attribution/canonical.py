"""Canonical serialization for attribution content hashes.

The v1 schemas deliberately use bounded integers rather than floating-point
numbers. With fixed ASCII property names this compact, recursively sorted JSON
form is stable across supported Python versions and is suitable for replayable
content hashes. It is not advertised as a general-purpose JCS implementation.
"""

from __future__ import annotations

import hashlib
import json
from typing import Any

from pydantic import BaseModel


def _json_value(value: Any) -> Any:
    if isinstance(value, BaseModel):
        return _json_value(value.model_dump(mode="json"))
    if isinstance(value, dict):
        return {key: _json_value(child) for key, child in value.items()}
    if isinstance(value, (list, tuple)):
        return [_json_value(child) for child in value]
    return value


def _reject_floats(value: Any, path: str = "$") -> None:
    if isinstance(value, float):
        raise TypeError("floating-point value is not canonical at {}".format(path))
    if isinstance(value, dict):
        for key, child in value.items():
            if not isinstance(key, str):
                raise TypeError("canonical object keys must be strings at {}".format(path))
            _reject_floats(child, "{}.{}".format(path, key))
    elif isinstance(value, (list, tuple)):
        for index, child in enumerate(value):
            _reject_floats(child, "{}[{}]".format(path, index))


def canonical_json_bytes(value: Any) -> bytes:
    payload = _json_value(value)
    _reject_floats(payload)
    return json.dumps(
        payload,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
        allow_nan=False,
    ).encode("utf-8")


def content_hash(value: Any) -> str:
    return hashlib.sha256(canonical_json_bytes(value)).hexdigest()
