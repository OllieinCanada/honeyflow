import ast
from pathlib import Path


def _literal_assignment(path: Path, name: str):
    tree = ast.parse(path.read_text(encoding="utf-8"))
    for statement in tree.body:
        if isinstance(statement, ast.Assign):
            if any(isinstance(target, ast.Name) and target.id == name for target in statement.targets):
                return ast.literal_eval(statement.value)
        if isinstance(statement, ast.AnnAssign) and isinstance(statement.target, ast.Name):
            if statement.target.id == name:
                return ast.literal_eval(statement.value)
    raise AssertionError("{} missing from {}".format(name, path.name))


def test_alembic_revisions_are_unique_linear_and_have_one_head():
    versions = Path(__file__).parents[1] / "alembic" / "versions"
    revisions = {}
    for path in versions.glob("*.py"):
        revision = _literal_assignment(path, "revision")
        assert revision not in revisions, "duplicate revision {}".format(revision)
        revisions[revision] = (_literal_assignment(path, "down_revision"), path.name)
    children = {parent for parent, _ in revisions.values() if parent is not None}
    heads = set(revisions) - children
    assert heads == {"20260902_0010"}
    current = "20260902_0010"
    visited = set()
    while current is not None:
        assert current in revisions
        assert current not in visited
        visited.add(current)
        current = revisions[current][0]
    assert visited == set(revisions)


def test_provenance_feature_defaults_off_without_importing_runtime_settings():
    config_path = Path(__file__).parents[1] / "app" / "config.py"
    tree = ast.parse(config_path.read_text(encoding="utf-8"))
    settings = next(node for node in tree.body if isinstance(node, ast.ClassDef) and node.name == "Settings")
    assignment = next(
        node
        for node in settings.body
        if isinstance(node, ast.AnnAssign)
        and isinstance(node.target, ast.Name)
        and node.target.id == "provenance_enabled"
    )
    assert ast.literal_eval(assignment.value) is False
