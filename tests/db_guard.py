"""Refuse to run the Postgres-backed tests against a database that looks real.

The integration suite (and ``scripts/check-backward-compat.sh``) TRUNCATEs
``entries`` / ``credentials`` and DROPs the schema in whatever database
``HIVEMIND_DATABASE_URL`` names -- the same variable ``make api`` /
``make migrate`` use. Exporting a production DSN and running ``make test``
would wipe production, so the suite only proceeds when the database name
looks like a dev / test one. The dev default (``hivemind``) and its
per-worktree variants (``hivemind_<x>``) qualify; set
``HM_TEST_ALLOW_ANY_DB=1`` to override deliberately.

Under pytest-xdist every worker gets its own database, ``<name>_gw0``,
``<name>_gw1``, ... (``worker_dsn``), so the workers do not wipe each
other's rows. Those are exactly as destructive as the base database and
pass the same check.
"""

from __future__ import annotations

import re
from urllib.parse import parse_qs, quote, unquote, urlsplit

_SAFE_TOKEN = re.compile(r"(^|[_-])(test|tests|dev|ci|scratch|tmp)([_-]|$)")


# Hosts a disposable database can live on: this machine, or the `postgres`
# service name docker-compose / GitLab CI give the DB container. A DB named
# `hivemind` on any OTHER host is indistinguishable from the documented
# production DSN (DEPLOY.md), so it needs the explicit override.
_LOCAL_HOSTS = {"", "localhost", "127.0.0.1", "::1", "postgres"}


class UnsafeTestDatabase(RuntimeError):
    """The configured database does not look like a disposable test database."""


# libpq takes these from the query string over the URL's path and host, so a
# DSN carrying one could name a database the checks below never see.
_OVERRIDING_PARAMS = ("dbname", "service", "hostaddr")


def database_name(dsn: str) -> str:
    return unquote(urlsplit(dsn).path.lstrip("/").split("?")[0])


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
    overriding = sorted(set(parse_qs(urlsplit(dsn).query)) & set(_OVERRIDING_PARAMS))
    if overriding:
        raise UnsafeTestDatabase(
            f"refusing to run the integration tests with {', '.join(overriding)} in the "
            "HIVEMIND_DATABASE_URL query: libpq would connect to what it names, not to the "
            "checked database. Put the database in the URL path, or set "
            "HM_TEST_ALLOW_ANY_DB=1 if you really mean it."
        )
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
        "prod" not in name and name != "hivemind_live"
    )
    # `hivemind_live` is the standalone local pool (docker-compose.standalone.yaml):
    # it is local, not disposable — it must survive `make test`.
    if not safe:
        raise UnsafeTestDatabase(
            f"refusing to run the integration tests against database {name!r}: they TRUNCATE "
            "entries/credentials and DROP the schema. Point HIVEMIND_DATABASE_URL at a "
            "disposable database (e.g. 'hivemind', 'hivemind_test'), or set "
            "HM_TEST_ALLOW_ANY_DB=1 if you really mean it."
        )


# Postgres silently truncates an identifier to 63 bytes, which would fold two
# workers' databases (or a worker's and the base) into one.
_MAX_DATABASE_NAME = 63


def worker_dsn(dsn: str, worker: str) -> str:
    """``dsn`` with its database name suffixed ``_<worker>`` (xdist's ``gw0``)."""
    parts = urlsplit(dsn)
    base = database_name(dsn)
    if not base:
        raise UnsafeTestDatabase(
            "HIVEMIND_DATABASE_URL names no database to derive a worker's from."
        )
    name = f"{base}_{worker}"
    if len(name.encode()) > _MAX_DATABASE_NAME:
        raise UnsafeTestDatabase(
            f"the worker database name {name!r} is longer than Postgres's {_MAX_DATABASE_NAME} "
            "bytes; use a shorter database name in HIVEMIND_DATABASE_URL."
        )
    # Not urlunsplit: it drops the empty `//` of a socket DSN (`postgresql:///db`).
    query = f"?{parts.query}" if parts.query else ""
    return f"{parts.scheme}://{parts.netloc}/{quote(name, safe='')}{query}"
