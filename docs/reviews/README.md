# Reviews

Security, performance and design reviews of Hivemind, kept as a record of what was checked, what was found and what was fixed. Each one names the commit it read, so line numbers point into that commit, not into today's code. The fixes they mention are in the linked pull requests and, where a decision was needed, in an [ADR](../adr/README.md).

| Date | Review | Read against |
|---|---|---|
| 2026-10-01 | [Security and performance review of main](2026-10-01-main-review.md) | v2.0.2 |
| 2026-10-01 | [Security and performance review of v2.1.0](2026-10-01-v2.1.0-review.md) | v2.1.0 |
| 2026-10-01 | [Plugins and skills review](2026-10-01-plugins-skills-review.md): install steps checked live per harness, and the case for a shorter skill | c2aeb3e |
| 2026-10-02 | [Security and performance review](2026-10-02-full-review.md) | v2.3.1 |
| 2026-10-02 | [Architecture and maintainability review](2026-10-02-architecture-review.md) | v2.3.2 |
| 2026-10-02 | [Minor-fixes sweep](2026-10-02-minor-fixes-sweep.md) | v2.3.3 |
| 2026-10-10 | [Feedback system review](2026-10-10-feedback-system-review.md), including why Elo does not fit | fc9f387 |
| 2026-10-10 | [Index measurements](2026-10-10-index-measurements.md) behind migration 0013 | v2.4.0 |
| 2026-10-11 | [Post-2.5.0 review](2026-10-11-post-2.5.0-review.md) | after v2.5.0 |
| 2026-11-11 | [Changes since 2.19.0](2026-11-11-changes-since-2.19.0.md): the readability refactor checked for behaviour changes, and every ADR reference in the shortened comments | v2.20.0 |
| 2026-11-11 | [Changes from 2.21.0 to 2.23.0](2026-11-11-changes-2.21.0-to-2.23.0.md): the update skill, re-extraction and the `not_english` warning, with a fix for its false alarms | v2.23.0 |
| 2026-11-12 | [Changes since 2.23.1](2026-11-12-changes-since-2.23.1.md): the admin panel and portal features of 2.24.0 (ADRs 0135 to 0141) | b627590 |
| 2026-11-12 | [Memories as data, relevance, and self versus fleet](2026-11-12-memories-as-data-and-scope.md): how agents are told to weigh entries, what the search score means, why brainstorms reach the fleet, and a live check of [ADR 0146](../adr/0146-scope-by-evidence.md) | 88c4170 |

Retrieval measurements live in [retrieval-experiments.md](../retrieval-experiments.md), and research proposals in [proposals/](../proposals/).
