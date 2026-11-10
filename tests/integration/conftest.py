"""Fixtures every Postgres-backed test module gets."""

from __future__ import annotations

import os
from collections.abc import Iterator

import pytest
import yoyo.migrations

_parsed: dict[tuple[str, int, int], tuple[dict[str, str], str, list[str]]] = {}
_read_sql_migration = yoyo.migrations.read_sql_migration


def _read_sql_migration_once(path: str) -> tuple[dict[str, str], str, list[str]]:
    """yoyo's ``read_sql_migration``, parsing each file once per process.

    Keyed on the path with its mtime and size, so an edited file is parsed
    again. The caller gets its own dict and list: yoyo reverses the
    rollback statements in place.
    """
    try:
        stat = os.stat(path)
    except OSError:  # a migration without a .rollback.sql
        return _read_sql_migration(path)
    key = (path, stat.st_mtime_ns, stat.st_size)
    if key not in _parsed:
        _parsed[key] = _read_sql_migration(path)
    directives, leading_comment, statements = _parsed[key]
    return dict(directives), leading_comment, list(statements)


@pytest.fixture(autouse=True, scope="session")
def _parse_each_migration_file_once() -> Iterator[None]:
    """Every ``migrate`` and ``rollback`` builds the chain from scratch, and
    yoyo splits all sixty-odd SQL files with sqlparse to do it: about 45 ms
    of pure parsing, three times per rollback. The files do not change
    during a run, so the split is remembered. What is applied, in which
    order and against which database is still decided by the real code on
    every call; ``tests/unit/migration_checksums.txt`` pins the file contents.
    """
    with pytest.MonkeyPatch.context() as patch:
        patch.setattr(yoyo.migrations, "read_sql_migration", _read_sql_migration_once)
        yield
