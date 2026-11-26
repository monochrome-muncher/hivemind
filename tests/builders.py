"""Shared test builders: the apps, credentials and worlds that several test modules start from.

``tests/fakes.py`` holds the fakes at the port seams; this module composes them (with the real
services) into the surfaces the tests drive. A test module never imports another test module.
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import replace
from datetime import UTC, datetime, timedelta

import httpx
import pytest
from prometheus_client.parser import text_string_to_metric_families

from hivemind.api.deps import HivemindApp, create_app
from hivemind.api.main import create_app_for_config
from hivemind.config import SearchConfig, Settings
from hivemind.domain.access import AgentStatus, TrustLevel
from hivemind.domain.audit import ActorKind, AuditAction, AuditFilters
from hivemind.domain.entry import EntryDraft, EntryState, Kind
from hivemind.domain.feedback import Correction, Verdict
from hivemind.domain.upstream import UpstreamChange, UpstreamChangeKind
from hivemind.mcp.app import McpHivemind
from hivemind.memstore import MemoryStore
from hivemind.ports import Authenticator, Credential, Embedder, Extractor, KeyReplacement, Store
from hivemind.services.access import AccessService
from hivemind.services.search import Hit, SearchService, SimilarEntry
from hivemind.services.wiring import build_services
from tests.fakes import (
    FakeAuthenticator,
    agent_credential,
    make_clock,
    make_embedder,
    make_search_config,
)

# --- the MCP surface ------------------------------------------------------------------------


def mcp_app(
    store: Store,
    credential: Credential,
    clock: Callable[[], datetime] | None = None,
    *,
    embedder: Embedder | None = None,
    config: SearchConfig | None = None,
    extractor: Extractor | None = None,
    access: AccessService | None = None,
    authenticator: Authenticator | None = None,
) -> McpHivemind:
    """The MCP tools over ``store`` as ``credential``, wired as the server wires them.

    The services are the shared ones (``build_services``); ``clock`` is the search service's
    ``now`` (a fixed one by default), and ``access`` replaces the access service when a test shares its own."""
    embedder = embedder or make_embedder()
    config = config or make_search_config()
    services = build_services(
        store, embedder, config, extractor=extractor, authenticator=authenticator
    )
    services = replace(
        services,
        search_service=SearchService(store, embedder, config, now_fn=clock or make_clock()),
        access_service=access or services.access_service,
    )
    return McpHivemind.from_services(services, credential)


DEV_MCP = Credential(user_id="dev", agent_id="hivemind-mcp")


def make_mcp_app(credential: Credential = DEV_MCP, store: MemoryStore | None = None) -> McpHivemind:
    """A ``MemoryStore``-backed ``McpHivemind`` acting as ``credential``."""
    clock = make_clock()
    return mcp_app(store or MemoryStore(clock), credential, clock=clock)


EXPECTED_TOOLS = [
    "hive_write",
    "hive_search",
    "hive_get",
    "hive_list",
    "hive_withdraw",
    "hive_feedback",
    "hive_pin",
    "hive_pinned",
    "hive_register",
    "hive_whoami",
]


ALICE = Credential(user_id="alice", agent_id="agent-1")
ORG = Credential(user_id="org", is_org=True, access_controlled=True)
HTTP_ALICE = Credential(user_id="alice", agent_id="agent-a")
WRITER = agent_credential("writer", "fleet-a", TrustLevel.CONTRIBUTOR)

# --- the REST surface -----------------------------------------------------------------------


class KeyedAuthenticator:
    """Dict-backed Authenticator (SPEC.md §8.1, ADR 0012): user + agent
    sub-keys + key management (the full ``Authenticator`` port)."""

    def __init__(self, keys: dict[str, Credential]) -> None:
        self._keys = keys
        self._org_key: str | None = None

    async def verify(self, key: str) -> Credential | None:
        return self._keys.get(key)

    async def issue_agent_key(self, agent_name: str) -> str:
        raw_key = f"hm_agent_{agent_name}"
        self._keys[raw_key] = Credential(
            user_id=agent_name, agent_name=agent_name, access_controlled=True
        )
        return raw_key

    async def replace_agent_key(
        self, agent_name: str, *, audit_actor_kind: ActorKind, audit_actor: str
    ) -> KeyReplacement:
        old = f"hm_agent_{agent_name}"
        had_old = self._keys.pop(old, None) is not None
        new = f"{old}_2"
        self._keys[new] = Credential(
            user_id=agent_name, agent_name=agent_name, access_controlled=True
        )
        return KeyReplacement(new, "n" * 12, "o" * 12 if had_old else None)

    async def lock_agent_key(self, agent_name: str, *, key_id: str) -> bool:
        return False

    async def revoke_agent_key(self, agent_name: str) -> None:
        raw_key = f"hm_agent_{agent_name}"
        self._keys.pop(raw_key, None)

    async def rotate_org_key(self) -> str:
        if self._org_key is not None:
            self._keys.pop(self._org_key, None)
        self._org_key = "hm_org"
        self._keys[self._org_key] = Credential(user_id="org", is_org=True, access_controlled=True)
        return self._org_key

    def add_key(self, raw_key: str, credential: Credential) -> None:
        """Test helper: register a credential under a raw key directly."""
        self._keys[raw_key] = credential

    async def issue_admin_key(self) -> str:
        raw_key = "hm_admin_new"
        self._keys[raw_key] = Credential(user_id="admin", is_admin=True)
        return raw_key


class StaticAuthenticator:
    """Dict-backed ``Authenticator`` that only verifies (the ``Authenticator`` port)."""

    def __init__(self, keys: dict[str, Credential]) -> None:
        self._keys = keys

    async def verify(self, key: str) -> Credential | None:
        return self._keys.get(key)


def make_rest_authenticator() -> KeyedAuthenticator:
    return KeyedAuthenticator(
        keys={
            "key-alice": Credential(user_id="alice", agent_id="agent-alice"),
            "key-bob": Credential(user_id="bob", agent_id="agent-bob"),
            "key-alice-user": Credential(user_id="alice"),
            "key-admin": Credential(user_id="ops", agent_id="admin-agent", is_admin=True),
            # v2 access-model keys (ADRs 0011-0012):
            "key-org": Credential(user_id="org", is_org=True, access_controlled=True),
        }
    )


def make_hivemind_app() -> HivemindApp:
    """A full HivemindApp wired from fakes: real services, fake ports."""
    return create_app_for_config(
        Settings(),
        store=MemoryStore(make_clock()),
        embedder=make_embedder(),
        authenticator=make_rest_authenticator(),
        search_config=make_search_config(),
    )


def make_app_with_clock(clock) -> HivemindApp:
    """A HivemindApp over a caller-controlled clock (for created_at tests)."""
    return create_app_for_config(
        Settings(),
        store=MemoryStore(clock),
        embedder=make_embedder(),
        authenticator=make_rest_authenticator(),
        search_config=make_search_config(),
    )


async def post_entry(
    client: httpx.AsyncClient,
    key: str,
    summary: str,
    **body: object,
) -> dict:
    payload = {"kind": "fact", "summary": summary, **body}
    resp = await client.post("/v1/entries", json=payload, headers={"X-API-Key": key})
    assert resp.status_code == 201, resp.text
    return resp.json()


def samples(text: str) -> dict[tuple[str, tuple[tuple[str, str], ...]], float]:
    """Every sample of a scrape, keyed by (name, sorted labels)."""
    return {
        (s.name, tuple(sorted(s.labels.items()))): s.value
        for family in text_string_to_metric_families(text)
        for s in family.samples
    }


# --- the portal ----------------------------------------------------------------------------

ADMIN_HEADERS = {"X-API-Key": "key-admin"}
CAROL_HEADERS = {"X-API-Key": "key-carol"}  # a contributor in blue
PAT_HEADERS = {"X-API-Key": "key-pat"}  # privileged in blue: reads every fleet


async def add_entry(app, summary: str, **kw):  # type: ignore[no-untyped-def]
    draft = EntryDraft(
        kind=Kind.FACT, summary=summary, author=kw.pop("author", "carol"), agent="a", **kw
    )
    return await app.store.create_entry(draft, await make_embedder().embed_text(summary))


async def seed_portal():  # type: ignore[no-untyped-def]
    app = make_hivemind_app()
    blue = await app.store.create_fleet("blue")
    red = await app.store.create_fleet("red")
    for key, name, level in (
        ("key-carol", "carol", TrustLevel.CONTRIBUTOR),
        ("key-pat", "pat", TrustLevel.PRIVILEGED),
    ):
        app.authenticator.add_key(  # type: ignore[attr-defined]
            key,
            Credential(
                user_id=name,
                agent_name=name,
                access_controlled=True,
                trust_level=level,
                home_fleet_id=blue.id,
            ),
        )
    mine = await add_entry(app, "carol private note kaniko", scope="self")
    in_blue = await add_entry(app, "blue fleet deploy kaniko", scope="fleet", fleet_id=blue.id)
    by_bob = await add_entry(
        app, "bob blue fleet kaniko", scope="fleet", fleet_id=blue.id, author="bob"
    )
    in_red = await add_entry(
        app, "red fleet deploy kaniko", scope="fleet", fleet_id=red.id, author="bob"
    )
    return app, blue, red, mine, in_blue, by_bob, in_red


# --- retirement and succession -----------------------------------------------------------

ADMIN = Credential(user_id="admin", is_admin=True, key_id="abcdef012345")
T0 = datetime(2026, 10, 1, 9, 0, tzinfo=UTC)
ADMIN_H = {"X-API-Key": "hm_admin"}


class TickingClock:
    def __init__(self) -> None:
        self._now = T0

    def __call__(self) -> datetime:
        self._now += timedelta(seconds=1)
        return self._now


class StoreAuthenticator(FakeAuthenticator):
    """Resolves ``k-<name>`` from the agent's record and its successions on
    every request, and only while the agent is active (ADR 0039)."""

    def __init__(self, store: MemoryStore) -> None:
        super().__init__(store=store)
        self._records = store

    async def verify(self, key: str) -> Credential | None:
        if not key.startswith("k-"):
            return await super().verify(key)
        agent = await self._records.get_agent(key[2:])
        if agent is None or agent.status is not AgentStatus.ACTIVE:
            return None
        return Credential(
            user_id=agent.name,
            agent_id=agent.name,
            agent_name=agent.name,
            access_controlled=True,
            trust_level=agent.trust_level,
            home_fleet_id=agent.home_fleet_id,
            inherits_from=await self._records.inherited_authors(agent.name),
        )


class World:
    def __init__(self) -> None:
        self.store = MemoryStore(TickingClock())
        self.auth = StoreAuthenticator(self.store)
        self.app = create_app_for_config(
            Settings(),
            store=self.store,
            embedder=make_embedder(),
            authenticator=self.auth,
            search_config=make_search_config(),
        )
        self.client = httpx.AsyncClient(
            transport=httpx.ASGITransport(app=create_app(self.app)), base_url="http://t"
        )
        self.successions = self.app.succession_service
        self.blue = ""
        self.red = ""

    async def setup(self) -> World:
        self.blue = (await self.store.create_fleet("blue")).id
        self.red = (await self.store.create_fleet("red")).id
        return self

    async def agent(
        self, name: str, fleet: str, level: TrustLevel = TrustLevel.CONTRIBUTOR
    ) -> None:
        await self.store.register_agent(name)
        await self.store.activate_agent(name, trust_level=level, home_fleet_id=fleet)

    async def entry(self, author: str, summary: str, scope: str = "self", fleet: str | None = None):
        draft = EntryDraft(
            kind=Kind.FACT,
            summary=summary,
            author=author,
            agent=author,
            scope=scope,
            fleet_id=fleet,
        )
        return await self.store.create_entry(draft, await make_embedder().embed_text(summary))

    def h(self, name: str) -> dict[str, str]:
        return {"X-API-Key": f"k-{name}"}

    async def get(self, name: str, entry_id: str) -> int:
        return (await self.client.get(f"/v1/entries/{entry_id}", headers=self.h(name))).status_code

    async def withdraw(self, name: str, entry_id: str, path: str = "/v1/entries/{}/withdraw"):
        return await self.client.post(
            path.format(entry_id), json={"reason": "obsolete"}, headers=self.h(name)
        )

    async def rate(self, name: str, entry_id: str) -> httpx.Response:
        return await self.client.post(
            f"/v1/entries/{entry_id}/feedback", json={"verdict": "helpful"}, headers=self.h(name)
        )

    async def write(self, name: str, summary: str, **body) -> httpx.Response:  # type: ignore[no-untyped-def]
        return await self.client.post(
            "/v1/entries",
            json={"kind": "fact", "summary": summary, **body},
            headers=self.h(name),
        )

    async def audit(self, action: AuditAction) -> list:  # type: ignore[type-arg]
        return await self.store.list_audit(AuditFilters(action=action), 100)


# --- wire shapes -------------------------------------------------------------------------

DT_OCCURRED = datetime(2026, 10, 1, 12, 0, 0, tzinfo=UTC)
DT_CREATED = datetime(2026, 10, 2, 12, 0, 0, tzinfo=UTC)
DT_CONFIRMED = datetime(2026, 10, 3, 12, 0, 0, tzinfo=UTC)
DT_REPORT = datetime(2026, 10, 4, 12, 0, 0, tzinfo=UTC)

HIT = Hit(
    entry_id="22222222-2222-4222-8222-222222222222",
    kind=Kind.INSIGHT,
    summary="hit summary",
    tags=("tag-h",),
    author="author-h",
    agent="agent-h",
    occurred_at=DT_OCCURRED,
    score=0.87,
    match="both",
    similarity=0.81,
    scope="org",
    fleet_id=None,
    feedback=(3, 1, 2),
    last_confirmed_at=DT_CONFIRMED,
    contested=True,
    correction=Correction(
        verdict=Verdict.STALE,
        note="correction note",
        reporter="reporter-c",
        updated_at=DT_REPORT,
    ),
    supersedes=2,
    state=EntryState.ACTIVE,
    similar=(
        SimilarEntry(entry_id="33333333-3333-4333-8333-333333333333", occurred_at=DT_CREATED),
    ),
    upstream_changed=(
        UpstreamChange(
            id="44444444-4444-4444-8444-444444444444", change=UpstreamChangeKind.SUPERSEDED
        ),
    ),
)


def clear_hivemind_env(monkeypatch: pytest.MonkeyPatch) -> None:
    """Drop every HIVEMIND_* / ENVIRONMENT var this process inherited."""
    import os

    for name in list(os.environ):
        if name.startswith("HIVEMIND_") or name == "ENVIRONMENT":
            monkeypatch.delenv(name, raising=False)


async def active_agent(app: HivemindApp, name: str = "alice") -> str:
    """Register ``name`` in a fleet of its own and activate it; the key of its agent."""
    fleet = await app.store.create_fleet(f"fleet-{name}")
    await app.store.register_agent(name)
    _, key = await app.access_service.activate(name, TrustLevel.CONTRIBUTOR, fleet.id, ADMIN)
    return key
