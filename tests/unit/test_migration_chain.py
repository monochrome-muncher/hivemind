"""The migration chain's shape invariants (ADR 0020). Hermetic — no DB.

Two properties, both of which only fail *after* a migration has shipped,
which is exactly when they are impossible to notice by eye:

1. **Applied migrations are immutable — and so are their rollbacks.**
   yoyo records what it applied by id, not by content: editing an
   already-applied file changes what a *fresh* pool gets while every
   existing pool keeps the old shape — the same silent fresh-vs-upgraded
   divergence that killed the declarative re-apply (ADR 0020). A
   `.rollback.sql` is executable DDL that ships in the image too: a pool
   rolled back with an edited file diverges from one rolled back with
   the original, the same class of bug. The committed checksum file
   pins each shipped migration AND its rollback; changing one is a CI
   failure, and the fix is a NEW migration.

2. **The chain is well-formed.** Ids sort in apply order, every `.sql`
   migration has a rollback companion, and `0001` has none (reversing
   it would drop the pool — ADR 0020).
"""

from __future__ import annotations

import hashlib
import re
from pathlib import Path

import pytest

MIGRATIONS_DIR = Path(__file__).resolve().parents[2] / "src" / "hivemind" / "store" / "migrations"
CHECKSUMS = Path(__file__).with_name("migration_checksums.txt")


def _migration_files() -> list[Path]:
    """Every migration file (not rollbacks), in apply order."""
    return sorted(
        p
        for p in MIGRATIONS_DIR.iterdir()
        if p.suffix in {".py", ".sql"} and not p.name.endswith(".rollback.sql")
    )


def _pinnable_files() -> list[Path]:
    """Every migration file AND its rollback (ADR 0020): rollbacks ship in
    the image and are just as frozen as the migration they reverse."""
    return sorted(p for p in MIGRATIONS_DIR.iterdir() if p.suffix in {".py", ".sql"})


def _digest(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _pinned() -> dict[str, str]:
    pins: dict[str, str] = {}
    for line in CHECKSUMS.read_text().splitlines():
        line = line.strip()
        if not line or line.startswith("#"):
            continue
        digest, name = line.split(None, 1)
        pins[name] = digest
    return pins


class TestMigrationsAreImmutable:
    """A shipped migration — and its rollback — is frozen. Corrections are
    new migrations."""

    def test_every_migration_is_pinned(self) -> None:
        pins = _pinned()
        unpinned = [p.name for p in _pinnable_files() if p.name not in pins]
        assert not unpinned, (
            f"new migration/rollback file(s) {unpinned} are not pinned. Add their "
            f"sha256 to {CHECKSUMS.name} in the same commit that introduces them."
        )

    def test_no_pinned_migration_has_changed(self) -> None:
        pins = _pinned()
        changed = [
            p.name for p in _pinnable_files() if p.name in pins and _digest(p) != pins[p.name]
        ]
        assert not changed, (
            f"applied migration/rollback file(s) {changed} were edited. yoyo tracks "
            "migrations by id, not content, so an edit changes what a FRESH pool "
            "gets while every existing pool keeps the old shape (and a rollback is "
            "executable DDL that ships in the image, so the same divergence applies "
            "to it). Revert the edit and write a NEW migration instead (ADR 0020)."
        )

    def test_pins_do_not_reference_deleted_migrations(self) -> None:
        names = {p.name for p in _pinnable_files()}
        orphans = [n for n in _pinned() if n not in names]
        assert not orphans, (
            f"pinned migration/rollback file(s) {orphans} no longer exist. A shipped "
            "migration is never deleted — pools that applied it would silently diverge."
        )


class TestChainIsWellFormed:
    def test_chain_is_not_empty(self) -> None:
        assert _migration_files(), "the migration chain is empty"

    def test_ids_are_zero_padded_and_unique(self) -> None:
        # yoyo orders by __depends__ then filename, so lexical order must
        # equal numeric order: 0002 before 0010, never "2" before "10".
        prefixes = [p.name.split(".", 1)[0] for p in _migration_files()]
        assert all(len(x) == 4 and x.isdigit() for x in prefixes), (
            f"migration ids must be 4-digit zero-padded, got {prefixes}"
        )
        assert len(set(prefixes)) == len(prefixes), f"duplicate migration number in {prefixes}"

    def test_initial_schema_has_no_rollback(self) -> None:
        # ADR 0020: a rollback that would destroy data is not written,
        # and reversing 0001 drops every entry in the pool.
        initial = _migration_files()[0]
        assert initial.name.startswith("0001."), initial.name
        rollback = MIGRATIONS_DIR / (initial.stem + ".rollback.sql")
        assert not rollback.exists(), (
            "0001.initial-schema must NOT have a rollback: reversing it drops the "
            "whole pool. That direction is `make pg-reset` or a restore from backup."
        )

    @pytest.mark.parametrize("migration", _migration_files()[1:], ids=lambda p: p.name)
    def test_sql_migrations_after_the_first_ship_a_rollback(self, migration: Path) -> None:
        # Structural changes are reversible (ADR 0020). A migration that
        # deliberately has no rollback (a backfill) must say so in a
        # leading comment, so the omission is a decision, not an oversight.
        rollback = MIGRATIONS_DIR / (migration.stem + ".rollback.sql")
        if rollback.exists():
            return
        head = migration.read_text()[:2000].lower()
        assert "no rollback" in head, (
            f"{migration.name} has no {rollback.name} and does not explain why. "
            "Add the rollback, or state 'no rollback' and the reason in a leading "
            "comment (ADR 0020: destructive rollbacks are not written)."
        )


_AUDIT = r'(?:public\s*\.\s*)?"?audit_log"?'
# Any statement that removes audit rows or the table, wherever it sits.
_AUDIT_DESTROYER = re.compile(
    rf"\b(?:DELETE\s+FROM\s+(?:ONLY\s+)?{_AUDIT}|TRUNCATE\b[^;]*?{_AUDIT}"
    rf"|DROP\s+TABLE\b[^;]*?{_AUDIT})[^;]*;",
    re.I | re.S,
)
_STRICT_DELETE = re.compile(rf"DELETE\s+FROM\s+{_AUDIT}\s+WHERE\s+(.+?);", re.I | re.S)
_STRICT_CLAUSE = re.compile(
    r"(action|actor_kind)\s*(?:=\s*('[^']+')|IN\s*\(\s*('[^']+'(?:\s*,\s*'[^']+')*)\s*\))",
    re.I,
)
# The one rollback allowed to destroy the audit log by other means: it
# refuses to run while the log holds a row (ADR 0027).
_AUDIT_DROP_ALLOWLIST = {"0005.audit-log.rollback.sql"}


def _strip_sql_comments(sql: str) -> str:
    return re.sub(r"--[^\n]*", "", sql)


def _audit_deletions_in_sql(name: str, sql: str) -> tuple[set[str], set[str]]:
    """The actions and actor kinds named by a rollback's audit DELETEs.

    Every statement that deletes, truncates or drops `audit_log` must be
    `DELETE FROM audit_log WHERE (action|actor_kind) (= 'x' | IN ('x', ...))`
    joined by OR, or sit in a rollback on the allowlist; anything else (a
    bare DELETE, `<>`, `NOT IN`, AND, a subquery, TRUNCATE) fails, because
    the guard's table could not describe it."""
    actions: set[str] = set()
    kinds: set[str] = set()
    for match in _AUDIT_DESTROYER.finditer(_strip_sql_comments(sql)):
        statement = match.group(0)
        if name in _AUDIT_DROP_ALLOWLIST:
            continue
        strict = _STRICT_DELETE.fullmatch(statement.strip())
        assert strict is not None, f"{name}: unsupported audit_log statement: {statement!r}"
        clauses = re.split(r"\s+OR\s+", strict.group(1), flags=re.I)
        for clause in clauses:
            m = _STRICT_CLAUSE.fullmatch(clause.strip())
            assert m is not None, f"{name}: unsupported audit_log predicate: {clause!r}"
            values = set(re.findall(r"'([^']+)'", m.group(2) or m.group(3)))
            (actions if m.group(1).lower() == "action" else kinds).update(values)
    return actions, kinds


def test_rollback_audit_table_matches_the_shipped_rollback_files() -> None:
    """ADR 0119: `ROLLBACK_AUDIT_DELETES` is what the rollback guard counts,
    so it must name exactly what each frozen `.rollback.sql` deletes, and
    every rollback that deletes audit rows must be in it."""
    from hivemind.store.migrate import ROLLBACK_AUDIT_DELETES

    found: dict[str, tuple[set[str], set[str]]] = {}
    for path in MIGRATIONS_DIR.glob("*.rollback.sql"):
        deleted = _audit_deletions_in_sql(path.name, path.read_text())
        if deleted[0] or deleted[1]:
            found[path.name.removesuffix(".rollback.sql")] = deleted
    assert found, "the parser found no audit DELETE at all"
    assert set(found) == set(ROLLBACK_AUDIT_DELETES)
    for migration_id, (actions, kinds) in found.items():
        entry = ROLLBACK_AUDIT_DELETES[migration_id]
        assert set(entry.actions) == actions, migration_id
        assert set(entry.actor_kinds) == kinds, migration_id


def test_every_audit_action_is_in_the_latest_check_constraint() -> None:
    """The widest `audit_log_action_check` (the newest migration that
    sets it) lists exactly the `AuditAction` vocabulary."""
    from hivemind.domain.audit import AuditAction

    newest = max(
        p
        for p in MIGRATIONS_DIR.glob("*.sql")
        if not p.name.endswith(".rollback.sql") and "audit_log_action_check" in p.read_text()
    )
    listed = set(
        re.findall(r"'([a-z_]+\.[a-z_]+)'", newest.read_text().split("CHECK (action", 1)[1])
    )
    assert listed == {a.value for a in AuditAction}


@pytest.mark.parametrize(
    "sql",
    [
        "DELETE FROM audit_log;",
        "DELETE FROM public.audit_log WHERE action <> 'a';",
        "DELETE FROM audit_log WHERE action NOT IN ('a');",
        "DELETE FROM audit_log WHERE action = 'a' AND actor = 'b';",
        "DELETE FROM audit_log WHERE action IN (SELECT 'a');",
        "TRUNCATE audit_log;",
        "DROP TABLE public.audit_log;",
    ],
)
def test_the_audit_drift_check_rejects_forms_the_table_cannot_describe(sql: str) -> None:
    with pytest.raises(AssertionError):
        _audit_deletions_in_sql("9999.x.rollback.sql", sql)


def test_the_audit_drift_check_ignores_comments_and_reads_the_strict_form() -> None:
    sql = "-- DELETE FROM audit_log;\nDELETE FROM public.audit_log WHERE action IN ('a', 'b') OR actor_kind = 'k';"
    assert _audit_deletions_in_sql("9999.x.rollback.sql", sql) == ({"a", "b"}, {"k"})
