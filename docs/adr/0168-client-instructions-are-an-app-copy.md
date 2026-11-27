# The packages ship their own copy of the server instructions

Amends [ADR 0162](0162-python-and-npm-client-packages.md) decision 2 (`instructions()` is the server's `instructions` from the handshake) and extends [ADR 0163](0163-client-skill-copy-drops-plugin-only-lines.md) to the server text.

Status: Accepted. Asked for on 2026-11-27.

## Context

`instructions()` returned the server instructions as the handshake sent them, then the app copy of the skill. The server instructions tell an agent whose key shows `key_kind: "org"` to "follow the hivemind-setup skill". An app has no such skill, and the skill copy right after it says the opposite: tell the user that the app needs an activated agent key. ADR 0163 reworded the skill and missed the server text.

The text was also only there after connecting. Frameworks that open their own MCP connection had to connect once with the package just to read it, and many of them never show the server instructions to the model.

## Decision

1. `make sync-client-text` also copies `contract.INSTRUCTIONS` into each package as `instructions.txt`, with the `hivemind-setup` clause replaced by the app wording. The clause must appear exactly once, or the script fails.
2. `instructions()` returns that copy, then the skill. It no longer reads the handshake's text. The package and the server move in lockstep (ADR 0162 decision 8), so the copy is the text of the server of the same version.
3. The same string is exported as `INSTRUCTIONS` in both packages, next to `REMINDER`, so it can be read without a connection.
4. `tests/unit/test_client_packages.py` checks that each copy is what the script makes from the current server text, and that it does not name `hivemind-setup`.

## Consequences

- An app's agent gets one rule for an org key, not two.
- A client talking to a newer server keeps the instructions of its own version until it is upgraded, as it already did for the skill.
