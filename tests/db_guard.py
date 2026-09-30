"""Refuse to run the Postgres-backed tests against a database that looks real.

The integration suite (and ``scripts/check-backward-compat.sh``) TRUNCATEs
``entries`` / ``credentials`` and DROPs the schema in whatever database
``HIVEMIND_DATABASE_URL`` names -- the same variable ``make api`` /
``make migrate`` use. Exporting a production DSN and running ``make test``
would wipe production, so the suite only proceeds when the database name
looks like a dev / test one. The dev default (``hivemind``) and its
per-worktree variants (``hivemind_<x>``) qualify; set
``HM_TEST_ALLOW_ANY_DB=1`` to override deliberately.
"""

from __future__ import annotations

import re
from urllib.parse import parse_qs, urlsplit

_SAFE_TOKEN = re.compile(r"(^|[_-])(test|tests|dev|ci|scratch|tmp)([_-]|$)")


# Hosts a disposable database can live on: this machine, or the `postgres`
# service name docker-compose / GitLab CI give the DB container. A DB named
# `hivemind` on any OTHER host is indistinguishable from the documented
# production DSN (DEPLOY.md), so it needs the explicit override.
_LOCAL_HOSTS = {"", "localhost", "127.0.0.1", "::1", "postgres"}


class UnsafeTestDatabase(RuntimeError):
    """The configured database does not look like a disposable test database."""


def database_name(dsn: str) -> str:
    return urlsplit(dsn).path.lstrip("/").split("?")[0]


def database_host(dsn: str) -> str:
    parts = urlsplit(dsn)
    host = (parts.hostname or "").lower()
    if not host:  # a unix socket is given as ?host=/var/run/postgresql
        q = parse_qs(parts.query).get("host", [""])[0]
        return "" if q.startswith("/") else q.lower()
    return host


def check_test_database(dsn: str, *, allow_any: bool) -> None:
    if allow_any:
        return
    name = database_name(dsn).lower()
    host = database_host(dsn)
    if host not in _LOCAL_HOSTS:
        raise UnsafeTestDatabase(
            f"refusing to run the integration tests against host {host!r}: they TRUNCATE "
            "entries/credentials and DROP the schema, and only a local database (localhost, "
            "a unix socket, or the `postgres` service) is assumed disposable. Set "
            "HM_TEST_ALLOW_ANY_DB=1 if you really mean it."
        )
    safe = (name == "hivemind" or name.startswith("hivemind_") or _SAFE_TOKEN.search(name)) and (
        "prod" not in name
    )
    if not safe:
        raise UnsafeTestDatabase(
            f"refusing to run the integration tests against database {name!r}: they TRUNCATE "
            "entries/credentials and DROP the schema. Point HIVEMIND_DATABASE_URL at a "
            "disposable database (e.g. 'hivemind', 'hivemind_test'), or set "
            "HM_TEST_ALLOW_ANY_DB=1 if you really mean it."
        )
