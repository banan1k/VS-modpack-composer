"""Regression test for relation-edge merges that used to violate UNIQUE constraints.

The algorithm is executed with a tiny async-session double so it can be tested without requiring
an actual SQLite async driver in test environments where aiosqlite is unavailable.
"""
import ast
from pathlib import Path
from types import SimpleNamespace

ROOT = Path(__file__).parents[1]


class _Column:
    def __init__(self, name):
        self.name = name


class _FakeModel:
    id = _Column("id")


class _Query:
    def __init__(self, model):
        self.model = model

    def order_by(self, *_args):
        return self


class _Scalars:
    def __init__(self, rows):
        self._rows = rows

    def all(self):
        return list(self._rows)


class _Result:
    def __init__(self, rows):
        self._rows = rows

    def scalars(self):
        return _Scalars(self._rows)


class _FakeSession:
    def __init__(self, model, rows, canonical):
        self.rows_by_model = {model: rows}
        self.deleted = []
        self.flush_count = 0
        self.canonical = canonical
        self.deletion_snapshot = None

    async def execute(self, query):
        return _Result(self.rows_by_model[query.model])

    async def delete(self, row):
        self.deleted.append(row)

    async def flush(self):
        self.flush_count += 1
        if self.flush_count == 1:
            self.deletion_snapshot = {row.id for row in self.deleted}
            for model, rows in self.rows_by_model.items():
                self.rows_by_model[model] = [row for row in rows if row not in self.deleted]

    async def get(self, _model, _identity):
        return self.canonical


def _load_repoint_function():
    source = (ROOT / "app/db.py").read_text(encoding="utf-8")
    tree = ast.parse(source)
    node = next(
        child for child in tree.body
        if isinstance(child, ast.AsyncFunctionDef) and child.name == "_repoint_relation_edges"
    )
    # Inject dependencies to avoid importing app.db / aiosqlite in this isolated algorithm test.
    node.body = [
        child for child in node.body
        if not (isinstance(child, ast.ImportFrom) and (child.module == "sqlalchemy" or child.level > 0))
    ]
    namespace = {
        "select": lambda model: _Query(model),
        "Mod": _FakeModel,
    }
    exec(compile(ast.Module(body=[node], type_ignores=[]), str(ROOT / "app/db.py"), "exec"), namespace)
    return namespace["_repoint_relation_edges"]


def _edge(edge_id, source_id, target_id, confidence, verified=False, required_version=None):
    return SimpleNamespace(
        id=edge_id,
        mod_id=source_id,
        target_mod_id=target_id,
        relation_type="dependency",
        confidence=confidence,
        verified=verified,
        required_version=required_version,
        target_name=None,
        target_url=None,
        source_kind="modinfo",
        raw_phrase=None,
        evidence=None,
    )


def test_repointing_duplicate_source_deletes_conflicting_edge_before_key_change():
    import asyncio

    repoint = _load_repoint_function()
    # Row 1 references duplicate source mod 10. Row 2 already points from canonical mod 11 to
    # the same target; both would end up with the same UNIQUE key after merge.
    duplicate_edge = _edge(1, 10, 90, 0.64)
    canonical_edge = _edge(2, 11, 90, 1.0, verified=True, required_version="0.8.4")
    model = type("Dependency", (), {"id": _Column("id")})
    canonical = SimpleNamespace(
        name="Canonical",
        mod_db_url="https://mods.example/canonical",
    )
    session = _FakeSession(model, [duplicate_edge, canonical_edge], canonical=canonical)

    asyncio.run(repoint(session, model, duplicate_id=10, canonical_id=11))

    remaining = session.rows_by_model[model]
    assert [edge.id for edge in remaining] == [2]
    assert duplicate_edge.id in (session.deletion_snapshot or set())
    assert canonical_edge.confidence == 1.0
    assert canonical_edge.verified is True
    assert canonical_edge.required_version == "0.8.4"


def test_repointing_duplicate_target_does_not_leave_two_identical_edges():
    import asyncio

    repoint = _load_repoint_function()
    duplicate_target_edge = _edge(1, 5, 10, 0.64)
    canonical_target_edge = _edge(2, 5, 11, 0.92, verified=True)
    model = type("Dependency", (), {"id": _Column("id")})
    canonical = SimpleNamespace(
        name="Canonical",
        mod_db_url="https://mods.example/canonical",
    )
    session = _FakeSession(model, [duplicate_target_edge, canonical_target_edge], canonical=canonical)

    asyncio.run(repoint(session, model, duplicate_id=10, canonical_id=11))

    remaining = session.rows_by_model[model]
    keys = [(edge.mod_id, edge.target_mod_id, edge.relation_type) for edge in remaining]
    assert len(keys) == len(set(keys)) == 1
    assert keys[0] == (5, 11, "dependency")
    assert duplicate_target_edge.id in (session.deletion_snapshot or set())
