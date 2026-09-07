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

from dataclasses import dataclass

from pydantic_settings import BaseSettings, SettingsConfigDict


@dataclass(frozen=True, slots=True)
class SearchConfig:
    """Tunable retrieval parameters (defaults follow SPEC.md §6.2/§6.4)."""

    rrf_k: int = 60
    weight_keyword: float = 0.5
    weight_vector: float = 0.5
    candidate_top_k: int = 20
    default_limit: int = 10
    half_life_days: float = 30.0
    quality_helpful_weight: float = 0.05
    quality_stale_weight: float = 0.10
    quality_wrong_weight: float = 0.25
    quality_min: float = 0.5
    quality_max: float = 1.2

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
    """Environment-driven service settings (HIVEMIND_* env vars)."""

    model_config = SettingsConfigDict(env_prefix="HIVEMIND_", extra="ignore")

    database_url: str = "postgresql://hivemind:hivemind@localhost:5432/hivemind"
    embedding_endpoint: str = "http://localhost:8001/v1"
    embedding_api_key: str = ""
    embedding_model: str = "text-embedding-3-small"
    embedding_dim: int = 1536
    embedding_prefix_chars: int = 2048

    # retrieval knobs (mirror SearchConfig defaults)
    rrf_k: int = 60
    weight_keyword: float = 0.5
    weight_vector: float = 0.5
    candidate_top_k: int = 20
    default_limit: int = 10
    half_life_days: float = 30.0
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
            quality_helpful_weight=self.quality_helpful_weight,
            quality_stale_weight=self.quality_stale_weight,
            quality_wrong_weight=self.quality_wrong_weight,
            quality_min=self.quality_min,
            quality_max=self.quality_max,
        )
