"""Replay the recorded medium synthetic attribution benchmark."""

from __future__ import annotations

import json
from pathlib import Path

from benchmarks.attribution_medium import run_benchmark

EXPECTED = Path(__file__).parent / "fixtures" / "attribution_medium_expected.json"


def test_medium_synthetic_benchmark_matches_recorded_result() -> None:
    expected = json.loads(EXPECTED.read_text(encoding="utf-8"))
    result = run_benchmark()
    result.pop("elapsed_ms")

    assert result == expected
