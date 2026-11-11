# Admin panel and agent portal: review and new ideas

Date: 2026-11-11. Thread "Find missing admin panel and portal features".
Status: proposal, nothing built. Base: main at 49e8107 (2.23.1).

How I looked: read every view of both apps and their routes, then ran the API, panel and portal locally on a seeded pool (3 fleets, 6 agents, 20 memories, verdicts, withdrawals) and went through each page in a browser.

Earlier rounds (2026-10-18, 10-27, 10-28, 11-07) covered the feedback log, agent page, pins, flag backlog, CSV, key replace, fleet rename/merge/move, idle list, cleanup, retire, portal self-service, Needs you, agent history, restore, owner edit and redaction. None of that is repeated. The ideas below are new; most take a step back from "manage one record" towards "answer the question the operator or owner actually has".

## Bug found and fixed

**Flags column always empty in memory lists.** The memory tables in the panel (Memories list, agent page) and the portal (Its memories, Home fleet) have a Flags column, but `GET /v1/admin/entries` and `GET /v1/portal/entries` never returned feedback (`api/routes/admin_entries.py:100`, `api/routes/portal.py:98`), so every row showed "—", flagged or not. Only search results filled it. Fixed in PR #393 (merged): both list routes add each row's verdict counts from one batched read.

## Admin panel ideas, ranked

### 1. Owner page and "Offboard this owner" (M)

People leave or change teams, and their agents go with them. Today that means finding each agent and running Revoke or Retire one by one (`admin/static/manage.js:300`, buttons per agent). The Stats "Users" rows don't link anywhere (`admin/static/stats.js:49`, `href: null`).

Proposal: an owner page `#/owner?alias=…` (like the agent page): the owner's agents, their numbers, memories and verdicts. One "Offboard…" action: hand every agent's memories to one successor agent (the existing retire, ADR 0113) or revoke them all, with a preview, as one audited batch that can be undone like a retirement. Owner aliases stay unverified labels, so the page says so.

### 2. Search as an agent (S-M)

The most common support question is "why can't my agent find X?". The admin search sees everything (`admin_entries.py:117`), so it can't answer that.

Proposal: an "As agent" picker in Memories search. The search runs with that agent's visibility and fleet, not counted, and says which memories the admin can see that the agent can't (scope, other fleet, withdrawn). No new rights: the admin already reads all of it.

### 3. Why this rank (M)

Hits carry one number (`services/search.py:58`). Whether ranking needs a change is decided from production, and today nobody can see why a hit landed where it did.

Proposal: in the admin search only, an expandable row per hit with the parts of the score: vector similarity and rank, keyword rank, fused RRF, recency factor, quality multiplier, importance, social cap. Display only, admin route only; agents never see it.

### 4. Near-duplicate review list (S-M)

Health shows the echo rate as one number (`admin/static/health.js:62`; 40% on my small seed). Like the flag backlog before ADR 0093, there's no list behind it.

Proposal: a "Near copies" list on Health: each recent memory with its earlier near copy and the cosine, both openable, newest first, with a fleet filter. The admin withdraws the duplicate or leaves it. It reuses the echo query (`store/pgstore/metrics.py:256`).

### 5. Memory browser filters the API already has (S)

`GET /v1/admin/entries` takes tags, entities and written/memory date ranges (`admin_entries.py:65-75`), but the Memories form offers none of them (`admin/static/memories.js:34`). Tags in the drawer are plain text (`drawer.js:171`). There is also no "withdrawn only" choice, which is what restore and redact work needs (`EntryFilters` has only `include_inactive`, `domain/entry.py:296`).

Proposal: tag, entity and date-range fields, clickable tag and entity chips (table and drawer) that filter by them, and a State choice of Active / Withdrawn / Superseded / All (a small filter addition in both stores).

### 6. Fleet page (S-M, no new routes)

Fleets are one table row each. Proposal: `#/fleet?id=…` with members, pins, memories, flag backlog, an activity chart and the fleet's merge/move history, built from routes that exist. Same shape as the agent page.

### 7. "Since you last looked" on the Overview (S, browser only)

Proposal: the panel remembers (in the browser) when the admin last opened it and shows a line such as "Since Tuesday: 2 new agents, 5 withdrawals by agents, 1 key locked, 3 new flags", each linking to the audit log or list from that time. Nothing on the server changes.

### 8. Key age (S-M)

The Agents page shows when a key was last used (`api/schemas/agents.py:82`) but not how old it is. Proposal: "key issued" date per agent and a "key older than N days" filter, so a rotation policy can be followed. Either from the audit log or a small column.

## Agent portal ideas, ranked

### P1. "It relied on these, and they changed" (S)

ADR 0098 added `relied_on_changed` to `GET /v1/entries` (`api/routes/entries.py:156`): memories the agent rated helpful that were later superseded, withdrawn or flagged. The agent sees it; the person behind it never does, and the portal allowlist has no route for it.

Proposal: a portal page listing them, each with what changed. It answers "is anything my agent built on now wrong?", the retraction problem ADR 0098 is about, for the human.

### P2. What's new in the home fleet (S, browser only)

Proposal: the Home fleet page marks memories, pins, supersessions and withdrawals since the last visit (remembered in the browser), so an owner can skim the week.

### P3. Connect a harness (S-M)

After the admin hands over a key, the owner has to find the setup steps for their harness in the plugin README. Proposal: a portal page with the steps per harness and the MCP address filled in (a new `HIVEMIND_PORTAL_MCP_URL` setting). It never shows the key; it tells the owner where to paste it. "Key last used" then confirms the setup worked.

### P4. Ask the admin (M, a new write surface)

After "This key leaked" the portal says "ask the admin" (`portal/static/portal.js:158`), and wanting a higher trust level or another fleet is the same. Proposal: the portal files a request (new key, trust level, fleet) with a short note; the admin sees it in Needs you and acts with the existing dialogs. Audited, one open request per kind per agent. Lower rank because it adds a write path to the portal.

## Order and sizes

| # | Feature | Size | Backend | Review |
|---|---|---|---|---|
| 1 | Owner page and offboarding | M | batch over retire/revoke, ADR | opus (data, undo) |
| 2 | Search as an agent | S-M | admin search param | opus (visibility) |
| 3 | Why this rank | M | score breakdown | none |
| 4 | Near-copy list | S-M | new admin list route | none |
| 5 | Memory browser filters | S | state filter | none |
| 6 | Fleet page | S-M | none | none |
| 7 | Since you last looked | S | none | none |
| 8 | Key age | S-M | maybe a column | none |
| P1 | Relied on, since changed | S | portal route | opus (visibility) |
| P2 | What's new in the fleet | S | none | none |
| P3 | Connect a harness | S-M | one setting | none |
| P4 | Ask the admin | M | table, routes, ADR | opus |

If you want a short first batch: 5, 7, P1 and P2 are small and need little or no backend; 1 and 2 are the ones that change daily operations most.

## Considered and left out

- Writing org memories from the panel: ADR 0080 keeps the browser read-only plus withdraw, and memories should come from agents.
- Bulk withdraw of a search result: agent cleanup already covers the realistic case, and a broad bulk action is easy to misuse.
- A log of searches that found nothing: queries can hold secrets or personal data, a new kind of stored data. The count stays enough.
- Live updates (websockets): the Refresh buttons are enough at current sizes.
- A dark mode toggle: decided to follow the system.

## Outcome (2026-11-11)

Picked: 1 (owner page and offboarding), 2 (search as an agent), 3 (why this rank) and P1 (relied on, since changed). Each is built as its own PR with an ADR (0135 to 0138).
