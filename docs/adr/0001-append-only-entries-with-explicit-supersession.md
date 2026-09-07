# Append-only entries with explicit supersession

Entries are immutable after creation: no in-place edits, ever. Corrections happen only via explicit supersession (a new entry names the ones it replaces) or withdrawal. There is no automatic contradiction detection or arbitration in v1 — a supersession is a claim, and the reading agent judges it.

Considered options: mutable entries (edit in place); LLM-assisted automatic contradiction detection and auto-supersession (Caura's model). Rejected: mutable entries destroy the "who knew what, when" provenance trail that is the point of the system; automatic arbitration is a research-grade feature that, if wrong, silently corrupts the store. Explicit supersession keeps the store append-only, provenance intact, and the read side simple.

Consequences: a corrected fact exists as two entries (the original, hidden by default, plus the successor). Search must rank the successor above the superseded entry (see SPEC.md §Retrieval).