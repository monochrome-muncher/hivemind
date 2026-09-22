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

import os
from dataclasses import dataclass

from pydantic_settings import BaseSettings, SettingsConfigDict

from hivemind.domain.entry import DEFAULT_PREFIX_TOKENS


@dataclass(frozen=True, slots=True)
class SearchConfig:
    """Tunable retrieval parameters (defaults follow SPEC.md §6.2/§6.4)."""

    rrf_k: int = 60
    weight_keyword: float = 0.5
    weight_vector: float = 0.5
    candidate_top_k: int = 20
    default_limit: int = 10
    half_life_days: float = 30.0
    # ADR 0022 / SPEC §6.4: a lower bound on the recency factor, so the
    # one unbounded factor in `entry_score`'s product can no longer
    # dominate RRF's compressed fused range (2.6230x at `rrf_k=60`).
    # 0.8 bounds the recency range at 1/0.8 = 1.25x, which is inside it:
    # match quality is the sort key, recency the tie-break. **This is a
    # band, not a slider** — at 0.9 the term is nearly off and scores
    # *worse* than switching it off, at 1.0 it is off. Do not tune it up
    # (ADR 0022). ``None`` = no floor = the pre-ADR-0022 unbounded form.
    recency_floor: float | None = 0.8
    quality_helpful_weight: float = 0.05
    quality_stale_weight: float = 0.10
    quality_wrong_weight: float = 0.25
    quality_min: float = 0.5
    quality_max: float = 1.2

    def __post_init__(self) -> None:
        if self.recency_floor is not None and not 0.0 < self.recency_floor <= 1.0:
            raise ValueError(f"recency_floor must be in (0, 1] or None, got {self.recency_floor}")

    def quality_kwargs(self) -> dict[str, float]:
        """Keyword args for ``retrieval.scoring.feedback_quality``."""
        return {
            "helpful_weight": self.quality_helpful_weight,
            "stale_weight": self.quality_stale_weight,
            "wrong_weight": self.quality_wrong_weight,
            "min_quality": self.quality_min,
            "max_quality": self.quality_max,
        }


class Settings(BaseSettings):
    """Environment-driven service settings (HIVEMIND_* env vars).

    The class default reads the ``.local`` profile file (``.env.local``)
    when present — the fallback profile (ADR 0017); ``load_settings()``
    selects the active profile from the ``ENVIRONMENT`` env var instead.
    Real environment variables always win over file values, and a
    missing file is silently ignored, so Kubernetes / CI (where the
    values come from env / Secrets and no profile file exists) are
    unaffected.
    """

    model_config = SettingsConfigDict(env_prefix="HIVEMIND_", extra="ignore", env_file=".env.local")

    database_url: str = "postgresql://hivemind:hivemind@localhost:5432/hivemind"
    embedding_endpoint: str = "http://localhost:8001/v1"
    embedding_api_key: str = ""
    embedding_model: str = "text-embedding-3-small"
    # The default when HIVEMIND_EMBEDDING_DIM is unset (ADR 0015). We never
    # assume a 1536-dim default — that is one provider's native dim; 1024
    # is the deploy-time default both OpenAI-compatible endpoints (the
    # ``dimensions`` parameter) and self-hosted Matryoshka servers (vLLM)
    # can serve. The dev Makefile pins 512 for fast local vLLM embedding.
    embedding_dim: int = 1024
    # ADR 0021: the embedded-text body budget, in whitespace-delimited
    # words ("prefix tokens"), NOT a model tokenizer's tokens. Shared
    # with the extractor, which reads the same text (ADR 0016, SPEC §13.1).
    embedding_prefix_tokens: int = DEFAULT_PREFIX_TOKENS
    # Retry budget for transient embedder failures (timeouts, connection
    # errors, 429, 5xx) — ADR 0014; 0 disables retrying.
    embedding_retries: int = 2

    # Entity-extraction extractor (ADR 0016, SPEC §13): an OpenAI-compatible
    # *chat* endpoint, usually a different model/service than the embedding
    # one. An empty endpoint disables extraction (the optional + best-effort
    # stance): entries land with empty `entities`, zero LLM-extraction cost.
    extractor_endpoint: str = ""
    extractor_api_key: str = ""
    extractor_model: str = ""
    extractor_timeout: float = 30.0
    # Retry budget for transient extractor failures (ADR 0014 pattern);
    # 0 disables retrying. A failure never blocks the write (best-effort).
    extractor_retries: int = 2

    # retrieval knobs (mirror SearchConfig defaults)
    rrf_k: int = 60
    weight_keyword: float = 0.5
    weight_vector: float = 0.5
    candidate_top_k: int = 20
    default_limit: int = 10
    half_life_days: float = 30.0
    # ADR 0022. See SearchConfig.recency_floor — a band, not a slider;
    # raising it toward 1.0 removes the recency term. From the
    # environment the settable range is (0, 1]: there is no spelling for
    # "no floor" (the unbounded form is the defect ADR 0022 fixes), and
    # a negligible floor (1e-9 = 30 half-lives of range) is its
    # practical equivalent if an operator ever needs it back.
    recency_floor: float | None = 0.8
    quality_helpful_weight: float = 0.05
    quality_stale_weight: float = 0.10
    quality_wrong_weight: float = 0.25
    quality_min: float = 0.5
    quality_max: float = 1.2

    def search_config(self) -> SearchConfig:
        return SearchConfig(
            rrf_k=self.rrf_k,
            weight_keyword=self.weight_keyword,
            weight_vector=self.weight_vector,
            candidate_top_k=self.candidate_top_k,
            default_limit=self.default_limit,
            half_life_days=self.half_life_days,
            recency_floor=self.recency_floor,
            quality_helpful_weight=self.quality_helpful_weight,
            quality_stale_weight=self.quality_stale_weight,
            quality_wrong_weight=self.quality_wrong_weight,
            quality_min=self.quality_min,
            quality_max=self.quality_max,
        )


# Environment profile files (ADR 0017): the ``ENVIRONMENT`` env var
# selects which per-environment dotenv file ``load_settings`` reads.
# Real env vars always win over file values, and a missing file is
# silently ignored — so Kubernetes / CI (no profile file present)
# is unaffected.
_ENV_FILES = {
    "production": ".env.production",
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


def load_settings() -> Settings:
    """Build ``Settings`` for the active environment profile (ADR 0017).

    Reads the ``ENVIRONMENT`` env var at call time and loads the
    matching profile file: ``production`` → ``.env.production``,
    ``staging`` → ``.env.staging``, ``test`` → ``.env.test``, anything
    else (or unset) → ``.env.local``. Real ``HIVEMIND_*`` env vars
    always win over file values; a missing file is silently ignored
    (the Kubernetes / CI posture: values come from env / Secrets).
    """
    # pydantic-settings' per-instance dotenv override (`_env_file`) is a
    # documented init parameter (runtime-verified) but missing from its
    # mypy stubs — a targeted ignore, not a type hole.
    return Settings(_env_file=env_file_for(os.environ.get("ENVIRONMENT")))  # type: ignore[call-arg]
