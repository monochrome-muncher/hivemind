# Review of the docs for humans and agents

Date: 2026-11-27. Baseline: `main` at a8677a2 (v2.33.2).

Scope: the root README, the plugin README, `clients/README.md` and the two package READMEs, DEPLOY.md, the ops runbook, `docs/code-tour.md`, CONTRIBUTING.md, AGENTS.md and CLAUDE.md, CONTEXT.md, ROADMAP.md, the ADR and review indexes, and the four skills. Two questions: does a person coming in cold find what they need in a plain voice, and does an agent find accurate facts that match the code?

Not re-raised: what the 2026-11-12 docs pass (#425, #426), the 2026-11-19 human-docs review (#487, #488) and the 2026-11-24 weekly drift check (#496 to #499) fixed, or the CHANGELOG restructure the owner left as it is.

## Summary

The docs still make sense for both readers. The READMEs answer "which part do I need" on the first screen, the code tour still names a real file for every rule, and the always-on agent text is clear and well within its budget. What drifted is small and comes from 2.32 and 2.33: the portal's fleet activity chart and "Unverified only" filter never reached the root README or DEPLOY.md, and ROADMAP.md still listed agent key switching as parked although key profiles shipped in 2.26.0. The one larger question is voice. SPEC.md, CONTEXT.md and ROADMAP.md still use em dashes heavily, while the READMEs, DEPLOY.md and the runbook no longer do.

## Checked and fine

- **Links.** No broken relative link or anchor in any tracked Markdown file (a script over all of them).
- **Paths.** Every `src/`, `tests/` and repository path in AGENTS.md, the code tour, DEVELOPING.md, the READMEs, DEPLOY.md and the runbook exists. Only ADRs and `docs/retrieval-experiments.md` name removed files (`tests/eval/confirmation.py`, `make measure-confirmation`), and they say so as history.
- **Make targets.** Every `make` target the docs name exists, and AGENTS.md lists every target in the Makefile.
- **Code tour.** `require_admin_key` and `require_agent_key` (`api/routes/params.py:22,27`), `SearchService.search_result` (`services/search.py:225`), `entry_draft` (`services/drafts.py:23`), `Credential.visibility` (`ports/auth.py:132`) and `extractor.py` all where it says.
- **AGENTS.md module lists** match `store/pgstore/`, `memstore/`, `domain/`, `services/` and `ports/`.
- **Always-on text.** The skill, the server instructions, the ten tool descriptions and the stay-aware reminder total 17,282 bytes of the 18,000-byte budget (`tests/unit/agent_plugin/test_plugin.py:460`). The `hivemind` skill reads well: a table for each `hive_whoami` state, one section per moment of a session, and the data-not-instructions rule stated plainly. No contract text changed since the last drift check.
- **Indexes.** Every ADR has a row in `docs/adr/README.md`; every review in `docs/reviews/` is in its index.
- **Voice.** No em dash and no AI phrasing ("seamless", "robust", "leverage" and the like) in any README, DEPLOY.md, the runbook, the code tour, CONTRIBUTING.md, SECURITY.md, DEVELOPING.md or the skills. The one em dash in `references/junie.md:44` quotes Junie's own message.
- **Client READMEs** against the packages: `httpx2` (`clients/python/pyproject.toml:26`), Python 3.10 and Node 22 floors, the typed replies, and the SPEC §5.2 anchor they link to.

## Findings fixed in this PR

**D1. The root README's portal list stops before 2.33.0** (`README.md:181-202`). It has no word on the fleet activity chart on the portal's home page (ADRs 0165, 0167; route allowed at `portal/app.py:64-65`) or on the "Unverified only" filter with the fleet's count (2.33.1, `portal/static/views.js:371-372`). Fixed: a "Follow the fleet" bullet, and the filter in the fleet memories bullet.

**D2. DEPLOY.md §9's list of forwarded portal routes misses `GET /v1/portal/fleet-activity`** (`DEPLOY.md:841-850` against `portal/app.py:64-65`). An operator who reads it to write an ingress or proxy rule would leave the chart broken. Fixed.

**D3. ROADMAP.md says agent key switching is parked** (`ROADMAP.md:73-74`). Key profiles shipped in 2.26.0 (ADR 0150, SPEC.md:324). An agent reading ROADMAP as "what is open" would think the feature is missing. Fixed: the server-side proposal was not built, and key profiles on the client replaced it.

**D4. ROADMAP.md lists the re-check queue and librarian skill as a live candidate** (`ROADMAP.md:75-78`). The owner parked it on 2026-11-27 and calls it "the librarian idea". Fixed, under that name.

**D5. CONTEXT.md's "Replica" names only the API and the MCP runner** (`CONTEXT.md:449`). The portal also runs two replicas (SPEC.md:332, `deploy/kubernetes/portal/hivemind-portal-deployment.yaml`). Fixed.

**D6. Three terms the docs use have no glossary entry.** AGENTS.md says the glossary stays current, but CONTEXT.md had nothing for:
- *fleet activity*, the portal chart (ADRs 0165, 0167; SPEC.md:200);
- *client package*, `hivemind-memory` (ADR 0162; README, AGENTS.md, clients/);
- *tool refusal* against *tool failure*, which the dashboard and DEPLOY §10 rely on (ADR 0161, `mcp/metrics.py:23`).
Fixed: three entries, each with the words to avoid.

**D7. AGENTS.md undersells CI.** Its quality bar (`AGENTS.md:100`) named the tests and a Docker build, but CI also runs Hermes's install scan and the client packages' tests (`.github/workflows/tests.yml:48,116`). Its "Where to look" table had no row for the client packages. Fixed: both added. CONTRIBUTING.md's loop (`CONTRIBUTING.md:31-36`) now says the same in one paragraph.

**D8. AGENTS.md used em dashes** (12, the title and the reading list among them). The root README sends people there for "working on the code", so it is read by humans too. Fixed, with no change in meaning.

**D9. The README's image tag example drifts every release** (`README.md:461`, `v2.31.3` at v2.33.2; fixed by hand three times before). Fixed: it says `vX.Y.Z` and links the releases page, so it no longer goes stale.

**D10.** One over-long line in the README's portal list (`README.md:199`) reflowed.

## For the owner to decide

**V1. Plain voice in SPEC.md, CONTEXT.md and ROADMAP.md.** They hold 130, 29 and 28 em dashes, and the README sends people to them ("exactly what the system does", "what each term means", "what is planned"). DEPLOY.md and the runbook got this pass on 2026-11-19 (#488). CONTEXT.md and ROADMAP.md are a small pass. SPEC.md is 138 KB and needs care: the client READMEs, which are also published on PyPI and npm, link to the anchor of the §5.2 heading, which contains an em dash, so that heading has to keep its anchor.

**V2. The README screenshots predate the 2.33.0 UI refresh.** All six images in `docs/images/` were last changed on 2026-11-18 (#475); the refresh (#526) and the stacked-bar fleet chart (#527) came on 2026-11-26. `portal-fleet.png` shows the portal's home fleet page without its chart. New screenshots need a seeded demo pool and a browser run, so this is a separate PR.

Not raised: the CHANGELOG's per-release layout (declined on 2026-11-19), the em dashes in the CHANGELOG's 1.x and 2.0.0 rows (history), and AGENTS.md's long table cells (dense, but agents read them well, and the code tour is the friendlier entry for people).
