"""Health, liveness and the usage report."""

from __future__ import annotations

from dataclasses import asdict

from fastapi import APIRouter, Depends

from hivemind.api.routes.params import API_PREFIX, AppDep, require_admin_key
from hivemind.api.schemas import (
    AgentsMetrics,
    EntriesMetrics,
    FeedbackMetrics,
    FleetsMetrics,
    HealthMetrics,
    HealthOut,
    MetricsOut,
    SearchesMetrics,
)

router = APIRouter(prefix=API_PREFIX)


@router.get("/health", response_model=HealthOut)
async def health() -> HealthOut:
    """Liveness/readiness (public, no auth — SPEC.md §5.1)."""
    return HealthOut(status="ok")


@router.get("/liveness", response_model=HealthOut)
async def liveness() -> HealthOut:
    """Shallow unauthenticated liveness probe (ADR 0019): 200 = the
    process answers. Public by design — k8s liveness probes carry
    no credential. (``/v1/health`` stays the static public 200 it
    is today; the REST surface has no deep DB-probe endpoint in v1.)
    """
    return HealthOut(status="ok")


@router.get("/metrics", response_model=MetricsOut, dependencies=[Depends(require_admin_key)])
async def metrics(app: AppDep) -> MetricsOut:
    """Usage counters (ROADMAP §3.3, Tier 3). Admin-gated: the report
    is operational data (usage volume, fleet writes, trust-level
    distribution, pending-agent count). Makes the SPEC §10
    usage-based triggers measurable (pair with the §1.1 eval harness)."""
    report = await app.metrics_service.usage_report()
    return MetricsOut(
        entries=EntriesMetrics(**asdict(report.entries)),
        fleets=FleetsMetrics(**asdict(report.fleets)),
        agents=AgentsMetrics(**asdict(report.agents)),
        searches=SearchesMetrics(**asdict(report.searches)),
        feedback=FeedbackMetrics(**asdict(report.feedback)),
        health=HealthMetrics(**asdict(report.health)) if report.health is not None else None,
    )
