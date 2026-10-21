"""Configuration knobs for Hivemind (SPEC.md §6.2, §6.4, §7).

``SearchConfig`` is a plain frozen dataclass — pure values, no env
parsing — so the search pipeline is testable with explicit numbers.
``Settings`` layers pydantic-settings on top to load the same knobs
from the environment (HIVEMIND_* prefix) for the deployable service.

Fusion weights, RRF k, decay half-life, and the quality formula are
deliberately config, not code constants (ADR 0006, SPEC.md §11):
tuning retrieval is a config change, not a code change.
"""

from __future__ import annotations

import difflib
import logging
import math
import os
from dataclasses import dataclass
from pathlib import Path
from urllib.parse import urlsplit

from dotenv import dotenv_values
from pydantic import Field, field_validator, model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

from hivemind.domain.entry import DEFAULT_PREFIX_TOKENS, Kind
from hivemind.domain.recency import RecencyHint
from hivemind.domain.validation import check_query_instruction
from hivemind.providers import clean_api_key, clean_endpoint

logger = logging.getLogger(__name__)

# The HIVEMIND_STRICT_ENV spellings that mean "on" (ADR 0032).
_TRUTHY = frozenset({"1", "true", "yes", "on"})


def _normalise_recency_by_kind(
    value: object,
) -> tuple[tuple[Kind, float], ...] | None:
    """``value`` as a tuple of (Kind, half-life days) sorted by kind, or ``None``.

    Rejects an unknown kind, a repeated kind, and a half-life that is not a
    finite number > 0 (ADR 0024, 0072). An empty value is ``None``.
    """
    if value is None:
        return None
    pairs = list(value.items()) if isinstance(value, dict) else list(value)  # type: ignore[call-overload]
    seen: dict[Kind, float] = {}
    for item in pairs:
        try:
            raw_kind, days = item
        except TypeError, ValueError:
            raise ValueError(
                f"recency_by_kind: expected (kind, days) pairs, got {item!r}"
            ) from None
        try:
            kind = Kind(raw_kind)
        except ValueError:
            known = ", ".join(k.value for k in Kind)
            raise ValueError(
                f"recency_by_kind: unknown kind {raw_kind!r} (known: {known})"
            ) from None
        if kind in seen:
            raise ValueError(f"recency_by_kind: kind {kind.value!r} is named twice")
        if isinstance(days, bool) or not isinstance(days, int | float):
            raise ValueError(f"recency_by_kind: {kind.value} must be a number, got {days!r}")
        if not math.isfinite(days) or days <= 0:
            raise ValueError(
                f"recency_by_kind: {kind.value} must be a finite number of days > 0, got {days}"
            )
        seen[kind] = float(days)
    if not seen:
        return None
    return tuple(sorted(seen.items(), key=lambda pair: pair[0].value))


def parse_recency_by_kind(text: str) -> tuple[tuple[Kind, float], ...] | None:
    """Parse ``HIVEMIND_RECENCY_BY_KIND``: ``insight=180,decision=365`` (ADR 0072).

    Empty or "none" (case-insensitive) means off. Kinds are case-insensitive;
    unknown or repeated kinds and non-finite or non-positive values raise
    ``ValueError`` (via ``SearchConfig`` for the value rules).
    """
    text = text.strip()
    if text.lower() in ("", "none"):
        return None
    pairs: list[tuple[str, float]] = []
    for part in text.split(","):
        name, sep, number = part.partition("=")
        name = name.strip().lower()
        if not sep or not name:
            raise ValueError(
                f"HIVEMIND_RECENCY_BY_KIND must look like insight=180,decision=365, got {text!r}"
            )
        try:
            days = float(number.strip())
        except ValueError:
            raise ValueError(
                f"HIVEMIND_RECENCY_BY_KIND: {name} needs a number of days, got {number.strip()!r}"
            ) from None
        pairs.append((name, days))
    return _normalise_recency_by_kind(pairs)


# The floor under ``prefer_recent`` (ADR 0073); see docs/retrieval-experiments.md.
PREFER_RECENT_FLOOR: float | None = 0.5
# The task line put before the search query for an instruction-aware embedder
# (ADR 0097). On by default: measured to help the Qwen3-Embedding family.
# ``HIVEMIND_QUERY_INSTRUCTION=""`` turns it off. tests/eval/query_instruction.py
# measures this same text.
DEFAULT_QUERY_INSTRUCTION = (
    "Given an agent's task or question, retrieve shared memory entries that answer it"
)


@dataclass(frozen=True, slots=True)
class RecencyTerms:
    """The age inputs of ``entry_score`` for one entry (ADR 0073)."""

    half_life_days: float
    recency_floor: float | None
    use_confirmation: bool


@dataclass(frozen=True, slots=True)
class SearchConfig:
    """Tunable retrieval parameters (defaults follow SPEC.md §6.2/§6.4)."""

    rrf_k: int = 60
    weight_keyword: float = 0.5
    weight_vector: float = 0.5
    candidate_top_k: int = 20
    default_limit: int = 10
    half_life_days: float = 30.0
    # Lower bound on the recency factor so it cannot dominate RRF's fused
    # range (ADR 0022, SPEC §6.4). A band, not a slider: 0.9 scores worse
    # than off, 1.0 is off. Do not tune it up. ``None`` = no floor.
    recency_floor: float | None = 0.8
    # The vector stream keeps only entries whose cosine similarity to the
    # query is above this (ADR 0062). Model-specific, so off by default:
    # ``None`` keeps ADR 0061's floor of 0. In [0, 1).
    vector_min_similarity: float | None = None
    # Read-time grouping of unlinked versions of one fact (ADR 0074): two
    # active candidates whose stored vectors are at least this similar are
    # shown adjacent, newest first, each naming the other. Nothing is hidden
    # or merged and no score changes. Model-specific, so off by default:
    # ``None`` = today's behaviour. In (0, 1) when set.
    version_group_min_similarity: float | None = None
    # Consensus-share feedback quality (ADR 0063): each weight is where
    # quality heads when every report gives that verdict; the prior counts
    # imaginary neutral reports (> 0).
    quality_helpful_weight: float = 0.2
    quality_stale_weight: float = 0.4
    quality_wrong_weight: float = 1.0
    quality_prior: float = 2.0
    quality_min: float = 0.5
    quality_max: float = 1.2
    # Ceiling on the upward effect of all social signals together (ADR
    # 0070). Finite and >= 1: below 1 it would penalise unrated entries.
    social_max: float = 1.2
    # Weight of relevance against novelty in an ``explore`` search's MMR
    # re-ordering (ADR 0078): 1 is plain relevance order, lower pushes
    # near-copies of a higher hit down. In (0, 1].
    explore_lambda: float = 0.7
    # A confirmation ("last confirmed", SPEC §4.2) restarts the recency
    # clock for the upside, inside the social_max cap (ADR 0071). Off until
    # measured with the real embedders.
    recency_from_confirmation: bool = False
    # A half-life in days per kind, as ``((Kind.INSIGHT, 180.0), ...)``
    # (ADR 0072); a kind not named keeps ``half_life_days``. The floor is
    # the same for every kind. ``None`` = one curve for all, today's
    # behaviour exactly. Normalised to a sorted tuple, so it is hashable.
    recency_by_kind: tuple[tuple[Kind, float], ...] | None = None
    # A search may carry a ``recency`` hint (ADR 0073). Off: the hint is
    # accepted and ignored, every search scores as the default. On (the
    # default, ADR 0101): ``prefer_recent`` and ``ignore_age`` change that one
    # search's scoring; a search with no hint scores as before.
    recency_hint_enabled: bool = True
    # The floor ``prefer_recent`` scores under (ADR 0073): lower than
    # ``recency_floor`` so age counts for more, or ``None`` for no floor. Not
    # an environment setting; a search config built by a test or the
    # measurement harness can vary it.
    prefer_recent_floor: float | None = PREFER_RECENT_FLOOR
    # The importance factor spans 0.9..1.1 (one step about 1.05x, below
    # ``social_max``) instead of 0.6..1.0 (ADR 0088). On by default since
    # the measurement with the Qwen3 embedders; false restores 0.6..1.0.
    narrow_importance: bool = True
    # For an instruction-aware embedder (Qwen3-Embedding, ADR 0097): the task
    # line put before the search query as ``Instruct: {task}\nQuery:{query}``
    # on the vector stream only. Entries are never embedded with it. On by
    # default; empty turns it off.
    query_instruction: str = DEFAULT_QUERY_INSTRUCTION

    def __post_init__(self) -> None:
        # Fail at config time (ADR 0024), not per request as a
        # ZeroDivisionError, a negative LIMIT, empty results or NaN scores.
        object.__setattr__(
            self, "recency_by_kind", _normalise_recency_by_kind(self.recency_by_kind)
        )
        if self.rrf_k < 0:
            raise ValueError(f"rrf_k must be >= 0, got {self.rrf_k}")
        if self.candidate_top_k < 1:
            raise ValueError(f"candidate_top_k must be >= 1, got {self.candidate_top_k}")
        if self.default_limit < 1:
            raise ValueError(f"default_limit must be >= 1, got {self.default_limit}")
        for name in (
            "weight_keyword",
            "weight_vector",
            "quality_helpful_weight",
            "quality_stale_weight",
            "quality_wrong_weight",
            "quality_min",
            "quality_max",
        ):
            value = getattr(self, name)
            if not math.isfinite(value) or value < 0:
                raise ValueError(f"{name} must be a finite number >= 0, got {value}")
        if not math.isfinite(self.quality_prior) or self.quality_prior <= 0:
            raise ValueError(f"quality_prior must be a finite number > 0, got {self.quality_prior}")
        if self.quality_min > self.quality_max:
            raise ValueError(
                f"quality_min ({self.quality_min}) must be <= quality_max ({self.quality_max})"
            )
        if not math.isfinite(self.social_max) or self.social_max < 1.0:
            raise ValueError(f"social_max must be a finite number >= 1, got {self.social_max}")
        if not math.isfinite(self.half_life_days) or self.half_life_days <= 0:
            # A zero/negative half life would divide by zero (or silently
            # invert decay) inside ``entry_score``; inf silently turns it off.
            raise ValueError(f"half_life_days must be finite and > 0, got {self.half_life_days}")
        if self.recency_floor is not None and not 0.0 < self.recency_floor <= 1.0:
            raise ValueError(f"recency_floor must be in (0, 1] or None, got {self.recency_floor}")
        if not (math.isfinite(self.explore_lambda) and 0.0 < self.explore_lambda <= 1.0):
            raise ValueError(f"explore_lambda must be in (0, 1], got {self.explore_lambda}")
        if self.prefer_recent_floor is not None and not 0.0 < self.prefer_recent_floor <= 1.0:
            raise ValueError(
                f"prefer_recent_floor must be in (0, 1] or None, got {self.prefer_recent_floor}"
            )
        threshold = self.vector_min_similarity
        if threshold is not None and not (math.isfinite(threshold) and 0.0 <= threshold < 1.0):
            # 1 or more would empty every vector list.
            raise ValueError(f"vector_min_similarity must be in [0, 1) or None, got {threshold}")
        check_query_instruction(self.query_instruction)
        group_cut = self.version_group_min_similarity
        if group_cut is not None and not (math.isfinite(group_cut) and 0.0 < group_cut < 1.0):
            # 0 would group everything; 1 or more groups nothing.
            raise ValueError(
                f"version_group_min_similarity must be in (0, 1) or None, got {group_cut}"
            )

    def embedded_query(self, query: str) -> str:
        """The text the vector stream embeds for ``query`` (ADR 0097): the
        query itself, or with the instruction in front when one is set. The
        keyword stream always gets the raw query."""
        if not self.query_instruction:
            return query
        return f"Instruct: {self.query_instruction}\nQuery:{query}"

    def half_life_for(self, kind: Kind) -> float:
        """The half-life in days entries of ``kind`` age by (ADR 0072)."""
        for named, days in self.recency_by_kind or ():
            if named == kind:
                return days
        return self.half_life_days

    def recency_terms(self, kind: Kind, hint: RecencyHint) -> RecencyTerms:
        """How age enters the score of an entry of ``kind`` under ``hint``.

        Pure: nothing is stored, so one search's hint cannot reach another.
        With ``recency_hint_enabled`` off every hint scores as ``DEFAULT``
        (ADR 0073), exactly as before the hint existed.
        """
        half_life = self.half_life_for(kind)
        if self.recency_hint_enabled:
            if hint is RecencyHint.IGNORE_AGE:
                # 0.5 ** (age / inf) is exactly 1.0 for every finite age; no
                # floor, and no confirmation clock, which would be age again.
                return RecencyTerms(math.inf, None, use_confirmation=False)
            if hint is RecencyHint.PREFER_RECENT:
                # Never weaker than the default: a deployment with a lower
                # floor, or none, keeps it.
                floor = self.prefer_recent_floor
                if self.recency_floor is None:
                    floor = None
                elif floor is not None:
                    floor = min(floor, self.recency_floor)
                return RecencyTerms(half_life, floor, self.recency_from_confirmation)
        return RecencyTerms(half_life, self.recency_floor, self.recency_from_confirmation)

    def applied_hint(self, hint: RecencyHint) -> RecencyHint:
        """The hint ``recency_terms`` honours: ``DEFAULT`` while the setting is off."""
        return hint if self.recency_hint_enabled else RecencyHint.DEFAULT

    def quality_kwargs(self) -> dict[str, float]:
        """Keyword args for ``retrieval.scoring.feedback_quality``."""
        return {
            "helpful_weight": self.quality_helpful_weight,
            "stale_weight": self.quality_stale_weight,
            "wrong_weight": self.quality_wrong_weight,
            "prior": self.quality_prior,
            "min_quality": self.quality_min,
            "max_quality": self.quality_max,
        }


class Settings(BaseSettings):
    """Environment-driven service settings (HIVEMIND_* env vars).

    The class default reads ``.env.local`` (the fallback profile, ADR
    0017); ``load_settings()`` picks the profile from ``ENVIRONMENT``.
    Real env vars win over file values and a missing file is ignored.
    """

    model_config = SettingsConfigDict(
        env_prefix="HIVEMIND_",
        extra="ignore",
        env_file=".env.local",
        # A validation error must never echo a value back (an API key).
        hide_input_in_errors=True,
    )

    # ``repr=False`` on every secret-bearing field: a stray ``%r`` of the
    # settings object must not print a password or key.
    database_url: str = Field(
        default="postgresql://hivemind:hivemind@localhost:5432/hivemind", repr=False
    )
    # Optional DIRECT (non-pooled) DSN for `hivemind-migrate` only: its
    # session advisory lock (ADR 0020) cannot be held through a
    # transaction-mode PgBouncer. Empty = migrate through `database_url`.
    migrate_database_url: str = Field(default="", repr=False)
    # Root logger level (DEBUG/INFO/WARNING/ERROR/CRITICAL, case-insensitive).
    log_level: str = "INFO"
    # Read only by `hivemind-admin` (ADR 0029). `admin_api_url`: base URL
    # of the hivemind-api it proxies to (e.g. `http://hivemind-api:8000`);
    # empty is a startup error for that runner. `admin_api_ca_bundle`: PEM
    # file to verify an https API URL against; empty = system trust store.
    admin_api_url: str = ""
    admin_api_ca_bundle: str = ""
    # Read only by `hivemind-portal` (ADR 0095), with the same meaning as
    # the two admin settings above.
    portal_api_url: str = ""
    portal_api_ca_bundle: str = ""
    # MCP-HTTP DNS-rebinding guard (ADR 0042): comma-separated accepted
    # Host values (``name`` or ``name:*``) and browser Origins. Empty hosts
    # = the MCP SDK default (guard only on a loopback bind), which keeps an
    # ingress-fronted 0.0.0.0 deployment working.
    mcp_allowed_hosts: str = ""
    mcp_allowed_origins: str = ""
    # Unknown HIVEMIND_* variables are logged by default, since platforms
    # inject prefixed variables; True makes them a startup failure (ADR
    # 0032, for CI and dev). Read by load_settings() BEFORE Settings is built.
    strict_env: bool = False
    # ADR 0082: when true, a dev-mode credential (one that is not an admin
    # key, an org key, or an active agent's key) sits at trust level 0 on
    # the data plane. Off by default, so the keyless dev runner and local
    # scripts are unchanged; production deployments set it true.
    require_stored_keys: bool = False
    # asyncpg pool size, per pod (same defaults as ``make_pool``).
    pool_min_size: int = Field(default=1, ge=0)
    pool_max_size: int = Field(default=10, ge=1)
    # Bounds on Postgres calls from the app pools (never `hivemind-migrate`,
    # whose CREATE INDEX CONCURRENTLY must run unbounded). Seconds; 0
    # disables the command/acquire bound. `pool_statement_timeout_ms` is an
    # OPT-IN server-side bound sent as a startup parameter; a
    # transaction-mode PgBouncer rejects or ignores it, so leave it 0 (off)
    # unless the pool connects to Postgres directly.
    pool_command_timeout: float = Field(default=30.0, ge=0, allow_inf_nan=False)
    pool_statement_timeout_ms: int = Field(default=0, ge=0)
    pool_acquire_timeout: float = Field(default=10.0, ge=0, allow_inf_nan=False)
    pool_connect_timeout: float = Field(default=10.0, ge=0, allow_inf_nan=False)
    embedding_endpoint: str = "http://localhost:8001/v1"
    embedding_api_key: str = Field(default="", repr=False)
    embedding_model: str = "text-embedding-3-small"
    # Per-request timeout (seconds) when the embedder builds its own client.
    # Feeds the k8s `terminationGracePeriodSeconds` arithmetic
    # (`deploy/kubernetes/*.yaml`, DEPLOY.md §5): raise both together.
    embedding_timeout: float = Field(default=10.0, gt=0, allow_inf_nan=False)
    # Wall-clock budget for ONE embed call, retries and backoff included
    # (httpx's timeout is per phase; ADR 0041). Unset = every attempt at
    # full timeout plus backoff (``providers.default_deadline``, the
    # DEPLOY.md §5 worst case). A value below the timeout is a startup error.
    embedding_deadline: float | None = Field(default=None, gt=0, allow_inf_nan=False)
    # Default when HIVEMIND_EMBEDDING_DIM is unset (ADR 0015): 1024, which
    # OpenAI-compatible endpoints (``dimensions``) and Matryoshka servers
    # can both serve, not one provider's native 1536. The dev Makefile pins 512.
    embedding_dim: int = Field(default=1024, ge=1, le=2000)  # pgvector HNSW cap
    # Embedded-text body budget in whitespace-delimited words, NOT
    # tokenizer tokens (ADR 0021); shared with the extractor (SPEC §13.1).
    embedding_prefix_tokens: int = Field(default=DEFAULT_PREFIX_TOKENS, ge=0)  # 0 = summary only
    # Retry budget for transient embedder failures (timeouts, connection
    # errors, 429, 5xx) — ADR 0014; 0 disables retrying.
    embedding_retries: int = Field(default=2, ge=0)

    # Entity extractor (ADR 0016, SPEC §13): an OpenAI-compatible *chat*
    # endpoint. Empty disables extraction: entries land with empty
    # `entities` and no LLM cost.
    extractor_endpoint: str = ""
    extractor_api_key: str = Field(default="", repr=False)
    extractor_model: str = ""
    extractor_timeout: float = Field(default=30.0, gt=0, allow_inf_nan=False)
    # The overall wall-clock budget for ONE extraction, retries included.
    extractor_deadline: float | None = Field(default=None, gt=0, allow_inf_nan=False)
    # Retry budget for transient extractor failures (ADR 0014 pattern);
    # 0 disables retrying. A failure never blocks the write (best-effort).
    extractor_retries: int = Field(default=2, ge=0)

    # How long (seconds) MetricsService reuses the grouped scan of the
    # entries behind /v1/metrics and /metrics (ADR 0069); 0 scans on every
    # report. The entry counts can lag by up to this long.
    metrics_entry_counts_ttl: float = Field(default=300.0, ge=0, allow_inf_nan=False)

    # Retrieval knobs, mirroring SearchConfig (whose __post_init__ holds
    # the value rules, run at load time by ``_check_consistency``).
    rrf_k: int = 60
    weight_keyword: float = 0.5
    weight_vector: float = 0.5
    candidate_top_k: int = 20
    default_limit: int = 10
    half_life_days: float = 30.0
    # See SearchConfig.recency_floor (ADR 0022). In (0, 1]; empty or "none"
    # (case-insensitive) means no floor (ADR 0023).
    recency_floor: float | None = 0.8
    # See SearchConfig.vector_min_similarity (ADR 0062). Unset, empty or
    # "none" (case-insensitive) means off.
    vector_min_similarity: float | None = None
    # See SearchConfig.version_group_min_similarity (ADR 0074). Same
    # spellings for off.
    version_group_min_similarity: float | None = None
    # See SearchConfig (ADR 0063).
    quality_helpful_weight: float = 0.2
    quality_stale_weight: float = 0.4
    quality_wrong_weight: float = 1.0
    quality_prior: float = 2.0
    quality_min: float = 0.5
    quality_max: float = 1.2
    # See SearchConfig.social_max (ADR 0070).
    social_max: float = 1.2
    # See SearchConfig.explore_lambda (ADR 0078).
    explore_lambda: float = 0.7
    # See SearchConfig.recency_from_confirmation (ADR 0071).
    recency_from_confirmation: bool = False
    # See SearchConfig.recency_by_kind (ADR 0072): ``insight=180,decision=365``.
    # Empty or "none" means one curve for every kind.
    recency_by_kind: str = ""
    # See SearchConfig.recency_hint_enabled (ADR 0073).
    recency_hint: bool = True
    # See SearchConfig.narrow_importance (ADR 0088).
    narrow_importance: bool = True
    # See SearchConfig.query_instruction (ADR 0097). Defaults to
    # DEFAULT_QUERY_INSTRUCTION; an explicitly empty value means off;
    # surrounding whitespace is stripped.
    query_instruction: str = DEFAULT_QUERY_INSTRUCTION

    @field_validator("query_instruction")
    @classmethod
    def _clean_query_instruction(cls, value: str) -> str:
        return value.strip()

    @field_validator("embedding_api_key", "extractor_api_key")
    @classmethod
    def _clean_api_key(cls, value: str) -> str:
        """Strip whitespace (a k8s Secret / ``--from-file`` value usually
        ends in a newline, which makes httpx refuse the header) and
        reject control characters. Never echoes the value."""
        return clean_api_key(value)

    @field_validator("embedding_endpoint", "extractor_endpoint")
    @classmethod
    def _clean_endpoint(cls, value: str) -> str:
        """Strip surrounding whitespace (a trailing newline otherwise
        surfaces as a raw InvalidURL at the first request); reject inner
        whitespace. Never echoes the value (it may carry userinfo)."""
        return clean_endpoint(value)

    @property
    def migration_dsn(self) -> str:
        """The DSN ``hivemind-migrate`` uses: ``migrate_database_url`` when
        set, else ``database_url`` (ROADMAP 3.13)."""
        return self.migrate_database_url.strip() or self.database_url

    @model_validator(mode="after")
    def _check_consistency(self) -> Settings:
        """Cross-field checks, at startup with a clear message."""
        if self.pool_min_size > self.pool_max_size:
            raise ValueError(
                f"pool_min_size ({self.pool_min_size}) must be <= "
                f"pool_max_size ({self.pool_max_size})"
            )
        self.search_config()  # the retrieval knobs' rules live on SearchConfig
        for name, endpoint, key in (
            ("embedding", self.embedding_endpoint, self.embedding_api_key),
            ("extractor", self.extractor_endpoint, self.extractor_api_key),
        ):
            if not endpoint:
                continue
            parts = urlsplit(endpoint)
            if parts.scheme not in ("http", "https") or not parts.hostname:
                raise ValueError(
                    f"{name}_endpoint must be an http(s) URL with a host "
                    f"(got {endpoint_label(endpoint)!r})"
                )
            if key and parts.scheme == "http" and not _is_loopback(parts.hostname):
                logger.warning(
                    "%s_endpoint %s is plain http and an API key is set: the key travels "
                    "unencrypted; use https unless the network is trusted",
                    name,
                    endpoint_label(endpoint),
                )
        for name, timeout, deadline in (
            ("embedding", self.embedding_timeout, self.embedding_deadline),
            ("extractor", self.extractor_timeout, self.extractor_deadline),
        ):
            if deadline is not None and deadline < timeout:
                raise ValueError(
                    f"{name}_deadline ({deadline:g}s) must be >= {name}_timeout ({timeout:g}s)"
                )
        if self.extractor_endpoint and not self.extractor_model.strip():
            raise ValueError("extractor_model must be set when extractor_endpoint is set")
        return self

    @field_validator(
        "recency_floor", "vector_min_similarity", "version_group_min_similarity", mode="before"
    )
    @classmethod
    def _parse_recency_floor(cls, value: object) -> object:
        """Map empty / "none" (case-insensitive) to ``None``: no floor (ADR
        0023), no similarity threshold (ADR 0062).

        Anything else is left to pydantic's float parsing and the range
        checks in ``SearchConfig.__post_init__``.
        """
        if isinstance(value, str) and value.strip().lower() in ("", "none"):
            return None
        return value

    @field_validator("strict_env", mode="before")
    @classmethod
    def _parse_strict_env(cls, value: object) -> object:
        """Parse ``HIVEMIND_STRICT_ENV`` exactly as ``load_settings``'s
        pre-check does (ADR 0032): the ``_TRUTHY`` spellings are true and
        any other string (empty included) is false, so the two readings
        can never disagree."""
        if isinstance(value, str):
            return value.strip().lower() in _TRUTHY
        return value

    def search_config(self) -> SearchConfig:
        return SearchConfig(
            rrf_k=self.rrf_k,
            weight_keyword=self.weight_keyword,
            weight_vector=self.weight_vector,
            candidate_top_k=self.candidate_top_k,
            default_limit=self.default_limit,
            half_life_days=self.half_life_days,
            recency_floor=self.recency_floor,
            vector_min_similarity=self.vector_min_similarity,
            version_group_min_similarity=self.version_group_min_similarity,
            quality_helpful_weight=self.quality_helpful_weight,
            quality_stale_weight=self.quality_stale_weight,
            quality_wrong_weight=self.quality_wrong_weight,
            quality_prior=self.quality_prior,
            quality_min=self.quality_min,
            quality_max=self.quality_max,
            social_max=self.social_max,
            explore_lambda=self.explore_lambda,
            recency_from_confirmation=self.recency_from_confirmation,
            recency_by_kind=parse_recency_by_kind(self.recency_by_kind),
            recency_hint_enabled=self.recency_hint,
            narrow_importance=self.narrow_importance,
            query_instruction=self.query_instruction,
        )


def redact_url(url: str) -> str:
    """``url`` with any userinfo removed, for logging.

    Deliberately blunt: everything up to the last ``@`` (scheme kept) is
    dropped, so ``http://user:pa/ss@host`` and a scheme-less
    ``user:secret@host:8001/v1`` are both cleaned, at the price of
    trimming a path that legitimately contains ``@``.
    """
    scheme, sep, rest = url.partition("://")
    if not sep:
        scheme, rest = "", url
    if "@" in rest:
        rest = rest.rsplit("@", 1)[1]
    return f"{scheme}://{rest}" if sep else rest


def endpoint_label(url: str) -> str:
    """Scheme + host[:port] only, for error messages: never the path,
    query or userinfo of a configured endpoint."""
    cleaned = redact_url(url)
    scheme, sep, rest = cleaned.partition("://")
    if not sep:
        scheme, rest = "", cleaned
    host = rest.split("/", 1)[0].split("?", 1)[0].split("#", 1)[0]
    return f"{scheme}://{host}" if sep else host


def _is_loopback(host: str) -> bool:
    return host == "localhost" or host.startswith("127.") or host in ("::1", "[::1]")


# Environment profile files (ADR 0017), selected by ``ENVIRONMENT``.
_ENV_FILES = {
    "production": ".env.production",
    "prod": ".env.production",
    "staging": ".env.staging",
    "test": ".env.test",
}
_DEFAULT_ENV_FILE = ".env.local"


def env_file_for(environment: str | None) -> str:
    """The profile file for an ``ENVIRONMENT`` value (ADR 0017).

    ``production`` / ``staging`` / ``test`` map to their profile file;
    anything else (including unset / empty) maps to the ``.local``
    fallback. Case- and whitespace-insensitive.
    """
    key = (environment or "").strip().lower()
    return _ENV_FILES.get(key, _DEFAULT_ENV_FILE)


# ``HIVEMIND_*`` variables that are deliberately NOT ``Settings`` fields
# (read from ``os.environ`` or by non-Python code) and so must not be
# reported as unknown (ADR 0024). ``extra="forbid"`` on the model is not
# an option: RUNNER, HOST and PORT are set on every pod. Anything neither
# here nor a field is reported: a warning, or a failure with
# HIVEMIND_STRICT_ENV (ADR 0032).
_ALLOWED_EXTRA_ENV_VARS: dict[str, str] = {
    "HIVEMIND_RUNNER": "selects the runner in entrypoint.sh",
    "HIVEMIND_HOST": (
        "REST/MCP-HTTP/admin/portal bind host, read via os.environ in api/main.py, "
        "mcp/http.py, admin/main.py and portal/main.py"
    ),
    "HIVEMIND_PORT": (
        "REST/MCP-HTTP/admin/portal bind port, read via os.environ in api/main.py, "
        "mcp/http.py, admin/main.py and portal/main.py"
    ),
    # Per-process identity (ADR 0009), so deliberately outside Settings.
    "HIVEMIND_MCP_KEY": "per-agent hivemind-mcp-pg credential, read via os.environ in mcp/runners.py",
    "HIVEMIND_MCP_HTTP_PORT": "mcp-http port for docker compose and `make mcp-http-dev`; the app never reads it",
}


def _known_hivemind_env_vars() -> set[str]:
    """Every ``HIVEMIND_*`` name ``Settings`` itself understands."""
    prefix = Settings.model_config.get("env_prefix", "")
    return {f"{prefix}{name}".upper() for name in Settings.model_fields}


def _strict_env(env_file: str) -> bool:
    """Whether ``HIVEMIND_STRICT_ENV`` asks for a hard failure (ADR 0032).

    Read by hand because it decides how the check before ``Settings`` is
    built behaves. A real environment variable wins over the profile
    file, as everywhere else (ADR 0017)."""
    raw = os.environ.get("HIVEMIND_STRICT_ENV")
    if raw is None:
        file_path = Path(env_file)
        if file_path.is_file():
            raw = dotenv_values(file_path).get("HIVEMIND_STRICT_ENV")
    return (raw or "").strip().lower() in _TRUTHY


def _check_unknown_hivemind_env_vars(env_file: str) -> None:
    """Report a ``HIVEMIND_*`` variable ``Settings`` would ignore.

    One WARNING by default, since platforms inject prefixed variables
    (ADR 0032); raises with ``HIVEMIND_STRICT_ENV=true`` (ADR 0024).
    Checks both process env vars and the active profile file. Runs in
    ``load_settings()`` rather than on the model so tests constructing
    ``Settings(...)`` directly are unaffected.
    """
    known = _known_hivemind_env_vars() | _ALLOWED_EXTRA_ENV_VARS.keys()
    present = {name for name in os.environ if name.startswith("HIVEMIND_")}
    file_path = Path(env_file)
    if file_path.is_file():
        present |= {
            name for name in dotenv_values(file_path) if name and name.startswith("HIVEMIND_")
        }
    unknown = sorted(present - known)
    if not unknown:
        return
    # Match on the part after "HIVEMIND_": the shared prefix would
    # otherwise dominate the similarity ratio and suggest nonsense.
    suffix_to_known = {name.removeprefix("HIVEMIND_"): name for name in known}
    lines = []
    for name in unknown:
        candidates = difflib.get_close_matches(name.removeprefix("HIVEMIND_"), suffix_to_known, n=1)
        hint = f" — did you mean {suffix_to_known[candidates[0]]}?" if candidates else ""
        lines.append(f"  {name}{hint}")
    message = (
        "Unknown HIVEMIND_* environment variable(s) — neither a Settings "
        "field nor on the exemption list in src/hivemind/config.py "
        "(_ALLOWED_EXTRA_ENV_VARS):\n"
        + "\n".join(lines)
        + "\nIf one is a typo, fix the name. Variables injected by the platform "
        "(e.g. GitLab Auto DevOps, Kubernetes service links) can be ignored."
    )
    if _strict_env(env_file):
        raise RuntimeError(message + "\n(Failing because HIVEMIND_STRICT_ENV is set.)")
    logger.warning("%s", message)


def load_settings() -> Settings:
    """Build ``Settings`` for the active environment profile (ADR 0017).

    The profile file comes from ``env_file_for(ENVIRONMENT)``; real env
    vars win over it and a missing file is ignored. Unknown
    ``HIVEMIND_*`` variables are reported first (ADR 0024, ADR 0032).
    """
    env_file = env_file_for(os.environ.get("ENVIRONMENT"))
    _check_unknown_hivemind_env_vars(env_file)
    # `_env_file` is a documented init parameter missing from the mypy stubs.
    return Settings(_env_file=env_file)  # type: ignore[call-arg]


def configure_logging(level: str = "INFO") -> None:
    """Configure the root logger once, at process startup.

    Every runner calls this first; without a handler INFO lines are lost
    and formats differ. An invalid ``level`` name raises rather than
    silently falling back to WARNING.
    """
    numeric_level = logging.getLevelName(level.upper())
    if not isinstance(numeric_level, int):
        raise ValueError(
            f"invalid HIVEMIND_LOG_LEVEL {level!r}; expected one of "
            "DEBUG, INFO, WARNING, ERROR, CRITICAL"
        )
    logging.basicConfig(
        level=numeric_level,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )
    # httpx logs every outbound request URL at INFO (userinfo included):
    # noise per write/search and a credential leak for a URL with a
    # password. Keep the libraries at WARNING whatever the root level.
    for noisy in ("httpx", "httpcore"):
        logging.getLogger(noisy).setLevel(max(numeric_level, logging.WARNING))
