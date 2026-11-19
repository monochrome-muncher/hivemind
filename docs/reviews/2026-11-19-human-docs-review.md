# Review of the human-facing docs

Date: 2026-11-19. Baseline: `main` at dcfcd70 (v2.31.1).

Scope: the root README, the plugin README (read in full), the per-harness notes it links to, `clients/README.md` and the two package READMEs, DEPLOY.md, `docs/ops-runbook.md`, `docs/code-tour.md`, CONTRIBUTING.md, SECURITY.md and the plugin CHANGELOG. The question was the one a person coming in cold asks: what is this, which install path is mine, how do I get it running, how do I operate it, and where do I find things.

Not re-raised: the items fixed in the 2026-11-12 docs pass (#425, #426), the 2026-11-19 full review (#478) and the client packages review (#484).

## Summary

The two READMEs a newcomer reads first are in good shape. The root README answers "which part do I need" in its first screen, explains the terms before it uses them, labels every command, and has a troubleshooting list built from real snags. The plugin README is long (45 KB) but each harness has its own section and the contents list gets you there. Neither has an em dash or AI phrasing. Every relative link and anchor in the files above resolves (checked with a script over all of them).

The weak spot is the operator side. A person deploying Hivemind for an organization gets the least help of any reader:

- DEPLOY.md never said to point the ConfigMap at your embedding service.
- DEPLOY.md assumed GitLab with no word on doing the same without it.
- The ops runbook calls itself the single-node story, but 15 of its 21 sections are admin tasks that apply to every deployment, and nothing pointed Kubernetes operators there.
- Both operator docs are written in a dense, ADR-citing voice with em dashes (64 in DEPLOY.md, 39 in the runbook), unlike the READMEs.

## Findings fixed in this PR

**H1. DEPLOY.md never says to edit `deploy/kubernetes/configmap.yaml`.** The ConfigMap ships `HIVEMIND_EMBEDDING_ENDPOINT: "http://vllm:8000/v1"`, model Qwen3-Embedding-0.6B and dim 1024 (`deploy/kubernetes/configmap.yaml:6-8`). The prerequisites say to stand up an embedding endpoint, and the checklist (§3) lists the GitLab variables, but no step says to put the endpoint, model and dimension in the ConfigMap. An operator whose embedder is not a Service called `vllm` deploys pods that fail every write with `embedding_unavailable`, and the dimension gets fixed by the first migration. Fixed: a paragraph at the top of §2 and a line in the §3 checklist.

**H2. No way to deploy without GitLab.** README and DEPLOY.md name GitLab CI/CD as the way to deploy on Kubernetes. The deploy job is a handful of `kubectl` commands (`.gitlab-ci.yml:174-208`) and `scripts/ci-bootstrap-keys.sh` runs fine by hand, but a reader on GitHub Actions, Argo CD or a laptop had to reverse-engineer that. Fixed: a "Without GitLab" section in DEPLOY.md §3 with the same steps, using the published `ghcr.io` image and a checkout of the matching tag. The root README mentions it.

**H3. The ops runbook's scope was hidden.** Its title was "Ops Runbook (Tier 3.1)" (an internal roadmap tier), its intro said "deployment, backups, monitoring, key rotation", and DEPLOY.md said "the two do not overlap". In fact sections 4 and 6 to 21 (keys, the embedding dimension, merging fleets, cleanups, retiring, redaction, offboarding, phases, scouts, renames) apply to any deployment. With 21 sections and 1,400 lines it also had no contents list. Fixed: a plain title, an intro that says which sections are single-machine and which are for everyone, and a contents list split into "Running it" and "Admin tasks". DEPLOY.md's intro and §8, and the root README's docs list, now point there for admin tasks.

**H4. DEPLOY.md had no contents list** for its nine sections. Fixed, along with a plain title ("Deploying Hivemind on Kubernetes").

**H5. DEPLOY.md's intro said two replicas of the API and the MCP runner only.** The portal also runs two (`deploy/kubernetes/portal/hivemind-portal-deployment.yaml:17`), as the root README says. Fixed.

**H6. DEPLOY.md §9's list of forwarded portal routes missed `POST /v1/portal/entries/{id}/move-to-self`** (`src/hivemind/portal/app.py:55`), and the feature list missed moving a memory to `self` (ADR 0145). Fixed.

**H7. DEPLOY.md §8's feature list stopped at 2.18.0.** Missing: rename an agent (ADR 0159), offboard an owner (ADR 0135), search as one agent with explanations (ADRs 0136, 0137), fleet phases and scouts (ADRs 0143, 0144), move to self (ADR 0145). Fixed in one sentence that also links the runbook steps.

**H8. DEPLOY.md §4.1 sent the reader to "§2 step 4" to re-run the bootstrap**; the bootstrap is step 3, and re-running it also issues a new admin key, which the text did not say. Fixed.

**H9. The plugin README says "The agent's name is permanent"** (`plugins/hivemind/README.md:383`). Since 2.29.0 an admin can rename an agent (ADR 0159). Fixed: "Only an admin can change the name later". The setup skill's wording ("unique in the organization and permanent (names are never released...)") is agent-facing and still true from the agent's side, so it is left alone.

**H10. The root README's admin panel list did not mention renaming an agent**, and the list of `HIVEMIND_RUNNER` values left out `reextract` (`entrypoint.sh:19`). Its example image tag was `v2.30.0`. Fixed (now `v2.31.1`).

**H11. `clients/README.md` reads as a maintainer page.** After two sentences for the person building an agent, it moves on to `make sync-client-text`, the live test server and a one-time publishing setup that this repository finished with 2.31.0. Fixed: a short "what you need to start" line for users, and everything else under "For maintainers", with a note that the one-time setup is done here and is for forks.

**H12. Small ones.** The root README's docs list now has SECURITY.md and says what else the runbook covers. The plugin README spelled KiloCode as "Kilo Code" once.

## Larger items, for the owner to decide

These are restructures rather than fixes, so they were not in the first PR (#487). The owner chose L1 and L2; they are done in a follow-up PR (plain voice in DEPLOY.md and the runbook, and DEPLOY.md §10 Monitoring). L3 is left as it is.

**L1. DEPLOY.md and the runbook in plain voice.** Both docs use em dashes heavily, bold for emphasis mid-sentence, "LOUD", "single source of truth", and an ADR number in most sentences. They are accurate, but they read like design notes, not instructions. A pass in the READMEs' voice would keep every fact and command, move ADR numbers to the end of a paragraph or a "Why" line, and drop the em dashes. It is mechanical but touches most of 130 KB, so it is worth a yes first.

**L2. Monitoring has no section of its own in DEPLOY.md.** Prometheus scraping, the dashboard and the alerts sit in §2 steps 7 and 8, two long paragraphs inside the quickstart. A "Monitoring" section (what is scraped, the dashboard, the alerts, what to watch after an upgrade), with the quickstart steps reduced to one line each, would make them findable. It fits naturally into L1.

**L3. The CHANGELOG is hard for a person to scan.** It is the only release history and the root README sends operators there before an upgrade, but it is titled "what changed for agents", lives under `plugins/hivemind/`, and is a three-column table whose cells run to 1,000 to 2,000 characters. A per-release heading with short "Agents", "Operators", "Plugin" and "Your copies" bullets would read far better. The `hivemind-update` skill reads this file and `tests/unit/test_agent_plugin.py` pins its rows, so the change needs the skill and test updated with it. The GitHub release pages already give a readable per-release view, so this is the lowest priority of the three.

## Checked and fine

- Root README walkthrough against the Makefile: ports (5432, 8000, 8001, 8080, 8081, 8088), the 512 local dimension, `make admin` and `make portal` on 8080 and 8081, `make up` on 8089 and 8090, `make pg-down` keeping data, `make mcp-http` using the local model.
- Admin panel and portal claims against the code, including "Since you last looked", locking a key, and the portal's own routes.
- `hivemind-incognito` arguments and `hivemind-as` profile file names against the scripts.
- code-tour.md: every path it names exists.
- CONTRIBUTING.md: the xdist worker count (at most eight) matches the Makefile.
- Voice: no em dashes in any README, CONTRIBUTING.md, SECURITY.md, the code tour or DEVELOPING.md.
