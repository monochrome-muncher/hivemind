"""Shared test fakes at the port seams (TDD).

Every fake implements a *port* (``Store``, ``Embedder``, ``clock``),
never a concrete class, so tests sit at the seam. The clock is
caller-controlled so time-dependent logic (decay, tie-breaks) is fully
deterministic.
"""

from __future__ import annotations

import hashlib
import inspect
import re
from collections.abc import Awaitable, Callable
from dataclasses import replace
from datetime import UTC, datetime, timedelta
from typing import Any

from hivemind.config import SearchConfig
from hivemind.domain.access import AgentStatus, InvalidAgentStatus, KeyLocked, UnknownAgent
from hivemind.domain.audit import ActorKind, AuditAction, AuditEvent
from hivemind.domain.entry import (
    EntityKind,
    EntryDraft,
    ExtractedEntity,
    Kind,
    embeddable_text,
)
from hivemind.memstore import MemoryStore
from hivemind.ports import Credential, KeyReplacement, Store, entry_embeddable_text

FIXED_NOW = datetime(2026, 6, 1, 12, 0, 0, tzinfo=UTC)

# A "salient" token for the FakeExtractor's default mode: a word of at
# least 4 characters (regex \w{4,} — deterministic, no punctuation).
_SALIENT_TOKEN_RE = re.compile(r"\w{4,}")


class FixedClock:
    """A clock that returns a fixed, advanceable time."""

    def __init__(self, start: datetime) -> None:
        self._now = start

    def __call__(self) -> datetime:
        return self._now

    def advance_days(self, days: float) -> None:
        self._now += timedelta(days=days)

    def set(self, now: datetime) -> None:
        self._now = now


class FakeEmbedder:
    """Deterministic token-hash embedder (4 dims).

    Overlapping tokens produce similar vectors; identical text produces
    identical vectors — enough semantics to test the vector stream
    without a real embedding service.
    """

    def __init__(self, dimension: int = 4) -> None:
        self._dimension = dimension

    @property
    def dimension(self) -> int:
        return self._dimension

    @property
    def model_name(self) -> str:
        return "fake-embedder"

    def _vector(self, text: str) -> list[float]:
        vec = [0.0] * self._dimension
        for token in text.lower().split():
            digest = hashlib.sha256(token.encode()).digest()
            for i in range(self._dimension):
                byte = digest[(i * 3) % 32]
                sign = 1.0 if byte % 2 == 0 else -1.0
                vec[i] += sign * (byte / 255.0)
        norm = (sum(v * v for v in vec) ** 0.5) or 1.0
        return [v / norm for v in vec]

    async def embed_text(self, text: str) -> list[float]:
        return self._vector(text)

    async def embed_entry(self, draft: EntryDraft) -> list[float]:
        return self._vector(entry_embeddable_text(draft))

    def entry_embeddable_text(self, draft: EntryDraft) -> str:
        return embeddable_text(draft.summary, draft.body)


class RecordingEmbedder(FakeEmbedder):
    """The hash embedder, remembering every text it was asked to embed."""

    def __init__(self) -> None:
        super().__init__()
        self.texts: list[str] = []

    async def embed_text(self, text: str) -> list[float]:
        self.texts.append(text)
        return await super().embed_text(text)


class CountingEmbedder:
    """Wraps the fake embedder and counts calls."""

    dimension = 4
    model_name = "fake-embedder"

    def __init__(self) -> None:
        self.inner = make_embedder()
        self.calls = 0

    async def embed_text(self, text: str) -> list[float]:
        self.calls += 1
        return await self.inner.embed_text(text)

    async def embed_entry(self, d: EntryDraft) -> list[float]:
        self.calls += 1
        return await self.inner.embed_entry(d)


def minimal_draft(**over: object) -> EntryDraft:
    """A valid one-field-per-need fact draft; keyword overrides replace fields."""
    base: dict[str, object] = {"kind": Kind.FACT, "summary": "s", "author": "a", "agent": "g"}
    return EntryDraft(**{**base, **over})  # type: ignore[arg-type]


class FakeExtractor:
    """Deterministic Extractor fake (ADR 0016): no live LLM needed.

    Two deterministic modes:
    * ``entities=None`` (default): "extract" the first three distinct
      salient tokens (length ≥ 4) of the entry's embeddable text, each
      with a stable hash-picked kind — enough semantics to test the
      write path end-to-end without a real extractor.
    * ``entities=(...)``: a scripted fixed result (e.g. to assert the
      exact stored facets).

    ``fail=True`` makes every call raise — for the WriteService
    best-effort path (an extraction failure must not block the write).
    """

    def __init__(
        self,
        *,
        entities: tuple[ExtractedEntity, ...] | None = None,
        fail: bool = False,
    ) -> None:
        self._scripted = entities
        self._fail = fail

    @property
    def model_name(self) -> str:
        return "fake-extractor"

    def _default_entities(self, draft: EntryDraft) -> tuple[ExtractedEntity, ...]:
        text = entry_embeddable_text(draft).lower()
        seen: set[str] = set()
        out: list[ExtractedEntity] = []
        for token in _SALIENT_TOKEN_RE.findall(text):
            if token in seen:
                continue
            seen.add(token)
            digest = hashlib.sha256(token.encode()).digest()
            kind = list(EntityKind)[digest[0] % len(list(EntityKind))]
            out.append(ExtractedEntity(name=token, kind=kind))
            if len(out) == 3:
                break
        return tuple(out)

    async def extract_entry(self, draft: EntryDraft) -> tuple[ExtractedEntity, ...]:
        if self._fail:
            raise RuntimeError("fake extractor failure (scripted)")
        if self._scripted is not None:
            return self._scripted
        return self._default_entities(draft)


def make_clock(start: datetime | None = None) -> FixedClock:
    return FixedClock(start or FIXED_NOW)


def make_embedder() -> FakeEmbedder:
    return FakeEmbedder()


def make_store(clock: FixedClock | None = None) -> MemoryStore:
    return MemoryStore(clock or make_clock())


async def seed_entity_entries(store: Store) -> dict[str, str]:
    """Seed two entries carrying machine-extracted entity facets (ADR 0016);
    ``a`` has Postgres and auth-service, ``b`` has Kubernetes."""
    a = await store.create_entry(
        EntryDraft(
            kind=Kind.FACT,
            summary="Postgres connection pool exhausted",
            author="alice",
            agent="a1",
        ),
        entities=(
            ExtractedEntity(name="Postgres", kind=EntityKind.SYSTEM),
            ExtractedEntity(name="auth-service", kind=EntityKind.SERVICE),
        ),
        entities_model="test-extractor",
    )
    b = await store.create_entry(
        EntryDraft(
            kind=Kind.FACT,
            summary="Kubernetes autoscaler misconfigured",
            author="alice",
            agent="a1",
        ),
        entities=(ExtractedEntity(name="Kubernetes", kind=EntityKind.SYSTEM),),
        entities_model="test-extractor",
    )
    return {"a": a.id, "b": b.id}


class RecordingStore(MemoryStore):
    """A ``MemoryStore`` that appends the name of every public ``Store``
    method called on it to ``calls`` (a call, not an attribute read)."""

    def __init__(self, clock: Callable[[], datetime] | None = None) -> None:
        super().__init__(clock or make_clock())
        self.calls: list[str] = []
        for name, _ in inspect.getmembers(MemoryStore, inspect.iscoroutinefunction):
            if not name.startswith("_"):
                setattr(self, name, self._recorded(name, getattr(self, name)))

    def _recorded(self, name: str, method: Callable[..., Awaitable[Any]]) -> Any:
        async def call(*args: Any, **kwargs: Any) -> Any:
            self.calls.append(name)
            return await method(*args, **kwargs)

        return call


def make_search_config(
    *,
    candidate_top_k: int = 10,
    default_limit: int = 5,
    half_life_days: float = 30.0,
    rrf_k: int = 60,
    recency_floor: float | None = 0.8,
) -> SearchConfig:
    """A ``SearchConfig`` with test-sized defaults.

    The knobs a test is likely to vary are keyword parameters: the eval
    harness sweeps ``half_life_days`` to turn the SPEC §6.4 recency term
    on (the 30-day default) and effectively off (a half-life so long that
    every candidate's decay factor rounds to 1.0), ``rrf_k`` to widen
    or compress the fused RRF range (ROADMAP §4.6), and ``recency_floor``
    to bound the recency factor from below (ADR 0022; ``None`` = off =
    the pre-ADR-0022 unbounded form). ``rrf_k``'s and ``recency_floor``'s
    defaults here mirror ``SearchConfig``'s own, which this file must
    never silently diverge from
    (``test_make_search_config_mirrors_the_production_rrf_k``,
    ``test_the_sweeps_baseline_is_floor_off_explicitly_not_by_default``).
    The query instruction (ADR 0097) is off here, though on in production, so
    a test's embedded query is the raw query; a test of the instruction sets it.
    The vector similarity cut-off (ADR 0100) is likewise off here
    (``vector_min_similarity=None``): the hash embedder's similarities are not
    Qwen-like, so the 0.45 production default would empty its vector stream.
    A test of the cut-off sets it.
    """
    return SearchConfig(
        candidate_top_k=candidate_top_k,
        default_limit=default_limit,
        half_life_days=half_life_days,
        rrf_k=rrf_k,
        recency_floor=recency_floor,
        query_instruction="",
        vector_min_similarity=None,
    )


def make_entry_clocks(n: int) -> list[FixedClock]:
    """n independent clocks all starting at FIXED_NOW."""
    return [FixedClock(FIXED_NOW) for _ in range(n)]


def fake_key_id(raw_key: str) -> str:
    """The fingerprint a real ``PgAuthenticator`` would put on the
    credential for ``raw_key`` (ADR 0027): the first 12 hex chars of its
    SHA-256 hash."""
    return hashlib.sha256(raw_key.encode("utf-8")).hexdigest()[:12]


class FakeAuthenticator:
    """An in-memory ``Authenticator`` fake for unit tests (TDD at the seam).

    Implements the full ``Authenticator`` port (verify + key management,
    ADR 0012). ``verify`` returns pre-registered credentials; key
    management is in-memory. The org + admin keys are pre-registered.
    Admin credentials carry ``key_id`` like the real adapter (ADR 0027).
    """

    def __init__(
        self,
        agent_credentials: dict[str, Credential] | None = None,
        org_key: str = "hm_org",
        admin_key: str = "hm_admin",
        store: Store | None = None,
        clock: Callable[[], datetime] | None = None,
    ) -> None:
        # Bind a store to model the real adapter's status gate (ADR 0039):
        # keys issued here authenticate only while their agent is active,
        # and issuing for a non-active agent raises.
        self._store = store
        self._by_key: dict[str, Credential] = {
            org_key: Credential(user_id="org", is_org=True),
            admin_key: Credential(user_id="admin", is_admin=True, key_id=fake_key_id(admin_key)),
            **(agent_credentials or {}),
        }
        self._issued_agent_keys: dict[str, str] = {}
        self._rotations = 0
        # Last use per key and how many times it was recorded (ADR 0110):
        # the real adapter's once-an-hour write, observable here.
        self._last_used: dict[str, datetime] = {}
        self.use_writes = 0
        self._clock: Callable[[], datetime] = clock or (lambda: datetime.now(UTC))
        self._replacements = 0
        self._locked: set[str] = set()  # raw keys locked from the portal

    async def verify(self, key: str) -> Credential | None:
        credential = self._by_key.get(key)
        if (
            credential is not None
            and self._store is not None
            and key in self._issued_agent_keys.values()
            and credential.agent_name is not None
        ):
            agent = await self._store.get_agent(credential.agent_name)
            if agent is None or agent.status is not AgentStatus.ACTIVE:
                return None
        if credential is not None and key in self._locked:
            raise KeyLocked
        if (
            credential is not None
            and isinstance(self._store, MemoryStore)
            and credential.agent_name is not None
            and key in self._issued_agent_keys.values()
        ):
            # Trust, home fleet and successions are read per request from the
            # agent's record, as the real adapter's one-query key check does
            # (ADR 0113).
            from_record = await self.credential_for_agent(credential.agent_name)
            assert from_record is not None
            credential = replace(from_record, key_id=credential.key_id)
        if credential is not None:
            self._record_use(key, credential)
        return credential

    async def credential_for_agent(self, name: str) -> Credential | None:
        """The credential of an active agent from its record, with no key (ADR 0136)."""
        if not isinstance(self._store, MemoryStore):
            return await self._registered_credential_for(name)
        agent = await self._store.get_agent(name)
        if agent is None or agent.status is not AgentStatus.ACTIVE:
            return None
        return Credential(
            user_id=name,
            agent_id=name,
            agent_name=name,
            access_controlled=True,
            trust_level=agent.trust_level,
            home_fleet_id=agent.home_fleet_id,
            inherits_from=await self._store.inherited_authors(name),
        )

    async def _registered_credential_for(self, name: str) -> Credential | None:
        """The pre-registered credential of ``name``, without its key, if its agent is active
        (or no store is bound to say otherwise)."""
        found = next((c for c in self._by_key.values() if c.agent_name == name), None)
        if found is None:
            return None
        if self._store is not None:
            agent = await self._store.get_agent(name)
            if agent is None or agent.status is not AgentStatus.ACTIVE:
                return None
        return replace(found, key_id=None)

    def _record_use(self, key: str, credential: Credential) -> None:
        """Mirror ``PgAuthenticator``: stamp the key at most once an hour."""
        now = self._clock()
        last = self._last_used.get(key)
        if last is not None and now - last < timedelta(hours=1):
            return
        self._last_used[key] = now
        self.use_writes += 1
        if isinstance(self._store, MemoryStore) and credential.agent_name is not None:
            self._store.record_key_use(credential.agent_name, now)

    async def issue_agent_key(self, agent_name: str) -> str:
        """Issue an agent key bound to ``agent_name``; return the raw key once."""
        if self._store is not None:
            agent = await self._store.get_agent(agent_name)
            if agent is None:
                raise UnknownAgent(f"unknown agent: {agent_name}")
            if agent.status is not AgentStatus.ACTIVE:
                raise InvalidAgentStatus(agent_name, agent.status, "issue a key for")
        # One live key per agent: issuing again kills the old one, so the new
        # raw key must differ from it (the first stays ``hm_agent_<name>``).
        old_raw = self._issued_agent_keys.get(agent_name)
        raw = f"hm_agent_{agent_name}"
        if old_raw is not None:
            self._by_key.pop(old_raw, None)
            self._replacements += 1
            raw = f"hm_agent_{agent_name}_{self._replacements}"
        self._issued_agent_keys[agent_name] = raw
        self._locked.discard(raw)
        if isinstance(self._store, MemoryStore):
            self._store.record_key_issued(agent_name, self._clock())
        self._by_key[raw] = Credential(
            user_id=agent_name, agent_name=agent_name, key_id=fake_key_id(raw)
        )
        return raw

    async def replace_agent_key(
        self, agent_name: str, *, audit_actor_kind: ActorKind, audit_actor: str
    ) -> KeyReplacement:
        """Swap the agent's key: the old one stops verifying at once."""
        old_raw = self._issued_agent_keys.get(agent_name)
        if self._store is not None:
            agent = await self._store.get_agent(agent_name)
            if agent is None:
                raise UnknownAgent(f"unknown agent: {agent_name}")
            if agent.status is not AgentStatus.ACTIVE:
                raise InvalidAgentStatus(agent_name, agent.status, "replace the key of")
        self._replacements += 1
        raw = f"hm_agent_{agent_name}_{self._replacements}"
        if old_raw is not None:
            self._by_key.pop(old_raw, None)
        self._issued_agent_keys[agent_name] = raw
        self._by_key[raw] = Credential(
            user_id=agent_name, agent_name=agent_name, key_id=fake_key_id(raw)
        )
        if isinstance(self._store, MemoryStore):
            self._store.record_key_issued(agent_name, self._clock())  # a new key starts unused
        replacement = KeyReplacement(
            raw, fake_key_id(raw), fake_key_id(old_raw) if old_raw is not None else None
        )
        if self._store is not None:
            await self._store.record_audit(
                AuditEvent(
                    actor_kind=audit_actor_kind,
                    actor=audit_actor,
                    action=AuditAction.AGENT_KEY_REPLACE,
                    target=agent_name,
                    detail={
                        "old_key": replacement.old_key_id,
                        "new_key": replacement.new_key_id,
                    },
                )
            )
        return replacement

    async def lock_agent_key(self, agent_name: str, *, key_id: str) -> bool:
        """Lock the agent's key if its fingerprint is ``key_id`` (ADR 0117)."""
        raw = self._issued_agent_keys.get(agent_name)
        if raw is None or fake_key_id(raw) != key_id or raw in self._locked:
            return False
        self._locked.add(raw)
        if isinstance(self._store, MemoryStore):
            self._store.record_key_locked(agent_name, self._clock())
        if self._store is not None:
            await self._store.record_audit(
                AuditEvent(
                    actor_kind=ActorKind.AGENT_KEY,
                    actor=agent_name,
                    action=AuditAction.AGENT_KEY_LOCK,
                    target=agent_name,
                    detail={"key": key_id, "via": "portal"},
                )
            )
        return True

    async def revoke_agent_key(self, agent_name: str) -> None:
        raw = self._issued_agent_keys.pop(agent_name, None)
        if raw is not None:
            self._by_key.pop(raw, None)

    async def rotate_org_key(self) -> str:
        for old in [k for k, c in self._by_key.items() if c.is_org]:  # all prior org keys die
            del self._by_key[old]
        self._rotations += 1
        new_key = f"hm_org_{self._rotations}"
        self._by_key[new_key] = Credential(user_id="org", is_org=True)
        return new_key

    async def issue_admin_key(self) -> str:
        raw = f"hm_admin_{len(self._by_key)}"
        self._by_key[raw] = Credential(user_id="admin", is_admin=True, key_id=fake_key_id(raw))
        return raw


def make_authenticator(
    agent_credentials: dict[str, Credential] | None = None,
) -> FakeAuthenticator:
    """A ``FakeAuthenticator`` with the given pre-registered agent credentials."""
    return FakeAuthenticator(agent_credentials=agent_credentials)
