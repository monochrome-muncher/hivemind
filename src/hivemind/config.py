"""Configuration knobs (SPEC §6.2, §6.4, §7; ADR 0006).

``SearchConfig`` is pure values for the search pipeline; ``Settings`` loads the same knobs from
HIVEMIND_* env vars.
"""

from __future__ import annotations

import difflib
import ipaddress
import logging
import math
import os
from dataclasses import dataclass
from pathlib import Path
from urllib.parse import urlsplit

from dotenv import dotenv_values
from pydantic import Field, field_validator, model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

from hivemind.domain.entry import DEFAULT_PREFIX_TOKENS
from hivemind.domain.recency import RecencyHint
from hivemind.domain.validation import check_query_instruction
from hivemind.providers import clean_api_key, clean_endpoint
from hivemind.retrieval.scoring import CONFIRMED_FLAG_SCALE

logger = logging.getLogger(__name__)

# The HIVEMIND_STRICT_ENV spellings that mean "on" (ADR 0032).
_TRUTHY = frozenset({"1", "true", "yes", "on"})


# Floor under ``prefer_recent`` (ADR 0073; docs/retrieval-experiments.md).
PREFER_RECENT_FLOOR: float | None = 0.5
# Vector similarity cut-off (ADR 0100), measured for Qwen3-Embedding: re-measure
# (``make measure-threshold``) for another embedder. Empty env value = off.
DEFAULT_VECTOR_MIN_SIMILARITY: float = 0.45
# Task line before the search query (ADR 0097); tests/eval/query_instruction.py measures this text.
DEFAULT_QUERY_INSTRUCTION = (
    "Given an agent's task or question, retrieve shared memory entries that answer it"
)


@dataclass(frozen=True, slots=True)
class RecencyTerms:
    """The age inputs of ``entry_score`` for one entry (ADR 0073)."""

    half_life_days: float
    recency_floor: float | None


@dataclass(frozen=True, slots=True)
class SearchConfig:
    """Tunable retrieval parameters (defaults follow SPEC.md §6.2/§6.4)."""

    rrf_k: int = 60
    weight_keyword: float = 0.5
    weight_vector: float = 0.5
    candidate_top_k: int = 20
    default_limit: int = 10
    half_life_days: float = 30.0
    # Floor on the recency factor (ADR 0022): a band, not a slider (0.9 scores worse than off,
    # 1.0 is off), so do not tune it up. ``None`` = no floor.
    recency_floor: float | None = 0.8
    # Vector stream cut-off (ADR 0062, 0100): model-specific, in [0, 1). ``None`` = off (floor of 0,
    # ADR 0061).
    vector_min_similarity: float | None = DEFAULT_VECTOR_MIN_SIMILARITY
    # Version grouping threshold (ADR 0074): off when ``None``, else in (0, 1); model-specific.
    version_group_min_similarity: float | None = None
    # Feedback quality (ADR 0063): each weight is where quality heads when every report gives
    # that verdict; the prior counts imaginary neutral reports (> 0).
    quality_helpful_weight: float = 0.2
    quality_stale_weight: float = 0.4
    quality_wrong_weight: float = 1.0
    quality_prior: float = 2.0
    quality_min: float = 0.5
    quality_max: float = 1.2
    # Ceiling on all social signals together (ADR 0070): finite and >= 1, since below 1 it would
    # penalise unrated entries.
    social_max: float = 1.2
    # Relevance against novelty in ``explore`` MMR re-ordering (ADR 0078): in (0, 1], 1 is plain
    # relevance order.
    explore_lambda: float = 0.7
    # Off: a ``recency`` hint is accepted and ignored (ADR 0073, 0101).
    recency_hint_enabled: bool = True
    # Floor under ``prefer_recent`` (ADR 0073), or ``None`` for none. Not an env setting; tests
    # and the measurement harness vary it.
    prefer_recent_floor: float | None = PREFER_RECENT_FLOOR
    # Importance factor spans 0.9..1.1 (ADR 0088); false restores 0.6..1.0.
    narrow_importance: bool = True
    # A confirmed entry's stale and wrong flags count at half weight (ADR 0106); false counts them
    # in full.
    confirmation_halves_flags: bool = True
    # Task line sent as ``Instruct: {task}\nQuery:{query}`` on the vector stream only (ADR 0097);
    # entries are never embedded with it. Empty = off.
    query_instruction: str = DEFAULT_QUERY_INSTRUCTION

    def __post_init__(self) -> None:
        # Reject bad values when the config is built, not per request (ZeroDivisionError, negative
        # LIMIT, NaN scores).
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
            # Zero or negative divides by zero or inverts decay; inf silently turns decay off.
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
        """The vector stream's text for ``query`` (ADR 0097); the keyword stream gets the raw
        query."""
        if not self.query_instruction:
            return query
        return f"Instruct: {self.query_instruction}\nQuery:{query}"

    def recency_terms(self, hint: RecencyHint) -> RecencyTerms:
        """How age enters an entry's score under ``hint`` (ADR 0073); ``DEFAULT`` for every hint
        when disabled."""
        if self.recency_hint_enabled:
            if hint is RecencyHint.IGNORE_AGE:
                # 0.5 ** (age / inf) is exactly 1.0 for every finite age.
                return RecencyTerms(math.inf, None)
            if hint is RecencyHint.PREFER_RECENT:
                # Never weaker than the default: a deployment with a lower
                # floor, or none, keeps it.
                floor = self.prefer_recent_floor
                if self.recency_floor is None:
                    floor = None
                elif floor is not None:
                    floor = min(floor, self.recency_floor)
                return RecencyTerms(self.half_life_days, floor)
        return RecencyTerms(self.half_life_days, self.recency_floor)

    def applied_hint(self, hint: RecencyHint) -> RecencyHint:
        """The hint ``recency_terms`` honours: ``DEFAULT`` while the setting is off."""
        return hint if self.recency_hint_enabled else RecencyHint.DEFAULT

    def quality_kwargs(self, *, confirmed: bool) -> dict[str, float]:
        """Keyword args for ``retrieval.scoring.feedback_quality``; ``confirmed`` is required so a
        caller must say (ADR 0106)."""
        halve = confirmed and self.confirmation_halves_flags
        return {
            "helpful_weight": self.quality_helpful_weight,
            "stale_weight": self.quality_stale_weight,
            "wrong_weight": self.quality_wrong_weight,
            "prior": self.quality_prior,
            "min_quality": self.quality_min,
            "max_quality": self.quality_max,
            "flag_scale": CONFIRMED_FLAG_SCALE if halve else 1.0,
        }


class Settings(BaseSettings):
    """Service settings from HIVEMIND_* env vars; ``load_settings()`` picks the profile file (ADR
    0017)."""

    model_config = SettingsConfigDict(
        env_prefix="HIVEMIND_",
        extra="ignore",
        env_file=".env.local",
        # A validation error must never echo a value back (an API key).
        hide_input_in_errors=True,
    )

    # ``repr=False`` on every secret-bearing field, so a stray ``%r`` cannot print a password or
    # key.
    database_url: str = Field(
        default="postgresql://hivemind:hivemind@localhost:5432/hivemind", repr=False
    )
    # Direct (non-pooled) DSN for `hivemind-migrate`: its advisory lock (ADR 0020) cannot be held
    # through transaction-mode PgBouncer. Empty = use `database_url`.
    migrate_database_url: str = Field(default="", repr=False)
    # Root logger level (DEBUG/INFO/WARNING/ERROR/CRITICAL, case-insensitive).
    log_level: str = "INFO"
    # `hivemind-admin` only (ADR 0029): the API base URL it proxies to (empty is a startup error)
    # and a PEM file for an https API URL (empty = system trust store).
    admin_api_url: str = ""
    admin_api_ca_bundle: str = ""
    # `hivemind-portal` only (ADR 0095): same meaning as the admin pair.
    portal_api_url: str = ""
    portal_api_ca_bundle: str = ""
    # MCP-HTTP DNS-rebinding guard (ADR 0042): comma-separated Host (``name`` or ``name:*``) and
    # Origin values. Empty hosts = SDK default (loopback bind only), which keeps an ingress-fronted
    # 0.0.0.0 deployment working.
    mcp_allowed_hosts: str = ""
    mcp_allowed_origins: str = ""
    # True makes an unknown HIVEMIND_* variable a startup failure (ADR 0032); read by
    # load_settings() BEFORE Settings is built.
    strict_env: bool = False
    # True puts a dev-mode credential (not an admin, org or active agent key) at trust level 0
    # (ADR 0082). Production sets it.
    require_stored_keys: bool = False
    # Days without key use before the admin list shows an agent as idle (ADR 0110); display only.
    idle_agent_days: int = Field(default=30, ge=1)
    # Serve an unauthenticated GET /metrics from hivemind-mcp-http (ADR 0104). Off by default:
    # compose publishes the MCP port on every interface. Off = 404, nothing collected.
    mcp_metrics_enabled: bool = False
    # asyncpg pool size, per pod (same defaults as ``make_pool``).
    pool_min_size: int = Field(default=1, ge=0)
    pool_max_size: int = Field(default=10, ge=1)
    # Bounds on app-pool Postgres calls, never `hivemind-migrate` (CREATE INDEX CONCURRENTLY must
    # run unbounded). Seconds; 0 disables. `pool_statement_timeout_ms` is a server-side startup
    # parameter that transaction-mode PgBouncer rejects or ignores: keep 0 unless connecting
    # directly.
    pool_command_timeout: float = Field(default=30.0, ge=0, allow_inf_nan=False)
    pool_statement_timeout_ms: int = Field(default=0, ge=0)
    pool_acquire_timeout: float = Field(default=10.0, ge=0, allow_inf_nan=False)
    pool_connect_timeout: float = Field(default=10.0, ge=0, allow_inf_nan=False)
    embedding_endpoint: str = "http://localhost:8001/v1"
    embedding_api_key: str = Field(default="", repr=False)
    embedding_model: str = "text-embedding-3-small"
    # Per-request timeout (seconds). Feeds the k8s `terminationGracePeriodSeconds` arithmetic
    # (DEPLOY.md §5): raise both together.
    embedding_timeout: float = Field(default=10.0, gt=0, allow_inf_nan=False)
    # Wall-clock budget for one embed call, retries included (ADR 0041). Unset =
    # ``providers.default_deadline``. Below the timeout is a startup error.
    embedding_deadline: float | None = Field(default=None, gt=0, allow_inf_nan=False)
    # Default 1024 (ADR 0015); the dev Makefile pins 512.
    embedding_dim: int = Field(default=1024, ge=1, le=2000)  # pgvector HNSW cap
    # Embedded-text body budget in words, not tokens (ADR 0021); shared with the extractor (SPEC
    # §13.1).
    embedding_prefix_tokens: int = Field(default=DEFAULT_PREFIX_TOKENS, ge=0)  # 0 = summary only
    # Retries for transient embedder failures (ADR 0014); 0 disables.
    embedding_retries: int = Field(default=2, ge=0)

    # Entity extractor (ADR 0016, SPEC §13): an OpenAI-compatible chat endpoint. Empty disables
    # extraction (entries get empty `entities`).
    extractor_endpoint: str = ""
    extractor_api_key: str = Field(default="", repr=False)
    extractor_model: str = ""
    extractor_timeout: float = Field(default=30.0, gt=0, allow_inf_nan=False)
    # Wall-clock budget for one extraction, retries included.
    extractor_deadline: float | None = Field(default=None, gt=0, allow_inf_nan=False)
    # Retries for transient extractor failures (ADR 0014); 0 disables. A failure never blocks the
    # write.
    extractor_retries: int = Field(default=2, ge=0)

    # Seconds MetricsService reuses the entry-count scan (ADR 0069); 0 = every report. Counts can
    # lag this long.
    metrics_entry_counts_ttl: float = Field(default=300.0, ge=0, allow_inf_nan=False)

    # Retrieval knobs mirroring SearchConfig, whose __post_init__ holds the value rules.
    rrf_k: int = 60
    weight_keyword: float = 0.5
    weight_vector: float = 0.5
    candidate_top_k: int = 20
    default_limit: int = 10
    half_life_days: float = 30.0
    # See SearchConfig (ADR 0022). Empty or "none" (case-insensitive) = no floor (ADR 0023).
    recency_floor: float | None = 0.8
    # See SearchConfig (ADR 0062, 0100). Unset = the default; empty or "none" = off.
    vector_min_similarity: float | None = DEFAULT_VECTOR_MIN_SIMILARITY
    # See SearchConfig (ADR 0074). Empty or "none" = off.
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
    # See SearchConfig.recency_hint_enabled (ADR 0073).
    recency_hint: bool = True
    # See SearchConfig.narrow_importance (ADR 0088).
    narrow_importance: bool = True
    # See SearchConfig.confirmation_halves_flags (ADR 0106).
    confirmation_halves_flags: bool = True
    # See SearchConfig (ADR 0097). Empty = off; whitespace is stripped.
    query_instruction: str = DEFAULT_QUERY_INSTRUCTION

    @field_validator("query_instruction")
    @classmethod
    def _clean_query_instruction(cls, value: str) -> str:
        return value.strip()

    @field_validator("embedding_api_key", "extractor_api_key")
    @classmethod
    def _clean_api_key(cls, value: str) -> str:
        """Strip whitespace (a k8s Secret value ends in a newline, which httpx rejects) and reject
        control characters."""
        return clean_api_key(value)

    @field_validator("embedding_endpoint", "extractor_endpoint")
    @classmethod
    def _clean_endpoint(cls, value: str) -> str:
        """Strip whitespace, reject inner whitespace. Never echoes the value (it may carry
        userinfo)."""
        return clean_endpoint(value)

    @property
    def migration_dsn(self) -> str:
        """The DSN ``hivemind-migrate`` uses (ROADMAP 3.13)."""
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
        """Map empty / "none" to ``None``: no floor (ADR 0023), no threshold (ADR 0062)."""
        if isinstance(value, str) and value.strip().lower() in ("", "none"):
            return None
        return value

    @field_validator("strict_env", mode="before")
    @classmethod
    def _parse_strict_env(cls, value: object) -> object:
        """Parse as the ``load_settings`` pre-check does (ADR 0032), so the two readings agree."""
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
            recency_hint_enabled=self.recency_hint,
            narrow_importance=self.narrow_importance,
            confirmation_halves_flags=self.confirmation_halves_flags,
            query_instruction=self.query_instruction,
        )


def redact_url(url: str) -> str:
    """``url`` without userinfo, for logging. Blunt: drops everything up to the last ``@``, even in
    a path."""
    scheme, sep, rest = url.partition("://")
    if not sep:
        scheme, rest = "", url
    if "@" in rest:
        rest = rest.rsplit("@", 1)[1]
    return f"{scheme}://{rest}" if sep else rest


def endpoint_label(url: str) -> str:
    """Scheme and host[:port] only, for error messages: never path, query or userinfo."""
    cleaned = redact_url(url)
    scheme, sep, rest = cleaned.partition("://")
    if not sep:
        scheme, rest = "", cleaned
    host = rest.split("/", 1)[0].split("?", 1)[0].split("#", 1)[0]
    return f"{scheme}://{host}" if sep else host


def _is_loopback(host: str) -> bool:
    """``localhost`` or a loopback address (``127.example.com`` is remote)."""
    if host == "localhost":
        return True
    try:
        return ipaddress.ip_address(host.strip("[]")).is_loopback
    except ValueError:
        return False


# Environment profile files (ADR 0017), selected by ``ENVIRONMENT``.
_ENV_FILES = {
    "production": ".env.production",
    "prod": ".env.production",
    "staging": ".env.staging",
    "test": ".env.test",
}
_DEFAULT_ENV_FILE = ".env.local"


def env_file_for(environment: str | None) -> str:
    """The profile file for an ``ENVIRONMENT`` value (ADR 0017); anything else is ``.env.local``."""
    key = (environment or "").strip().lower()
    return _ENV_FILES.get(key, _DEFAULT_ENV_FILE)


def bind_host() -> str:
    """The ``HIVEMIND_HOST`` bind host every HTTP runner reads (default all interfaces)."""
    return os.environ.get("HIVEMIND_HOST", "0.0.0.0")


def bind_port(default: int) -> int:
    """The ``HIVEMIND_PORT`` bind port; ``default`` is the runner's own.

    A non-numeric value raises ``ValueError`` from ``int()``.
    """
    return int(os.environ.get("HIVEMIND_PORT", str(default)))


# ``HIVEMIND_*`` variables that are not ``Settings`` fields and must not be reported as unknown
# (ADR 0024, 0032). ``extra="forbid"`` is no option: RUNNER, HOST and PORT are set on every pod.
_ALLOWED_EXTRA_ENV_VARS: dict[str, str] = {
    "HIVEMIND_RUNNER": "selects the runner in entrypoint.sh",
    "HIVEMIND_HOST": ("REST/MCP-HTTP/admin/portal bind host, read by bind_host()"),
    "HIVEMIND_PORT": ("REST/MCP-HTTP/admin/portal bind port, read by bind_port()"),
    # Per-process identity (ADR 0009), so deliberately outside Settings.
    "HIVEMIND_MCP_KEY": "per-agent hivemind-mcp-pg credential, read via os.environ in mcp/runners.py",
    "HIVEMIND_MCP_HTTP_PORT": "mcp-http port for docker compose and `make mcp-http-dev`; the app never reads it",
}

# Removed settings: ignored with one warning, never an error even under strict env (ADR 0103).
_RETIRED_ENV_VARS: dict[str, str] = {
    "HIVEMIND_RECENCY_FROM_CONFIRMATION": "ADR 0071, removed by ADR 0103",
    "HIVEMIND_RECENCY_BY_KIND": "ADR 0072, removed by ADR 0103",
}


def _known_hivemind_env_vars() -> set[str]:
    prefix = Settings.model_config.get("env_prefix", "")
    return {f"{prefix}{name}".upper() for name in Settings.model_fields}


def _strict_env(env_file: str) -> bool:
    """Whether ``HIVEMIND_STRICT_ENV`` is on (ADR 0032); read by hand because it runs before
    ``Settings`` exists."""
    raw = os.environ.get("HIVEMIND_STRICT_ENV")
    if raw is None:
        file_path = Path(env_file)
        if file_path.is_file():
            raw = dotenv_values(file_path).get("HIVEMIND_STRICT_ENV")
    return (raw or "").strip().lower() in _TRUTHY


def _check_unknown_hivemind_env_vars(env_file: str) -> None:
    """Warn about a ``HIVEMIND_*`` variable ``Settings`` would ignore, or raise under strict env
    (ADR 0024, 0032).

    Runs in ``load_settings()``, not on the model, so tests building ``Settings(...)`` are
    unaffected.
    """
    known = _known_hivemind_env_vars() | _ALLOWED_EXTRA_ENV_VARS.keys()
    present = {name for name in os.environ if name.startswith("HIVEMIND_")}
    file_path = Path(env_file)
    if file_path.is_file():
        present |= {
            name for name in dotenv_values(file_path) if name and name.startswith("HIVEMIND_")
        }
    retired = sorted(present & _RETIRED_ENV_VARS.keys())
    if retired:
        logger.warning(
            "Removed HIVEMIND_* environment variable(s) are ignored: %s. "
            "Delete them from the configuration (ADR 0103).",
            ", ".join(retired),
        )
    unknown = sorted(present - known - _RETIRED_ENV_VARS.keys())
    if not unknown:
        return
    # Match without the shared prefix, which would dominate the similarity ratio.
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
    """Build ``Settings`` from the ``ENVIRONMENT`` profile (ADR 0017); real env vars win, a missing
    file is ignored."""
    env_file = env_file_for(os.environ.get("ENVIRONMENT"))
    _check_unknown_hivemind_env_vars(env_file)
    # `_env_file` is a documented init parameter missing from the mypy stubs.
    return Settings(_env_file=env_file)  # type: ignore[call-arg]


def configure_logging(level: str = "INFO") -> None:
    """Configure the root logger at process startup; an invalid ``level`` raises instead of falling
    back to WARNING."""
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
    # basicConfig is a no-op when a handler already exists; the level must apply regardless.
    logging.getLogger().setLevel(numeric_level)
    # httpx logs every request URL at INFO, userinfo included (a credential leak): keep at WARNING.
    for noisy in ("httpx", "httpcore"):
        logging.getLogger(noisy).setLevel(max(numeric_level, logging.WARNING))
