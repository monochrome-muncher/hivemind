"""Issue, rotate, and revoke API keys (ADRs 0008, 0012); console entry point ``hivemind-keys``.

Raw keys are printed **once**; only their SHA-256 hash is stored (SPEC.md §8.1). Every mutating
command audits a ``cli`` row in the same transaction (ADR 0027); its ``--actor`` is **unverified**.
Keys appear only by fingerprint.

Usage:
    hivemind-keys [--actor NAME] issue-admin
    hivemind-keys issue-agent --name alice [--trust-level N --home-fleet ID]
    hivemind-keys rotate-agent --name alice   (replace an active agent's key, ADR 0108)
    hivemind-keys rotate-org
    hivemind-keys bootstrap-secret [--secret hivemind-keys]   (in-cluster, ADR 0044)
    hivemind-keys revoke --name alice
    hivemind-keys revoke-admin --key hm_xxx   (or --hash <sha256 from list>)
    hivemind-keys list
"""

from __future__ import annotations

import argparse
import asyncio
import getpass
import json
import os
import secrets
import sys
import uuid
from collections.abc import Callable, Mapping
from typing import NoReturn, Protocol

import asyncpg

from hivemind.config import load_settings
from hivemind.domain.access import AgentStatus, TrustLevel
from hivemind.domain.audit import ActorKind, AuditAction, key_fingerprint
from hivemind.domain.succession import AgentRetired
from hivemind.domain.validation import is_valid_uuid
from hivemind.store.auth import DEAD_CREDENTIAL_PREDICATE, ORG_KEY_LOCK, key_hash

_ISSUE_ADMIN = "INSERT INTO credentials (key_hash, kind, user_id) VALUES ($1, 'admin', $2)"
_ISSUE_AGENT = (
    "INSERT INTO credentials (key_hash, kind, user_id, agent_id, agent_name) "
    "VALUES ($1, 'agent', $2, $2, $2)"
)
_ISSUE_ORG = "INSERT INTO credentials (key_hash, kind, user_id) VALUES ($1, 'org', $2)"
_DELETE_ORG = "DELETE FROM credentials WHERE kind = 'org'"
_LIST = (
    "SELECT c.key_hash, c.kind, c.user_id, c.agent_name, (c.locked_at IS NOT NULL) AS locked, "
    f"{DEAD_CREDENTIAL_PREDICATE} AS dead FROM credentials c ORDER BY c.created_at"
)
# Share-lock the row so a merge cannot commit between this check and the activation (ADR 0107).
_FLEET_FOR_SHARE = "SELECT merged_into FROM fleets WHERE id = $1 FOR SHARE"
_REVOKE_AGENT = "DELETE FROM credentials WHERE kind = 'agent' AND agent_name = $1"
_REVOKE_ADMIN = "DELETE FROM credentials WHERE kind = 'admin' AND key_hash = $1"
_ADMIN_HASHES_BY_PREFIX = (
    "SELECT key_hash FROM credentials WHERE kind = 'admin' AND starts_with(key_hash, $1)"
)
_MIN_HASH_PREFIX = 12  # the fingerprint `hivemind-keys list` prints
_INSERT_AUDIT = (
    "INSERT INTO audit_log (actor_kind, actor, action, target, detail) "
    "VALUES ($1, $2, $3, $4, $5::jsonb)"
)
_HAS_AGENT_KEY = "SELECT 1 FROM credentials WHERE kind = 'agent' AND agent_name = $1"
_AGENT_STATUS_FOR_UPDATE = "SELECT status FROM agents WHERE name = $1 FOR UPDATE"
_ACTIVATE = (
    "UPDATE agents SET status = 'active', trust_level = $2, home_fleet_id = $3, "
    "activated_at = now() WHERE name = $1"
)
_SUCCESSOR_OF = "SELECT successor FROM successions WHERE retired_agent = $1"
_DELETE_AGENT_KEYS = "DELETE FROM credentials WHERE kind = 'agent' AND agent_name = $1"
_DELETE_AGENT_KEYS_RETURNING = _DELETE_AGENT_KEYS + " RETURNING key_hash"
_SET_REVOKED = "UPDATE agents SET status = 'revoked' WHERE name = $1"


class AgentKeyExists(Exception):
    """``issue-agent`` refused: one key per agent (ADR 0028); ``rotate-agent`` replaces it."""


class AgentNeedsActivation(Exception):
    """``issue-agent`` refused: a ``pending`` or ``revoked`` agent needs an explicit trust level
    and home fleet (ADR 0039), never a silent restore of stale values."""

    def __init__(self, name: str, status: str) -> None:
        super().__init__(f"agent {name!r} is {status}: pass --trust-level and --home-fleet")
        self.name = name
        self.status = status


class ActivationFlagsNotApplicable(Exception):
    """``issue-agent`` refused: activation flags for an ``active`` agent (the admin PATCH changes
    those, ADR 0039)."""


class UnknownFleet(Exception):
    """``issue-agent`` refused: ``--home-fleet`` is not a known fleet id."""


class FleetMergedError(Exception):
    """``issue-agent`` refused: ``--home-fleet`` was merged away (ADR 0107)."""

    def __init__(self, fleet_id: str, target_id: str) -> None:
        super().__init__(f"fleet {fleet_id} was merged into {target_id}")
        self.fleet_id = fleet_id
        self.target_id = target_id


class AgentNotActive(Exception):
    """``rotate-agent`` refused: only an ``active`` agent's key is replaced (ADR 0108)."""

    def __init__(self, name: str, status: str) -> None:
        super().__init__(f"agent {name!r} is {status}, not active")
        self.name = name
        self.status = status


class AgentNotRegistered(Exception):
    """``issue-agent`` or ``revoke`` refused: keys bind to registered names (ADR 0012)."""


class AgentAlreadyRevoked(Exception):
    """``revoke`` found the agent already ``revoked``: nothing to do, no audit row."""


def _raw_key() -> str:
    """A fresh random 32-byte API key (the raw secret, printed once)."""
    return "hm_" + secrets.token_hex(32)


def default_actor() -> str:
    """The ``--actor`` default: the OS user, unverified; the numeric uid when there is no passwd
    entry, since this break-glass tool must still work in such containers."""
    try:
        return getpass.getuser()
    except OSError, KeyError:
        return f"uid:{os.getuid()}"


async def _audit(
    conn: asyncpg.Connection,
    actor: str,
    action: AuditAction,
    target: str | None,
    detail: Mapping[str, str | None] | None = None,
) -> None:
    """Append a ``cli`` audit row in the mutation's transaction (ADR 0027). Takes no key."""
    await conn.execute(
        _INSERT_AUDIT,
        ActorKind.CLI.value,
        actor,
        action.value,
        target,
        json.dumps(detail or {}),
    )


async def _issue_admin(dsn: str, *, actor: str | None = None) -> str:
    """Issue an admin key; return the raw secret (printed once). Audited
    as ``admin_key.issue`` with the new key's fingerprint as target."""
    raw_key = _raw_key()
    conn = await asyncpg.connect(dsn)
    try:
        async with conn.transaction():
            await _insert_admin(conn, raw_key, actor or default_actor())
    finally:
        await conn.close()
    return raw_key


async def _insert_admin(conn: asyncpg.Connection, raw_key: str, actor: str) -> None:
    """Store ``raw_key`` as an admin key and audit it, on ``conn``'s open
    transaction."""
    stored_hash = key_hash(raw_key)
    await conn.execute(_ISSUE_ADMIN, stored_hash, "admin")
    await _audit(conn, actor, AuditAction.ADMIN_KEY_ISSUE, key_fingerprint(stored_hash))


async def _issue_agent(
    dsn: str,
    name: str,
    *,
    actor: str | None = None,
    trust_level: TrustLevel | None = None,
    home_fleet_id: str | None = None,
) -> str:
    """Issue an agent key bound to the registered name (ADR 0012), audited as ``agent_key.issue``.

    As REST activate (ADR 0039): an active agent without a key gets one, with a key is refused
    (ADR 0028); a pending or revoked agent needs ``trust_level`` and ``home_fleet_id`` and is
    activated with them in the same transaction."""
    raw_key = _raw_key()
    conn = await asyncpg.connect(dsn)
    try:
        async with conn.transaction():
            # Fleet row before agent row, as PgStore.activate_agent and the merge lock them: the
            # opposite order could deadlock with a merge.
            fleet = (
                await conn.fetchrow(_FLEET_FOR_SHARE, home_fleet_id)
                if home_fleet_id is not None and is_valid_uuid(home_fleet_id)
                else None
            )
            status = await conn.fetchval(_AGENT_STATUS_FOR_UPDATE, name)
            if status is None:
                raise AgentNotRegistered(name)
            if (successor := await conn.fetchval(_SUCCESSOR_OF, name)) is not None:
                raise AgentRetired(name, successor)  # ADR 0113: undo the retirement first
            detail: dict[str, str] | None = None
            if status == AgentStatus.ACTIVE:
                if trust_level is not None or home_fleet_id is not None:
                    raise ActivationFlagsNotApplicable(name)
                if await conn.fetchval(_HAS_AGENT_KEY, name) is not None:
                    raise AgentKeyExists(name)
            else:
                if trust_level is None or home_fleet_id is None:
                    raise AgentNeedsActivation(name, status)
                if fleet is None:
                    raise UnknownFleet(home_fleet_id)
                if fleet["merged_into"] is not None:
                    raise FleetMergedError(home_fleet_id, str(fleet["merged_into"]))
                await conn.execute(_DELETE_AGENT_KEYS, name)  # stale rows, if any
                await conn.execute(_ACTIVATE, name, trust_level.value, home_fleet_id)
                detail = {
                    "from": status,
                    "trust_level": str(trust_level.value),
                    "home_fleet_id": home_fleet_id,
                }
            await conn.execute(_ISSUE_AGENT, key_hash(raw_key), name)
            await _audit(conn, actor or default_actor(), AuditAction.AGENT_KEY_ISSUE, name, detail)
    finally:
        await conn.close()
    return raw_key


async def _rotate_agent(dsn: str, name: str, *, actor: str | None = None) -> str:
    """Replace an active agent's key (ADR 0108) in one transaction, so the old key dies at once;
    audited as ``agent_key.replace`` with both fingerprints."""
    raw_key = _raw_key()
    new_hash = key_hash(raw_key)
    conn = await asyncpg.connect(dsn)
    try:
        async with conn.transaction():
            status = await conn.fetchval(_AGENT_STATUS_FOR_UPDATE, name)
            if status is None:
                raise AgentNotRegistered(name)
            if status != AgentStatus.ACTIVE:
                raise AgentNotActive(name, status)
            old = await conn.fetch(_DELETE_AGENT_KEYS_RETURNING, name)
            await conn.execute(_ISSUE_AGENT, new_hash, name)
            await _audit(
                conn,
                actor or default_actor(),
                AuditAction.AGENT_KEY_REPLACE,
                name,
                {
                    "old_key": key_fingerprint(old[0]["key_hash"]) if old else None,
                    "new_key": key_fingerprint(new_hash),
                },
            )
    finally:
        await conn.close()
    return raw_key


async def _rotate_org(dsn: str, *, actor: str | None = None) -> str:
    """Rotate the shared org key (ADR 0031), audited as ``org_key.rotate``."""
    raw_key = _raw_key()
    conn = await asyncpg.connect(dsn)
    try:
        async with conn.transaction():
            await _replace_org(conn, raw_key, actor or default_actor())
    finally:
        await conn.close()
    return raw_key


async def _replace_org(conn: asyncpg.Connection, raw_key: str, actor: str) -> None:
    """Make ``raw_key`` the only org key and audit it, on ``conn``'s open
    transaction."""
    await conn.execute(ORG_KEY_LOCK)
    await conn.execute(_DELETE_ORG)
    await conn.execute(_ISSUE_ORG, key_hash(raw_key), "org")
    await _audit(conn, actor, AuditAction.ORG_KEY_ROTATE, None)


class SecretWriter(Protocol):
    """The slice of the Kubernetes API the bootstrap needs (ADR 0044)."""

    @property
    def namespace(self) -> str: ...

    def exists(self, name: str) -> bool: ...

    def create(self, name: str, string_data: dict[str, str], labels: dict[str, str]) -> None: ...

    def delete(self, name: str) -> None: ...


class SecretLeftBehind(RuntimeError):
    """The bootstrap commit failed and the Secret it created could not be
    deleted. Its keys most likely never reached the database, but a commit
    whose reply was lost may have reached it, so the message says how to
    check before the Secret is deleted."""

    def __init__(self, name: str, namespace: str, cause: Exception) -> None:
        super().__init__(
            f"the database commit failed after Secret {name!r} was created in namespace "
            f"{namespace!r}, and deleting it failed too ({cause}). Its keys most likely "
            f"never reached the database; `hivemind-keys list` shows whether an admin key "
            f"was issued by this run (revoke it with `hivemind-keys revoke-admin --hash`). "
            f"Then delete the Secret by hand (kubectl delete secret {name} -n {namespace}) "
            f"and run bootstrap-secret again."
        )


async def _bootstrap_secret(
    dsn: str, secrets_api: SecretWriter, name: str, *, actor: str | None = None
) -> bool:
    """First-run key bootstrap (ADR 0044): issue an admin key, rotate the org key and store both in
    the Kubernetes Secret ``name``; ``False`` if it exists. Keys are never printed.

    The Secret is created inside the transaction, so a failed create rolls the keys back; a failed
    commit deletes it, and if that fails too ``SecretLeftBehind`` is raised. (A pod killed between
    create and commit can still leave it.)"""
    if await asyncio.to_thread(secrets_api.exists, name):
        return False
    who = actor or default_actor()
    admin_key, org_key = _raw_key(), _raw_key()
    conn = await asyncpg.connect(dsn)
    try:
        tx = conn.transaction()
        await tx.start()
        try:
            await _insert_admin(conn, admin_key, who)
            await _replace_org(conn, org_key, who)
            await asyncio.to_thread(
                secrets_api.create,
                name,
                {"ADMIN_KEY": admin_key, "ORG_KEY": org_key},
                {"app.kubernetes.io/name": "hivemind", "app.kubernetes.io/component": "keys"},
            )
        except BaseException:
            await tx.rollback()
            raise
        try:
            await tx.commit()
        except BaseException as commit_error:
            try:
                await asyncio.to_thread(secrets_api.delete, name)
            except Exception as delete_error:
                # Left in place the Secret would make every later run issue nothing, so the
                # operator must remove it by hand.
                raise SecretLeftBehind(name, secrets_api.namespace, delete_error) from commit_error
            raise
    finally:
        await conn.close()
    return True


async def _list(dsn: str) -> None:
    """List credential hashes (never raw keys)."""
    conn = await asyncpg.connect(dsn)
    try:
        rows = await conn.fetch(_LIST)
    finally:
        await conn.close()
    if not rows:
        print("(no credentials)")
        return
    for row in rows:
        name = row["agent_name"] or row["user_id"] or "-"
        mark = "  [dead: never authenticates, ADR 0039]" if row["dead"] else ""
        if row["locked"]:
            mark += "  [locked from the portal: replace it with rotate-agent, ADR 0117]"
        print(f"{key_fingerprint(row['key_hash'])}…  {row['kind']:<6} {name}{mark}")


async def _revoke(dsn: str, name: str, *, actor: str | None = None) -> None:
    """Retire an agent's key and set it ``revoked``; the name stays
    reserved (ADRs 0012, 0028)."""
    conn = await asyncpg.connect(dsn)
    try:
        async with conn.transaction():
            prior = await conn.fetchval(_AGENT_STATUS_FOR_UPDATE, name)
            if prior is None:
                raise AgentNotRegistered(name)
            if prior == AgentStatus.REVOKED:
                raise AgentAlreadyRevoked(name)
            await conn.execute(_REVOKE_AGENT, name)
            await conn.execute(_SET_REVOKED, name)
            detail = {"from": prior}
            await _audit(conn, actor or default_actor(), AuditAction.AGENT_REVOKE, name, detail)
    finally:
        await conn.close()


async def _revoke_admin(
    dsn: str,
    *,
    raw_key: str | None = None,
    stored_hash: str | None = None,
    actor: str | None = None,
) -> bool:
    """Revoke one admin key (the second half of rotation), by raw secret, hash or fingerprint
    prefix. Returns whether a row was deleted (the CLI exits non-zero on a no-op, DEPLOY.md §5);
    only a deletion is audited."""
    if stored_hash is not None:
        target = stored_hash.strip().lower()
        if len(target) < _MIN_HASH_PREFIX or any(c not in "0123456789abcdef" for c in target):
            raise ValueError(
                f"--hash must be hex, at least {_MIN_HASH_PREFIX} characters "
                "(the fingerprint from `hivemind-keys list`, or the full SHA-256)"
            )
    elif raw_key is not None:
        target = key_hash(raw_key)  # the guard above proves it is set
    else:
        raise ValueError("either raw_key or stored_hash must be provided")
    conn = await asyncpg.connect(dsn)
    try:
        async with conn.transaction():
            if stored_hash is not None and len(target) < 64:
                # A fingerprint prefix: it must identify exactly one admin key.
                matches = [r["key_hash"] for r in await conn.fetch(_ADMIN_HASHES_BY_PREFIX, target)]
                if len(matches) > 1:
                    raise ValueError(f"hash prefix {target!r} matches {len(matches)} admin keys")
                if not matches:
                    return False
                target = matches[0]
            status = await conn.execute(_REVOKE_ADMIN, target)
            revoked = str(status) == "DELETE 1"
            if revoked:
                await _audit(
                    conn,
                    actor or default_actor(),
                    AuditAction.ADMIN_KEY_REVOKE,
                    key_fingerprint(target),
                )
    finally:
        await conn.close()
    return revoked


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Hivemind key management (ADR 0012)")
    parser.add_argument(
        "--actor",
        default=None,
        help=(
            "who to record in the audit log for a mutating command (default: the OS "
            "user). UNVERIFIED: it is whatever you type, so audit rows from this CLI "
            "are marked actor_kind=cli, never admin_key (ADR 0027)"
        ),
    )
    sub = parser.add_subparsers(dest="cmd", required=True)

    sub.add_parser("issue-admin", help="issue a new admin key")

    issue_agent = sub.add_parser("issue-agent", help="issue an agent key for a registered agent")
    issue_agent.add_argument("--name", required=True)
    issue_agent.add_argument(
        "--trust-level",
        type=int,
        choices=[level.value for level in TrustLevel],
        help="required for a pending/revoked agent: activates it at this level (ADR 0039)",
    )
    issue_agent.add_argument(
        "--home-fleet",
        help="required for a pending/revoked agent: the home fleet id to activate it into",
    )

    rotate_agent = sub.add_parser(
        "rotate-agent",
        help="replace an active agent's key; the old key stops working at once (ADR 0108)",
    )
    rotate_agent.add_argument("--name", required=True)

    sub.add_parser(
        "rotate-org",
        help="rotate the shared org key (closes registration; active agents unaffected)",
    )

    bootstrap = sub.add_parser(
        "bootstrap-secret",
        help=(
            "first-run bootstrap inside the cluster: issue an admin key + rotate the org key "
            "straight into a Kubernetes Secret, printing neither (no-op if it exists; ADR 0044)"
        ),
    )
    bootstrap.add_argument("--secret", default="hivemind-keys", help="the Secret to create")

    sub.add_parser("list", help="list credential hashes (never raw keys)")

    revoke_admin = sub.add_parser(
        "revoke-admin",
        help="revoke a specific admin key (rotation: issue a new one, then revoke the old)",
    )
    revoke_admin.add_argument(
        "--key", help="the raw admin secret to revoke (hashed before matching)"
    )
    revoke_admin.add_argument(
        "--hash",
        dest="stored_hash",
        help="the stored SHA-256 hex, or its unique fingerprint prefix (>= 12 hex) from `hivemind-keys list`",
    )

    revoke = sub.add_parser("revoke", help="revoke an agent's key (name stays reserved)")
    revoke.add_argument("--name", required=True)
    return parser


def _validate_args(parser: argparse.ArgumentParser, args: argparse.Namespace) -> None:
    """The argument checks argparse cannot express; each exits through ``parser.error``."""
    if args.cmd == "issue-agent" and args.home_fleet is not None:
        try:
            uuid.UUID(args.home_fleet)
        except ValueError:
            parser.error(f"--home-fleet {args.home_fleet!r} is not a fleet id (a UUID)")
    if args.cmd == "revoke-admin" and (args.key is None) == (args.stored_hash is None):
        parser.error("exactly one of --key / --hash is required")


def _print_key(kind: str, key: str) -> None:
    # The bare key on stdout, so `KEY=$(hivemind-keys ...)` still works;
    # what it is goes to stderr, for the person reading the terminal.
    print(f"{kind} (shown once; only its hash is stored, so save it now):", file=sys.stderr)
    sys.stderr.flush()
    print(key)


def _fail(message: str) -> NoReturn:
    print(message, file=sys.stderr)
    raise SystemExit(1) from None


def _run_issue_admin(args: argparse.Namespace, dsn: str, actor: str) -> None:
    _print_key("New admin key", asyncio.run(_issue_admin(dsn, actor=actor)))


def _run_issue_agent(args: argparse.Namespace, dsn: str, actor: str) -> None:
    try:
        _print_key(
            f"New agent key for {args.name}",
            asyncio.run(
                _issue_agent(
                    dsn,
                    args.name,
                    actor=actor,
                    trust_level=(
                        TrustLevel(args.trust_level) if args.trust_level is not None else None
                    ),
                    home_fleet_id=args.home_fleet,
                )
            ),
        )
    except AgentNeedsActivation as exc:
        _fail(
            f"{exc}; it is activated with exactly the level and fleet you give "
            "(a revoked agent's old ones are not restored, ADR 0039)"
        )
    except ActivationFlagsNotApplicable:
        _fail(
            f"agent {args.name} is already active: --trust-level / --home-fleet apply only "
            "when activating a pending or revoked agent (change an active agent with "
            "PATCH /v1/admin/agents/{name})"
        )
    except UnknownFleet as exc:
        _fail(f"no fleet with id {exc}; list them with GET /v1/admin/fleets")
    except AgentRetired as exc:
        _fail(f"{exc} (POST /v1/admin/successions/{exc.name}/undo)")
    except FleetMergedError as exc:
        _fail(
            f"fleet {exc.fleet_id} was merged into fleet {exc.target_id}; "
            "use the target as --home-fleet"
        )
    except AgentKeyExists:
        _fail(
            f"agent {args.name} already holds a key; replace it with "
            "`hivemind-keys rotate-agent --name ...` (it also replaces a key locked "
            "from the portal)"
        )
    except AgentNotRegistered:
        _fail(
            f"no agent registered as {args.name!r}; register it first "
            "(POST /v1/agents, then activate it) — keys are bound to "
            "registered names (ADR 0012)"
        )


def _run_rotate_agent(args: argparse.Namespace, dsn: str, actor: str) -> None:
    try:
        _print_key(
            f"New agent key for {args.name} (the previous one stops working)",
            asyncio.run(_rotate_agent(dsn, args.name, actor=actor)),
        )
    except AgentNotRegistered:
        _fail(f"no agent registered as {args.name!r}")
    except AgentNotActive as exc:
        _fail(
            f"agent {args.name} is {exc.status}: only an active agent's key is replaced. "
            "Use `issue-agent --trust-level ... --home-fleet ...` to activate it again"
        )


def _run_rotate_org(args: argparse.Namespace, dsn: str, actor: str) -> None:
    _print_key(
        "New org key (the previous one stops working)",
        asyncio.run(_rotate_org(dsn, actor=actor)),
    )


def _run_bootstrap_secret(args: argparse.Namespace, dsn: str, actor: str) -> None:
    from hivemind.store.kube_secret import KubeApiError, KubeSecrets

    try:
        created = asyncio.run(
            _bootstrap_secret(dsn, KubeSecrets.in_cluster(), args.secret, actor=actor)
        )
    except KubeApiError as exc:
        _fail(f"{exc}; no key was issued")
    except SecretLeftBehind as exc:
        _fail(str(exc))
    if created:
        print("issued the admin and org keys into the configured Secret (not printed)")
    else:
        print("target Secret already exists; nothing issued")


def _run_list(args: argparse.Namespace, dsn: str, actor: str) -> None:
    asyncio.run(_list(dsn))


def _run_revoke_admin(args: argparse.Namespace, dsn: str, actor: str) -> None:
    try:
        revoked = asyncio.run(
            _revoke_admin(dsn, raw_key=args.key, stored_hash=args.stored_hash, actor=actor)
        )
    except ValueError as exc:
        _fail(str(exc))
    if revoked:
        print("revoked the admin key (issue a replacement via `hivemind-keys issue-admin`)")
    else:
        _fail("no admin key matches that identifier (check `hivemind-keys list`)")


def _run_revoke(args: argparse.Namespace, dsn: str, actor: str) -> None:
    try:
        asyncio.run(_revoke(dsn, args.name, actor=actor))
    except AgentNotRegistered:
        _fail(f"no agent registered as {args.name!r}; nothing revoked")
    except AgentAlreadyRevoked:
        print(f"agent {args.name} is already revoked; nothing to do")
        return
    print(f"revoked the key for agent {args.name} (name stays reserved)")


_COMMANDS: dict[str, Callable[[argparse.Namespace, str, str], None]] = {
    "issue-admin": _run_issue_admin,
    "issue-agent": _run_issue_agent,
    "rotate-agent": _run_rotate_agent,
    "rotate-org": _run_rotate_org,
    "bootstrap-secret": _run_bootstrap_secret,
    "list": _run_list,
    "revoke-admin": _run_revoke_admin,
    "revoke": _run_revoke,
}


def main() -> None:
    """Console entry point (``hivemind-keys``)."""
    parser = _build_parser()
    args = parser.parse_args()
    actor: str = args.actor if args.actor is not None else default_actor()
    dsn = load_settings().database_url
    _validate_args(parser, args)
    _COMMANDS[args.cmd](args, dsn, actor)


if __name__ == "__main__":
    main()
