"""Version pairs and same-system distractors for the grouping cut-off (ADR 0074).

For each domain of ``domains.py``: six pairs of an older and a newer
statement of one fact whose value changed (nobody linked them), and six
same-system distractors, one per pair: an entry about the same service or
process that states a *different* setting, so it is not a version of the
fact. A good cut-off groups the pairs and leaves the distractors alone.

Each pair has two queries. ``query`` asks about the fact in the words of the
older statement (so the old entry often has the higher lexical overlap, the
case grouping exists for) and the newer entry is the answer. ``old_query``
asks for the older statement exactly, value included, and the older entry
is the answer. Entry dates are set by ``versions.py`` (older long ago,
newer recently).
"""

from __future__ import annotations

from dataclasses import dataclass

from tests.eval.domains import CODE, DATA, FINANCE, OPS, RESEARCH, Domain

# (kind, summary, tags, body)
Entry = tuple[str, str, tuple[str, ...], str | None]


@dataclass(frozen=True, slots=True)
class VersionPair:
    old: Entry
    new: Entry
    query: str  # about the fact, in the older statement's words; answer: new
    old_query: str  # the older statement, value included; answer: old
    distractor: Entry  # same system, a different setting


@dataclass(frozen=True, slots=True)
class VersionDomain:
    domain: Domain
    pairs: tuple[VersionPair, ...]

    @property
    def name(self) -> str:
        return self.domain.name


def _f(summary: str, *tags: str, body: str | None = None) -> Entry:
    return ("fact", summary, tags, body)


DATA_VERSIONS = VersionDomain(
    DATA,
    (
        VersionPair(
            _f("Drift monitor samples 10,000 rows per feature each night", "monitoring", "drift"),
            _f(
                "Drift monitor now samples 50,000 rows per feature; 10k was too noisy for sparse columns",
                "monitoring",
                "drift",
            ),
            "how many rows does the drift monitor sample per feature each night",
            "drift monitor samples 10,000 rows per feature",
            _f(
                "Drift monitor alerts when the population stability index exceeds 0.25",
                "monitoring",
            ),
        ),
        VersionPair(
            _f(
                "Confidence intervals in the pricing study use 1,000 bootstrap resamples", "pricing"
            ),
            _f(
                "Pricing study intervals moved to 10,000 bootstrap resamples after the first "
                "runs shifted between reruns",
                "pricing",
                body="Fixed seed per run; the interval width changed by less than 1 percent.",
            ),
            "bootstrap resamples for pricing study confidence intervals",
            "pricing study confidence intervals 1,000 bootstrap resamples",
            _f("The pricing study bootstrap is stratified by customer segment", "pricing"),
        ),
        VersionPair(
            _f("Cohort reports hide groups smaller than 20 users", "privacy", "cohorts"),
            _f("Minimum cohort size in reports is 50 users since the privacy review", "privacy"),
            "smallest group size shown in cohort reports",
            "cohort reports hide groups smaller than 20 users",
            _f("Cohort reports are bucketed by signup week, not signup day", "cohorts"),
        ),
        VersionPair(
            _f("The signup forecast covers the next 8 weeks", "forecasting", "signups"),
            _f(
                "Signup forecast now runs 26 weeks ahead for the finance planning cycle",
                "forecasting",
                body="The weekly refit is unchanged; only the horizon grew.",
            ),
            "how far ahead does the signup forecast cover in weeks",
            "signup forecast covers the next 8 weeks",
            _f(
                "The signup forecast is refit every Monday on the trailing three years",
                "forecasting",
            ),
        ),
        VersionPair(
            _f("Recommender holdout group is 2 percent of users", "recommender", "experiments"),
            _f(
                "Holdout for the recommender raised to 5 percent so lift is measurable "
                "within two weeks",
                "recommender",
            ),
            "what share of users is in the recommender holdout group",
            "recommender holdout group is 2 percent of users",
            _f("Recommender holdout membership is assigned by hashed user id", "recommender"),
        ),
        VersionPair(
            _f("Executive dashboard tiles are cached for 15 minutes", "dashboards", "cache"),
            _f(
                "Executive dashboard cache lifetime cut to 2 minutes after stale launch-day "
                "numbers were reported",
                "dashboards",
            ),
            "how long are executive dashboard tiles cached",
            "executive dashboard tiles are cached for 15 minutes",
            _f("Executive dashboard tiles show the cache age in the footer", "dashboards"),
        ),
    ),
)

CODE_VERSIONS = VersionDomain(
    CODE,
    (
        VersionPair(
            _f("The orders client times out after 5 seconds", "orders", "http"),
            _f(
                "Orders client timeout raised to 30 seconds because bulk exports were being cut off",
                "orders",
            ),
            "orders client timeout in seconds",
            "orders client times out after 5 seconds",
            _f("The orders client retries three times with jittered backoff", "orders", "http"),
        ),
        VersionPair(
            _f("The API connection pool size is 20", "database", "pool"),
            _f(
                "API database pool raised to 90 on 2026-08-14 after saturation during the sale",
                "database",
                body="Postgres max_connections was raised to match.",
            ),
            "API connection pool size",
            "API connection pool size is 20",
            _f(
                "The API pool recycles connections after 30 minutes of lifetime", "database", "pool"
            ),
        ),
        VersionPair(
            _f("Public API rate limit is 100 requests per minute per key", "api", "limits"),
            _f("Per-key rate limit lifted to 600 requests a minute for the partner tier", "api"),
            "public API rate limit requests per minute per key",
            "public API rate limit is 100 requests per minute per key",
            _f("Rate limited responses carry a Retry-After header", "api", "limits"),
        ),
        VersionPair(
            _f("CI build artifacts are kept for 7 days", "ci", "retention"),
            _f(
                "Artifact retention in CI extended to 30 days so release candidates stay reproducible",
                "ci",
            ),
            "how many days are CI build artifacts kept",
            "CI build artifacts are kept for 7 days",
            _f("CI artifacts are uploaded compressed with zstd", "ci"),
        ),
        VersionPair(
            _f("The upload endpoint accepts files up to 10 MB", "uploads", "limits"),
            _f(
                "Upload limit is now 50 MB; the gateway and the service both enforce it",
                "uploads",
                body="Larger files go through the resumable upload path instead.",
            ),
            "maximum file size the upload endpoint accepts",
            "upload endpoint accepts files up to 10 MB",
            _f("The upload endpoint scans files with ClamAV before storing them", "uploads"),
        ),
        VersionPair(
            _f("Thumbnail workers run 4 threads per pod", "thumbnails", "workers"),
            _f(
                "Thumbnail worker threads raised to 12 per pod once the memory leak was fixed",
                "thumbnails",
            ),
            "how many threads do the thumbnail workers run per pod",
            "thumbnail workers run 4 threads per pod",
            _f("Thumbnail output is capped at 2048 pixels on the long edge", "thumbnails"),
        ),
    ),
)

OPS_VERSIONS = VersionDomain(
    OPS,
    (
        VersionPair(
            _f("Application logs are kept for 14 days in Loki", "logging", "loki"),
            _f("Loki retention extended to 45 days for the audit requirement", "logging", "loki"),
            "how long are application logs kept in Loki",
            "application logs are kept for 14 days in Loki",
            _f("Loki compacts chunks every 10 minutes", "logging", "loki"),
        ),
        VersionPair(
            _f("The edge nginx allows 1024 worker connections", "nginx", "edge"),
            _f(
                "Edge nginx worker_connections raised to 8192 after the sale-day connection errors",
                "nginx",
                body="Open file limit for the service was raised in the same change.",
            ),
            "edge nginx worker connections limit",
            "edge nginx allows 1024 worker connections",
            _f("The edge nginx logs upstream response time in the access log", "nginx", "edge"),
        ),
        VersionPair(
            _f("etcd snapshots are taken every 6 hours", "etcd", "backup"),
            _f(
                "etcd snapshot interval shortened to 30 minutes after the control-plane scare",
                "etcd",
            ),
            "how often are etcd snapshots taken",
            "etcd snapshots are taken every 6 hours",
            _f("etcd snapshots are copied to the cold bucket with object lock", "etcd", "backup"),
        ),
        VersionPair(
            _f("Disk usage alerts fire at 80 percent", "alerting", "disk"),
            _f(
                "Disk usage thresholds moved to 90 percent warning and 95 percent page",
                "alerting",
                "disk",
            ),
            "at what percent disk usage do the alerts fire",
            "disk usage alerts fire at 80 percent",
            _f("Disk usage alerts ignore the tmpfs mounts", "alerting", "disk"),
        ),
        VersionPair(
            _f("PgBouncer default_pool_size is 20 per database", "pgbouncer", "postgres"),
            _f(
                "PgBouncer pool raised to 90 per database for the migration weekend",
                "pgbouncer",
                body="Revisit after the cutover; server connections were the bottleneck.",
            ),
            "PgBouncer default pool size per database",
            "PgBouncer default_pool_size is 20 per database",
            _f("PgBouncer runs in transaction mode for the API users", "pgbouncer", "postgres"),
        ),
        VersionPair(
            _f("Bastion sessions expire after 8 hours", "bastion", "ssh"),
            _f(
                "Bastion session lifetime cut to 1 hour; use the refresh command for longer work",
                "bastion",
            ),
            "how long do bastion sessions last before they expire",
            "bastion sessions expire after 8 hours",
            _f(
                "Bastion sessions are recorded and the recordings kept for 90 days",
                "bastion",
                "ssh",
            ),
        ),
    ),
)

RESEARCH_VERSIONS = VersionDomain(
    RESEARCH,
    (
        VersionPair(
            _f("The transport attitudes survey has 800 respondents", "survey", "transport"),
            _f(
                "The transport attitudes survey was topped up to 1,600 respondents after the "
                "second wave",
                "survey",
                "transport",
            ),
            "how many respondents does the transport attitudes survey have",
            "transport attitudes survey has 800 respondents",
            _f("The transport survey is fielded online in four languages", "survey", "transport"),
        ),
        VersionPair(
            _f("Subjects get 24 hours to reply before publication", "right-of-reply", "ethics"),
            _f("The right of reply window is extended to 72 hours for investigations", "ethics"),
            "how many hours do subjects get to reply before publication",
            "subjects get 24 hours to reply before publication",
            _f("Replies are published in full next to the story, never cut", "right-of-reply"),
        ),
        VersionPair(
            _f("Intercoder agreement target is Cohen's kappa 0.6", "coding", "reliability"),
            _f(
                "Kappa target for the content analysis raised to 0.8 after the pilot",
                "coding",
                body="Categories with kappa below 0.8 are merged or redefined before the main run.",
            ),
            "intercoder agreement target Cohen's kappa",
            "intercoder agreement target is Cohen's kappa 0.6",
            _f("Coding disagreements are resolved by a third coder", "coding", "reliability"),
        ),
        VersionPair(
            _f("Fact checks are published within 5 days of a claim", "fact-check", "workflow"),
            _f("Fact-check turnaround cut to 48 hours during the campaign", "fact-check"),
            "how soon are fact checks published after a claim",
            "fact checks are published within 5 days of a claim",
            _f("Fact checks cite the primary source link in the first paragraph", "fact-check"),
        ),
        VersionPair(
            _f("Interview participants receive a 20 euro voucher", "interviews", "incentives"),
            _f(
                "Participant voucher raised to 40 euros because of low response in rural areas",
                "interviews",
            ),
            "voucher value for interview participants",
            "interview participants receive a 20 euro voucher",
            _f(
                "Interview participants sign the consent form before the session starts",
                "interviews",
            ),
        ),
        VersionPair(
            _f("Raw field notes are archived for 3 years", "archive", "fieldwork"),
            _f(
                "Raw field notes archive retention extended to 10 years under the funder rules",
                "archive",
            ),
            "how many years are raw field notes archived",
            "raw field notes are archived for 3 years",
            _f("Field notes are scanned and indexed by study site", "archive", "fieldwork"),
        ),
    ),
)

FINANCE_VERSIONS = VersionDomain(
    FINANCE,
    (
        VersionPair(
            _f("Managers approve expenses up to 1,000 euros", "expenses", "approvals"),
            _f(
                "Manager expense approval limit lowered to 250 euros after the audit finding",
                "expenses",
            ),
            "expense approval limit for managers",
            "managers approve expenses up to 1,000 euros",
            _f("Manager approvals are recorded in the expense tool with a comment", "expenses"),
        ),
        VersionPair(
            _f("Commodity input costs are hedged at 40 percent", "hedging", "commodities"),
            _f(
                "Commodity hedge ratio raised to 70 percent given the price volatility",
                "hedging",
                body="Reviewed monthly; the ratio can move by 10 points without committee sign-off.",
            ),
            "what percent of commodity input costs are hedged",
            "commodity input costs are hedged at 40 percent",
            _f("Commodity hedges are reviewed monthly by the treasury committee", "hedging"),
        ),
        VersionPair(
            _f("Standard customer payment terms are 30 days", "receivables", "terms"),
            _f("Customer payment terms extended to 45 days for enterprise accounts", "receivables"),
            "standard customer payment terms in days",
            "standard customer payment terms are 30 days",
            _f("The late payment fee is 1.5 percent per month", "receivables", "terms"),
        ),
        VersionPair(
            _f("The revenue forecast is reviewed monthly", "forecasting", "revenue"),
            _f("Revenue forecast review moved to biweekly during the turnaround", "forecasting"),
            "how often is the revenue forecast reviewed",
            "the revenue forecast is reviewed monthly",
            _f("The revenue forecast is built bottom-up by region", "forecasting", "revenue"),
        ),
        VersionPair(
            _f("The petty cash float is 500 euros per office", "cash", "offices"),
            _f(
                "Office petty cash float cut to 200 euros; larger needs use the corporate card",
                "cash",
            ),
            "petty cash float per office",
            "the petty cash float is 500 euros per office",
            _f("Petty cash is counted and signed off every Friday", "cash", "offices"),
        ),
        VersionPair(
            _f("Project budgets carry a 10 percent contingency", "budgets", "projects"),
            _f(
                "Contingency on new project budgets increased to 15 percent after two overruns",
                "budgets",
            ),
            "contingency percent carried in project budgets",
            "project budgets carry a 10 percent contingency",
            _f("Contingency can only be released by the project sponsor", "budgets", "projects"),
        ),
    ),
)

VERSION_DOMAINS: tuple[VersionDomain, ...] = (
    DATA_VERSIONS,
    CODE_VERSIONS,
    OPS_VERSIONS,
    RESEARCH_VERSIONS,
    FINANCE_VERSIONS,
)
SYSADMIN = OPS.name
