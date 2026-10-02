"""A multi-domain fixture for the similarity threshold (ADR 0062).

Five kinds of agent work, each the size of a small fleet's pool: data
analysis, software engineering, system administration, journalism and
social science research, and finance analysis. Each domain has entries
(most with a short body, as real entries do), queries its entries answer,
and in-domain queries none of them answers: the hard cases, close in topic
to what the pool holds. ``CROSS_DOMAIN`` adds queries a fleet could ask of
another fleet's entry, where the work overlaps.

The queries are paraphrased rather than copied from their answer, in the
mixed styles agents search with: questions, keyword strings, longer
descriptions.
"""

from __future__ import annotations

from dataclasses import dataclass

# (kind, summary, tags, body)
Entry = tuple[str, str, tuple[str, ...], str | None]


@dataclass(frozen=True, slots=True)
class Domain:
    name: str
    entries: tuple[Entry, ...]
    answerable: tuple[tuple[str, tuple[int, ...]], ...]  # (query, entry indices)
    unanswered: tuple[str, ...]  # in-domain queries no entry in any domain answers


DATA = Domain(
    "data analysis",
    (
        (
            "decision",
            "Churn model uses a 90-day inactivity window as the churn label",
            ("churn", "modeling"),
            "Customers with no login and no purchase for 90 days are labelled churned. "
            "30 and 60 days produced too many false positives among seasonal buyers.",
        ),
        (
            "fact",
            "The events table double counts mobile sessions before March 2026",
            ("data-quality", "events"),
            "The mobile SDK fired session_start twice on resume. Deduplicate on "
            "(device_id, session_id) for any analysis spanning that period.",
        ),
        (
            "insight",
            "Weekly active users dip every Easter week by about 12 percent",
            ("seasonality", "engagement"),
            "Seen in 2024, 2025 and 2026. Do not read it as a product regression.",
        ),
        (
            "decision",
            "A/B tests use CUPED variance reduction with the pre-period metric",
            ("experimentation", "statistics"),
            None,
        ),
        (
            "fact",
            "The revenue dashboard refreshes at 06:00 UTC from the warehouse snapshot",
            ("dashboards", "revenue"),
            "Numbers viewed before 06:00 UTC show the day before yesterday.",
        ),
        (
            "insight",
            "Survey responses from the in-app prompt skew toward power users",
            ("survey", "bias"),
            "Weight by activity decile before reporting satisfaction scores.",
        ),
        (
            "fact",
            "Customer lifetime value is computed over 36 months with a 10 percent discount rate",
            ("ltv", "metrics"),
            None,
        ),
        (
            "decision",
            "Outliers in order value are winsorized at the 99th percentile",
            ("cleaning", "orders"),
            "Bulk B2B orders otherwise dominate averages in the consumer reports.",
        ),
        (
            "fact",
            "The marketing attribution model is last non-direct click",
            ("attribution", "marketing"),
            "Data-driven attribution was evaluated and rejected because of sparse conversion paths.",
        ),
        (
            "insight",
            "Time-series forecasts of signups need a holiday regressor for Black Friday",
            ("forecasting", "signups"),
            "Without it the Prophet model under-forecasts November by roughly a third.",
        ),
        (
            "fact",
            "Country is derived from billing address, not IP geolocation",
            ("geography", "definitions"),
            None,
        ),
        (
            "decision",
            "Use the median, not the mean, for session length in product reports",
            ("reporting", "engagement"),
            "Session length is heavily right-skewed by tabs left open.",
        ),
        (
            "fact",
            "The feature store recomputes user embeddings nightly in the batch job",
            ("feature-store", "ml"),
            None,
        ),
        (
            "insight",
            "Cohort retention curves flatten after week 8 for paid users",
            ("retention", "cohorts"),
            None,
        ),
        (
            "fact",
            "Analysts query the warehouse through a read-only role with a 5 minute timeout",
            ("warehouse", "access"),
            "Long exploratory queries should run on the sandbox replica instead.",
        ),
        (
            "insight",
            "Conversion rate fell after the checkout redesign mostly on Android tablets",
            ("conversion", "checkout"),
            None,
        ),
    ),
    (
        ("how do we define a churned customer", (0,)),
        ("mobile session counts look inflated in older data", (1,)),
        ("why does engagement drop around Easter", (2,)),
        ("variance reduction technique for experiments", (3,)),
        ("when is the revenue dashboard updated each day", (4,)),
        ("are in-app survey results representative of all users", (5,)),
        ("LTV horizon and discount rate", (6,)),
        ("how to handle extreme order values in averages", (7,)),
        ("which attribution model does marketing use", (8,)),
        ("signup forecast misses the November peak", (9,)),
        ("should I report average or median time spent per session", (11,)),
        ("checkout conversion drop by device", (15,)),
    ),
    (
        "how do we impute missing values in the customer age column",
        "minimum sample size rule for launching an A/B test",
        "which clustering method do we use for customer segmentation",
        "definition of a qualified marketing lead",
        "how are refunds treated in the net revenue metric",
        "net promoter score methodology",
    ),
)

CODE = Domain(
    "software engineering",
    (
        (
            "decision",
            "Backend services are written in Go; Python is only for data jobs",
            ("languages", "architecture"),
            None,
        ),
        (
            "fact",
            "The payments service retries idempotent requests with an Idempotency-Key header",
            ("payments", "api"),
            "Keys are stored for 24 hours. A replay with the same key returns the first response.",
        ),
        (
            "insight",
            "Flaky integration tests were caused by shared test database state",
            ("testing", "flaky"),
            "Each test now runs in a transaction that is rolled back. Never share fixtures between packages.",
        ),
        (
            "decision",
            "Feature flags live in LaunchDarkly and must be removed within two releases",
            ("feature-flags", "process"),
            None,
        ),
        (
            "fact",
            "The web frontend is React with TypeScript strict mode and Vite",
            ("frontend", "react"),
            None,
        ),
        (
            "decision",
            "Database migrations must be backwards compatible for one release",
            ("migrations", "database"),
            "Add the column, deploy, backfill, then remove the old column in the next release.",
        ),
        (
            "fact",
            "gRPC is used between internal services and REST at the public edge",
            ("grpc", "api"),
            None,
        ),
        (
            "insight",
            "The N+1 query in the orders endpoint came from lazy loading line items",
            ("performance", "orm"),
            "Eager-load line items with the order. Response time went from 900 ms to 80 ms.",
        ),
        (
            "decision",
            "Code review needs one approval from a code owner of each touched directory",
            ("review", "process"),
            None,
        ),
        (
            "fact",
            "Release branches are cut every second Tuesday and frozen 48 hours before release",
            ("release", "process"),
            None,
        ),
        (
            "insight",
            "Goroutine leak in the notifier came from unbuffered channels without a timeout",
            ("go", "concurrency"),
            "Use context cancellation on every send; the leak grew memory by 2 GB a day.",
        ),
        (
            "fact",
            "API pagination uses opaque cursors, never page numbers",
            ("api", "pagination"),
            None,
        ),
        (
            "decision",
            "Errors returned to clients carry a stable code and a request id, never a stack trace",
            ("errors", "api"),
            None,
        ),
        (
            "fact",
            "The monorepo builds with Bazel and remote caching on the CI cluster",
            ("build", "ci"),
            None,
        ),
        (
            "insight",
            "Upgrading the JSON library halved serialization CPU in the catalog service",
            ("performance", "serialization"),
            None,
        ),
        (
            "fact",
            "Mobile apps support the last two major iOS and Android versions",
            ("mobile", "compatibility"),
            None,
        ),
    ),
    (
        ("what language should a new backend service use", (0,)),
        ("how do retries avoid double charging customers", (1,)),
        ("integration tests fail randomly", (2,)),
        ("how long can a feature flag stay in the code", (3,)),
        ("zero-downtime schema change process", (5,)),
        ("protocol for service to service calls", (6,)),
        ("orders endpoint slow because of too many SQL queries", (7,)),
        ("who needs to approve my pull request", (8,)),
        ("when is the release branch cut", (9,)),
        ("memory keeps growing in a go service", (10,)),
        ("how should list endpoints paginate", (11,)),
        ("what should an API error response contain", (12,)),
    ),
    (
        "naming convention for kafka topics",
        "which logging library do the go services use",
        "how do we version the public REST API",
        "rules for accessibility in the web frontend",
        "how are secrets injected into local development",
        "load testing tool and target throughput",
    ),
)

OPS = Domain(
    "system administration",
    (
        (
            "fact",
            "Production runs on Kubernetes 1.33 across three availability zones",
            ("kubernetes", "infrastructure"),
            None,
        ),
        (
            "decision",
            "SSH to production hosts goes through the bastion with short-lived certificates",
            ("ssh", "access"),
            "Certificates are issued by Vault for 8 hours. Static keys were removed in 2025.",
        ),
        (
            "fact",
            "Postgres backups run nightly with point-in-time recovery for 14 days",
            ("backup", "postgres"),
            "A restore drill runs every first Monday; the last full restore took 47 minutes.",
        ),
        (
            "insight",
            "Disk pressure on log nodes came from journald without a size cap",
            ("logging", "disk"),
            "SystemMaxUse=2G is now set fleet-wide.",
        ),
        (
            "decision",
            "Pages fire only for user-facing symptoms; causes go to tickets",
            ("alerting", "on-call"),
            None,
        ),
        (
            "fact",
            "TLS certificates are issued by cert-manager from Let's Encrypt and renew at 30 days",
            ("tls", "certificates"),
            None,
        ),
        (
            "fact",
            "The on-call rotation is weekly, handing over Monday at 10:00 local time",
            ("on-call", "rotation"),
            None,
        ),
        (
            "insight",
            "The DNS outage in June was a TTL of 86400 on the load balancer record",
            ("dns", "incident"),
            "Lower TTLs to 300 seconds before any planned change.",
        ),
        (
            "decision",
            "Infrastructure changes go through Terraform with plan output in the pull request",
            ("terraform", "iac"),
            None,
        ),
        (
            "fact",
            "Node OS patches are applied by a rolling drain every Wednesday night",
            ("patching", "nodes"),
            None,
        ),
        (
            "fact",
            "Prometheus keeps 15 days of metrics; long-term storage is in Thanos",
            ("monitoring", "prometheus"),
            None,
        ),
        (
            "insight",
            "OOM kills in the API pods came from a JVM heap set above the container limit",
            ("memory", "kubernetes"),
            "Set -XX:MaxRAMPercentage=75 instead of a fixed -Xmx.",
        ),
        (
            "fact",
            "Office VPN is WireGuard; contractors get a separate peer group without prod access",
            ("vpn", "network"),
            None,
        ),
        (
            "decision",
            "Every service needs a runbook linked from its alerts before going live",
            ("runbook", "process"),
            None,
        ),
        (
            "fact",
            "Object storage buckets have versioning on and a 90-day noncurrent expiry",
            ("storage", "s3"),
            None,
        ),
        (
            "insight",
            "Cron jobs on the old VM overlapped because they had no lock",
            ("cron", "scheduling"),
            "Wrapped every job in flock.",
        ),
    ),
    (
        ("how do I get shell access to a production machine", (1,)),
        ("how far back can we restore the database", (2,)),
        ("log server disk filling up", (3,)),
        ("what should wake someone up at night", (4,)),
        ("certificate renewal automation", (5,)),
        ("when does on-call hand over", (6,)),
        ("cause of the june dns incident", (7,)),
        ("how are infrastructure changes reviewed", (8,)),
        ("pods killed for out of memory", (11,)),
        ("metrics retention period", (10,)),
        ("vpn access for contractors", (12,)),
        ("scheduled jobs running twice at the same time", (15,)),
    ),
    (
        "how do we rotate the database passwords",
        "capacity plan for adding a fourth availability zone",
        "which ports are open on the public firewall",
        "how long are audit logs kept for compliance",
        "procedure for decommissioning a server",
        "GPU node pool autoscaling limits",
    ),
)

RESEARCH = Domain(
    "journalism and social science",
    (
        (
            "decision",
            "Every published statistic needs two independent sources or a primary document",
            ("verification", "standards"),
            None,
        ),
        (
            "fact",
            "The housing survey sample is 2,400 households weighted to census age and region",
            ("survey", "housing"),
            "Margin of error is about 2 points at 95 percent confidence.",
        ),
        (
            "insight",
            "Interviewees in the labour story were more candid when not recorded",
            ("interviews", "methods"),
            "Take written notes and confirm quotes by email afterwards.",
        ),
        (
            "fact",
            "Anonymous sources must be approved by the managing editor",
            ("sources", "ethics"),
            None,
        ),
        (
            "insight",
            "Local election turnout correlates with distance to the polling station",
            ("elections", "turnout"),
            "Each extra kilometre lowered turnout by about 1.5 points in the 2025 municipal data.",
        ),
        (
            "decision",
            "Corrections are appended to the article with a dated note, never silently edited",
            ("corrections", "standards"),
            None,
        ),
        (
            "fact",
            "The coding scheme for the protest dataset has 14 categories and two coders",
            ("content-analysis", "coding"),
            "Inter-coder reliability was Krippendorff's alpha 0.82.",
        ),
        (
            "insight",
            "Freedom of information requests to the ministry take about 11 weeks on average",
            ("foi", "government"),
            None,
        ),
        (
            "fact",
            "Photos of minors need written consent from a guardian",
            ("photography", "consent"),
            None,
        ),
        (
            "insight",
            "Survey questions about income get 30 percent nonresponse unless asked in bands",
            ("survey", "questionnaire"),
            None,
        ),
        (
            "decision",
            "Interview transcripts are stored encrypted and deleted after two years",
            ("data-protection", "interviews"),
            None,
        ),
        (
            "fact",
            "The misinformation tracker classifies claims as false, misleading, or lacking context",
            ("fact-checking", "misinformation"),
            None,
        ),
        (
            "insight",
            "Readers trust charts with source lines more than the same chart without",
            ("audience", "visualization"),
            None,
        ),
        (
            "fact",
            "Ethics board approval is required for any study with vulnerable participants",
            ("ethics", "irb"),
            None,
        ),
        (
            "insight",
            "Regression on neighbourhood income needs clustered standard errors by district",
            ("statistics", "regression"),
            None,
        ),
        (
            "fact",
            "Embargoed reports may be read but not quoted before the embargo time",
            ("embargo", "press"),
            None,
        ),
    ),
    (
        ("how many sources do we need before publishing a number", (0,)),
        ("sample size and weighting of the housing poll", (1,)),
        ("tips for getting people to talk openly in interviews", (2,)),
        ("can I use an unnamed source", (3,)),
        ("what affects voter turnout in local elections", (4,)),
        ("how do we fix an error in a published story", (5,)),
        ("reliability of the protest event coding", (6,)),
        ("how long do FOI requests take", (7,)),
        ("asking about income in questionnaires", (9,)),
        ("how long do we keep interview recordings", (10,)),
        ("fact-check rating categories", (11,)),
        ("standard errors for district level data", (14,)),
    ),
    (
        "style guide rule for naming people on second reference",
        "how do we handle legal threats after publication",
        "focus group recruitment incentives",
        "which archive holds the historical newspaper scans",
        "translation policy for quotes in other languages",
        "how to cite preprints in the research report",
    ),
)

FINANCE = Domain(
    "finance analysis",
    (
        (
            "decision",
            "Discounted cash flow models use a 9 percent WACC for the base case",
            ("valuation", "dcf"),
            "Sensitivity tables show 8 and 10 percent. Terminal growth is 2 percent.",
        ),
        (
            "fact",
            "Quarter close happens on the fifth business day after quarter end",
            ("close", "accounting"),
            None,
        ),
        (
            "insight",
            "Gross margin fell two points because of freight costs, not pricing",
            ("margin", "costs"),
            "Freight per unit rose 40 percent in Q2 while average selling price held.",
        ),
        (
            "fact",
            "Revenue is recognised on delivery for hardware and ratably for subscriptions",
            ("revenue-recognition", "accounting"),
            None,
        ),
        (
            "decision",
            "FX exposure above 2 million euros is hedged with 3-month forwards",
            ("fx", "hedging"),
            None,
        ),
        (
            "fact",
            "The budget variance threshold for an explanation is 5 percent or 50,000 euros",
            ("budget", "variance"),
            None,
        ),
        (
            "insight",
            "Days sales outstanding rose to 58 because two distributors paid late",
            ("working-capital", "receivables"),
            None,
        ),
        (
            "fact",
            "Comparable companies for valuation are the six listed SaaS peers in the deck",
            ("valuation", "comparables"),
            None,
        ),
        (
            "decision",
            "Capex above 100,000 euros needs CFO sign-off and a payback under 3 years",
            ("capex", "approval"),
            None,
        ),
        (
            "fact",
            "The credit facility covenant requires net debt to EBITDA below 3.0",
            ("debt", "covenants"),
            None,
        ),
        (
            "insight",
            "Customer concentration risk: the top three customers are 41 percent of revenue",
            ("risk", "customers"),
            None,
        ),
        (
            "fact",
            "Expense reports over 500 euros need receipts and manager approval",
            ("expenses", "policy"),
            None,
        ),
        (
            "insight",
            "The equity research note models the competitor's buyback as EPS accretive by 4 percent",
            ("equity-research", "buyback"),
            None,
        ),
        ("fact", "Cash forecasts are rolled weekly for 13 weeks", ("cash", "forecasting"), None),
        (
            "decision",
            "Scenario analysis uses base, downside and severe downside, no upside case",
            ("scenarios", "planning"),
            None,
        ),
        (
            "fact",
            "Inflation assumption for the five-year plan is 2.5 percent per year",
            ("assumptions", "planning"),
            None,
        ),
    ),
    (
        ("discount rate for the valuation model", (0,)),
        ("when do the books close each quarter", (1,)),
        ("why did gross margin drop", (2,)),
        ("when do we book subscription revenue", (3,)),
        ("currency hedging policy", (4,)),
        ("when does a budget miss need a written explanation", (5,)),
        ("why are customers paying us more slowly", (6,)),
        ("which peers do we compare our valuation against", (7,)),
        ("approval needed for a large equipment purchase", (8,)),
        ("leverage covenant on the loan", (9,)),
        ("how dependent are we on our biggest customers", (10,)),
        ("cash flow forecast horizon", (13,)),
    ),
    (
        "transfer pricing policy between subsidiaries",
        "how do we depreciate leasehold improvements",
        "which bank handles payroll payments",
        "dividend policy for next year",
        "tax loss carryforward balance",
        "how are stock options expensed",
    ),
)

DOMAINS: tuple[Domain, ...] = (DATA, CODE, OPS, RESEARCH, FINANCE)

# Queries from one kind of work whose answer sits in another domain's pool:
# (query, domain name, entry index in that domain).
CROSS_DOMAIN: tuple[tuple[str, str, int], ...] = (
    ("how much revenue comes from the largest accounts", "finance analysis", 10),
    ("is the dashboard showing yesterday's numbers in the morning", "data analysis", 4),
    ("schema change rules when the analytics pipeline adds a column", "software engineering", 5),
    (
        "which statistics need a second source before a newsletter goes out",
        "journalism and social science",
        0,
    ),
    ("java service restarted with out of memory in the cluster", "system administration", 11),
    (
        "standard errors when observations are grouped by region",
        "journalism and social science",
        14,
    ),
)
