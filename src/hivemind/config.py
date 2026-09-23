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
import os
from dataclasses import dataclass
from pathlib import Path

from dotenv import dotenv_values
from pydantic import field_validator
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
    # asyncpg pool sizing for the Postgres store (``make_pool`` in
    # store/pool.py already defaults to these exact values; these fields
    # just make that existing constant operator-reachable, per pod, for
    # tuning concurrency against a `max_connections`-constrained org).
    pool_min_size: int = 1
    pool_max_size: int = 10
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
    # environment a real value is validated to (0, 1] (see
    # ``_parse_recency_floor`` below); ADR 0023 gives an explicit
    # spelling for "no floor" (empty string or "none", case-insensitive)
    # that resolves to ``None``, the pre-ADR-0022 unbounded form —
    # closing the gap ADR 0022 originally left (an operator reverting
    # the floor had to approximate it with a negligible value).
    recency_floor: float | None = 0.8
    quality_helpful_weight: float = 0.05
    quality_stale_weight: float = 0.10
    quality_wrong_weight: float = 0.25
    quality_min: float = 0.5
    quality_max: float = 1.2

    @field_validator("recency_floor", mode="before")
    @classmethod
    def _parse_recency_floor(cls, value: object) -> object:
        """Accept an explicit "no floor" spelling from the environment.

        ``None`` is a real, load-bearing value (the pre-ADR-0022
        unbounded recency term, ADR 0023), but every env var arrives as
        a string, and no string used to decode to it — an operator
        reverting the floor had to approximate with a negligible value
        like ``1e-9``. Empty string and the case-insensitive literal
        "none" both resolve to ``None`` here; anything else (including
        out-of-range numbers) is left for pydantic's normal float
        parsing and ``SearchConfig.__post_init__``'s (0, 1] check.
        """
        if isinstance(value, str) and value.strip().lower() in ("", "none"):
            return None
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


# ADR 0024 (unknown-``HIVEMIND_*``-variable rejection). ``Settings`` keeps
# ``extra="ignore"``, so a stale or typo'd ``HIVEMIND_*`` variable does
# nothing — silently. A naive ``extra="forbid"`` on the model would be
# worse: these five variables are deliberately NOT ``Settings`` fields —
# they are read straight out of ``os.environ`` by code that never
# constructs ``Settings`` — and three of them (``RUNNER``, ``HOST``,
# ``PORT``) are set on every pod, so ``extra="forbid"`` would crash-loop
# every container. This is the one place that says "deliberately not a
# setting" for each of them; a variable that is neither here nor a
# ``Settings`` field is a mistake, not a third category.
_ALLOWED_EXTRA_ENV_VARS: dict[str, str] = {
    # entrypoint.sh reads this directly (shell, never Settings) to choose
    # which console script to exec — api / mcp-http / migrate / keys.
    "HIVEMIND_RUNNER": "selects the runner in entrypoint.sh",
    # Listen address for the REST API and the MCP-HTTP runner. Set by the
    # Dockerfile's `ENV HIVEMIND_HOST=0.0.0.0` / the k8s ConfigMap, and
    # read via `os.environ` in api/main.py and mcp/http.py — a bind
    # concern for the process, not a retrieval/storage knob.
    "HIVEMIND_HOST": "REST/MCP-HTTP bind host, read via os.environ in api/main.py and mcp/http.py",
    # Listen port for the same two runners, set by the k8s Deployment env
    # and read via `os.environ` alongside HIVEMIND_HOST.
    "HIVEMIND_PORT": "REST/MCP-HTTP bind port, read via os.environ in api/main.py and mcp/http.py",
    # The per-agent credential for the stdio `hivemind-mcp-pg` runner
    # (ADR 0009), supplied by that agent's own operator config and read
    # via `os.environ` in mcp/server.py. Per-process identity, not a
    # service-wide knob, so it is deliberately outside Settings.
    "HIVEMIND_MCP_KEY": "per-agent hivemind-mcp-pg credential, read via os.environ in mcp/server.py",
    # docker-compose's own host-port mapping for the mcp-http service
    # (`${HIVEMIND_MCP_HTTP_PORT:-8088}:8088`). Consumed entirely by
    # compose's variable substitution; the application never reads it.
    "HIVEMIND_MCP_HTTP_PORT": "docker-compose host-port mapping for mcp-http; the app never reads it",
}


def _known_hivemind_env_vars() -> set[str]:
    """Every ``HIVEMIND_*`` name ``Settings`` itself understands."""
    prefix = Settings.model_config.get("env_prefix", "")
    return {f"{prefix}{name}".upper() for name in Settings.model_fields}


def _reject_unknown_hivemind_env_vars(env_file: str) -> None:
    """Fail loudly on a ``HIVEMIND_*`` variable ``Settings`` would ignore.

    Checked against real process env vars AND the active profile file
    (ADR 0017) — a typo in either currently does nothing (the bug this
    guards against). Runs in ``load_settings()``, the real deployment
    entry point, not on the ``Settings`` model: tests and dev code
    construct ``Settings(...)`` directly with explicit kwargs, and a
    model-level ``extra="forbid"`` would also reject the allowlisted
    operational variables every pod sets (see ``_ALLOWED_EXTRA_ENV_VARS``).
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
    # Match on the part after "HIVEMIND_": every name shares that prefix,
    # which otherwise dominates the similarity ratio and produces
    # confident-looking nonsense suggestions (e.g. HIVEMIND_NONSENSE ~
    # HIVEMIND_RUNNER, ratio 0.75, purely from the shared prefix).
    suffix_to_known = {name.removeprefix("HIVEMIND_"): name for name in known}
    lines = []
    for name in unknown:
        candidates = difflib.get_close_matches(name.removeprefix("HIVEMIND_"), suffix_to_known, n=1)
        hint = f" — did you mean {suffix_to_known[candidates[0]]}?" if candidates else ""
        lines.append(f"  {name}{hint}")
    raise RuntimeError(
        "Unknown HIVEMIND_* environment variable(s) — neither a Settings "
        "field nor on the exemption list in src/hivemind/config.py "
        "(_ALLOWED_EXTRA_ENV_VARS):\n"
        + "\n".join(lines)
        + "\nFix the name, unset the variable, or (if it is genuinely read "
        "outside Settings) add it to _ALLOWED_EXTRA_ENV_VARS with a reason."
    )


def load_settings() -> Settings:
    """Build ``Settings`` for the active environment profile (ADR 0017).

    Reads the ``ENVIRONMENT`` env var at call time and loads the
    matching profile file: ``production`` → ``.env.production``,
    ``staging`` → ``.env.staging``, ``test`` → ``.env.test``, anything
    else (or unset) → ``.env.local``. Real ``HIVEMIND_*`` env vars
    always win over file values; a missing file is silently ignored
    (the Kubernetes / CI posture: values come from env / Secrets).

    Before building ``Settings``, rejects any ``HIVEMIND_*`` variable
    that is neither a ``Settings`` field nor on the ADR 0024 exemption
    list — a typo'd or stale variable is a loud startup failure here,
    never a silent no-op.
    """
    env_file = env_file_for(os.environ.get("ENVIRONMENT"))
    _reject_unknown_hivemind_env_vars(env_file)
    # pydantic-settings' per-instance dotenv override (`_env_file`) is a
    # documented init parameter (runtime-verified) but missing from its
    # mypy stubs — a targeted ignore, not a type hole.
    return Settings(_env_file=env_file)  # type: ignore[call-arg]
