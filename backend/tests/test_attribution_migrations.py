"""Alembic topology and reversible attribution migration checks."""

from __future__ import annotations

import importlib.util
from pathlib import Path
from unittest.mock import patch

from alembic.config import Config
from alembic.script import ScriptDirectory

BACKEND_ROOT = Path(__file__).resolve().parents[1]
MIGRATION_PATH = BACKEND_ROOT / "alembic" / "versions" / "20260902_0010_attribution_manifests.py"


def test_alembic_has_one_linear_head() -> None:
    config = Config(str(BACKEND_ROOT / "alembic.ini"))
    config.set_main_option("script_location", str(BACKEND_ROOT / "alembic"))
    script = ScriptDirectory.from_config(config)

    assert script.get_heads() == ["20260902_0010"]
    assert script.get_revision("20260902_0010").down_revision == "20260221_0009"


def test_attribution_migration_upgrade_and_rollback_are_inverse() -> None:
    spec = importlib.util.spec_from_file_location("attribution_migration_0010", MIGRATION_PATH)
    assert spec is not None and spec.loader is not None
    migration = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(migration)

    created: list[str] = []
    dropped: list[str] = []
    with (
        patch.object(
            migration.op,
            "create_table",
            side_effect=lambda table_name, *args, **kwargs: created.append(table_name),
        ),
        patch.object(migration.op, "create_index"),
    ):
        migration.upgrade()
    with (
        patch.object(migration.op, "drop_index"),
        patch.object(
            migration.op,
            "drop_table",
            side_effect=lambda table_name, *args, **kwargs: dropped.append(table_name),
        ),
    ):
        migration.downgrade()

    assert created == ["attribution_manifests", "attribution_review_overlays"]
    assert dropped == ["attribution_review_overlays", "attribution_manifests"]
