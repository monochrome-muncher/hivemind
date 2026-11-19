# The packages' skill copy drops the plugin-only lines

Amends [ADR 0162](0162-python-and-npm-client-packages.md) decision 6 (a byte-identical copy of the skill).

Status: Accepted. Asked for on 2026-11-19, after the review of the client packages (`docs/reviews/2026-11-19-client-packages-review.md`).

## Context

`instructions()` hands an app's agent the server instructions and the `hivemind` skill. The skill is written for the plugin: it tells the agent to offer the `hivemind-setup` skill, to follow its "Register" and "Key rejected" steps, to offer its "Stay aware" step, and how incognito sessions (a launcher, an environment variable, a reminder) work. An app built on the packages has none of these, so those lines point the agent at things it cannot do.

## Decision

1. `make sync-client-text` runs `clients/sync_text.py`, which copies the skill without §6 (incognito sessions) and rewords the five passages that name the plugin's setup skill or incognito into what an app's agent can do: tell the user, who needs an activated or new key.
2. Each passage it rewords must appear in the plugin's skill exactly once. When the skill changes under one, the script fails, and the passage is updated in the script, so a copy that still names the plugin is never shipped.
3. `tests/unit/test_client_packages.py` checks that each package's copy is what the script makes from the current skill, and that the copy names neither `hivemind-setup`, incognito nor the plugin.

## Consequences

- The skill keeps one source. A change to it still needs `make sync-client-text`, and now and then an update to the script.
- The copy is about 1 KB shorter.
