# The admin panel exports its lists as CSV

Builds on [ADR 0091](0091-admin-panel-shared-front-end-modules.md) (the shared front-end modules) and the admin-only reads of [ADR 0080](0080-admin-memory-browser-and-activity.md), [ADR 0090](0090-admin-feedback-log.md) and [ADR 0093](0093-admin-flag-backlog.md).

Status: Accepted. Front end only: no endpoint, setting or migration.

## Context

An operator who wants to look at memories or feedback outside the panel (a spreadsheet, a notebook, an incident write-up) has to copy rows by hand, 50 at a time. The panel's lists already read everything they show through paged admin endpoints.

## Decision

1. **An "Export CSV" button** on the Memories list (listing or search), the Feedback log, the flagged list on Health and the three lists on an agent page. It exports every row under the filters the list shows, not just the loaded page.
2. **In the browser.** The panel fetches the rows through the same admin endpoints the list uses, 100 per page (the API's largest), and saves a file named `hivemind-<list>-<date>.csv`. No export endpoint is added and nothing else reaches the API.
3. **Capped at 10,000 rows**, about the API's largest offset. A capped export says so and suggests narrower filters. The export uses the filters the list shows (not text typed but not yet applied), and if the list reloads while it runs (new filters, a refresh) it stops instead of mixing two filters in one file.
4. **Agent text cannot run in a spreadsheet.** Summaries, tags and notes are written by agents, so a text cell starting with `=`, `+`, `-`, `@` (also their full-width forms, or after leading whitespace an importer may trim), a tab or a carriage return gets a leading apostrophe (the usual CSV injection guard). Cells are quoted per RFC 4180, lines end in CRLF, and the file starts with a UTF-8 byte-order mark so Excel reads it as UTF-8.
5. **Raw values.** Ids and ISO 8601 timestamps as the API gives them, the fleet's id and its name, tags joined with `; `, the verdict counts. A search export has `score` and no `created_at` (a search hit does not carry it); a listing export has the reverse.
6. **A shared module.** `csv.js` (quoting, the guard, paged fetching, the download) imports nothing, and the column sets live beside the rows in `lists.js`, so an agent-key app could offer the same export over its own endpoints.

## Consequences

- An export of a large list makes up to 100 API reads in a row; it runs only when the operator asks for it.
- It is not a point-in-time snapshot: the pages are offset reads of a live list. A row that a write shifts onto the next page is kept once (rows are de-duplicated by id), but a row that drops out mid-export (a flag resolved, say) can shift another past the export. For an exact copy, export a quiet pool or narrow the filters.
- A text cell that really begins with `-` or `+` (a negative number written as text, say) carries an apostrophe in the file.
- A search export holds what the search returns, which is capped by the search itself (twice the candidate count).
