"""Shared test fakes at the port seams (TDD).

Every fake implements a *port* (``Store``, ``Embedder``, ``clock``),
never a concrete class, so tests sit at the seam. The clock is
caller-controlled so time-dependent logic (decay, tie-breaks) is fully
deterministic.
"""

from __future__ import annotations

import hashlib
import re
from datetime import UTC, datetime, timedelta

from hivemind.config import SearchConfig
from hivemind.domain.entry import (
    EntityKind,
    EntryDraft,
    ExtractedEntity,
    embeddable_text,
)
from hivemind.memstore import MemoryStore
from hivemind.ports import Credential, entry_embeddable_text

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
    """
    return SearchConfig(
        candidate_top_k=candidate_top_k,
        default_limit=default_limit,
        half_life_days=half_life_days,
        rrf_k=rrf_k,
        recency_floor=recency_floor,
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
    ) -> None:
        self._by_key: dict[str, Credential] = {
            org_key: Credential(user_id="org", is_org=True),
            admin_key: Credential(user_id="admin", is_admin=True, key_id=fake_key_id(admin_key)),
        }
        self._issued_agent_keys: dict[str, str] = {}
        self._rotations = 0

    async def verify(self, key: str) -> Credential | None:
        return self._by_key.get(key)

    async def issue_agent_key(self, agent_name: str) -> str:
        """Issue an agent key bound to ``agent_name``; return the raw key once."""
        raw = f"hm_agent_{agent_name}"
        self._issued_agent_keys[agent_name] = raw
        self._by_key[raw] = Credential(user_id=agent_name, agent_name=agent_name)
        return raw

    async def revoke_agent_key(self, agent_name: str) -> None:
        raw = self._issued_agent_keys.pop(agent_name, None)
        if raw is not None:
            self._by_key.pop(raw, None)

    async def rotate_org_key(self) -> str:
        self._by_key.pop(
            "hm_org" if self._rotations == 0 else f"hm_org_{self._rotations - 1}", None
        )
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
