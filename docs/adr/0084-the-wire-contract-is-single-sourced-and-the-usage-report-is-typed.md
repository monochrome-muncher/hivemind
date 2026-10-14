# The wire contract is single-sourced, and the usage report is typed

Two shapes are held in two hand-written places each.

**The entry/hit wire shape.** The REST surface serialises entries and
search hits through pydantic models (`api/schemas.py`: `EntryOut`,
`HitOut`, `LinkOut`, `FeedbackSummaryOut`, …); the MCP surface serialises
the same shapes through dict builders (`mcp/app.py`: `_entry_dict`,
`_hit_dict`, `_link_dict`, `_feedback_dict`, …). The two mappings are
supposed to agree — an agent gets the same memory through either surface —
but nothing pins that agreement: a field added to one side silently
diverges from the other, and the only evidence is that both test suites
pass.

**The usage report.** `MetricsService.usage_report()` returns a
`dict[str, Any]` with the sections `entries`, `fleets`, `searches`,
`feedback`, `health`. The REST route unpacks it into pydantic models with
`**report["section"]` (a runtime-only shape check); `api/prometheus.py`
unpacks it with `report["section"]["field"]` (a KeyError at scrape time
if a name drifts). And `MetricsOut` is missing the `searches` section
entirely, so the admin panel's searches card
(`overview.js` reads `metrics.searches`) is a dead field.

## Decision

1. **The parity test owns the canonical field mapping** —
   `tests/unit/test_wire_contract.py` carries a mapping table per wire
   shape (full entry, list item, hit, feedback summary, link) and asserts
   that the REST and MCP serializers produce exactly those fields. It is
   a characterization test first (it pins today's behaviour), and the
   guard for every later change to a wire shape.
2. **`src/hivemind/wire.py`** — a neutral module (no FastAPI, no MCP)
   holding the canonical dict mappers: `entry_dict`, `list_item_dict`,
   `hit_dict`, `related_dict`, `link_dict`, `links_dict`, `counts_dict`,
   `feedback_dict`, `warning_dict`. The MCP surface imports them; the
   REST pydantic models stay (they are the HTTP response models), and the
   parity test keeps the two in lockstep.
3. **`src/hivemind/domain/usage.py`** — the typed usage report: one
   frozen dataclass per section (`EntriesUsage`, `FleetsUsage`,
   `SearchesUsage`, `FeedbackUsage`, `HealthUsage`) composed into
   `UsageReport`. `metrics.py` stays the owner of the values and the
   cache; it now returns `UsageReport` instead of `dict[str, Any]`. The
   REST route maps `UsageReport` → `MetricsOut` by named fields (no more
   `**` unpacking); `prometheus.py` reads named fields.
4. **`MetricsOut` gains `searches`** (`SearchesMetrics`): the section the
   report has always carried and the admin panel's card has always read.
   Restoring the field revives the card; no UI change.

## Not in this ADR

- The admin vocabulary endpoint (a distinct API surface driven by a UI
  need) — deferred; pick it up when the admin UI needs it.
- Making the REST surface consume `wire.py` directly (e.g. generating the
  pydantic models from the mapping table) — the parity test is the
  agreement mechanism for now.

## Consequences

- A wire-shape change touches the mapping table, `wire.py`, and the
  matching pydantic model — and the parity test fails if any one of the
  three drifts.
- A renamed report field is a type error at the service/route/prometheus
  seam, not a 500 at scrape time.
- The admin panel's searches card answers again.
