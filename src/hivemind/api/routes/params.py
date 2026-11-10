"""Dependency aliases and annotated parameter types shared by the route modules."""

from __future__ import annotations

from typing import Annotated

from fastapi import Depends, Path, Query
from pydantic import AfterValidator

from hivemind.api.deps import HivemindApp, get_app, get_prometheus, require_credential
from hivemind.api.prometheus import PrometheusMetrics
from hivemind.domain.validation import MAX_ID_CHARS, MAX_IDENTITY_CHARS, check_no_nul
from hivemind.ports import Credential

API_PREFIX = "/v1"

CredentialDep = Annotated[Credential, Depends(require_credential)]
AppDep = Annotated[HivemindApp, Depends(get_app)]
PrometheusDep = Annotated[PrometheusMetrics, Depends(get_prometheus)]


def _no_nul(value: str | None) -> str | None:
    """A path/query string reaches Postgres as a bind parameter: no U+0000
    (ADR 0040). A ``ValueError`` here is a FastAPI 422."""
    return check_no_nul(value, "value") if value is not None else None


# Names of stored records are NUL-checked only (ADR 0040), with no length cap, so a legacy agent
# stays manageable.
NameParam = Annotated[str, Path(), AfterValidator(_no_nul)]
ActorQuery = Annotated[str | None, Query(max_length=MAX_IDENTITY_CHARS), AfterValidator(_no_nul)]
IdQuery = Annotated[str | None, Query(max_length=MAX_ID_CHARS), AfterValidator(_no_nul)]
