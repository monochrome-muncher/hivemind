Status note: amended by [ADR 0145](0145-move-a-fleet-memory-to-self.md). The admin, or the person behind an agent from the portal, can narrow an active fleet memory to `self`; only `scope` changes, the text and links stay, and the move is audited.

Status note: amended by [ADR 0125](0125-redact-a-withdrawn-memory.md). The rule stands for every use but one: an admin can redact a withdrawn memory that holds a leaked secret or personal data, which wipes its text, embedding and keyword index for good while the author, dates, supersession links, feedback and audit trail stay. It is admin-only, audited and irreversible.

Status note: amended by [ADR 0124](0124-admin-restores-a-withdrawn-memory.md). A withdrawal is no longer one-way: the admin can put one withdrawn memory back to active, with a reason (audited; pins are not restored).

# Append-only entries with explicit supersession

Entries are immutable after creation: no in-place edits, ever. Corrections happen only via explicit supersession (a new entry names the ones it replaces) or withdrawal. There is no automatic contradiction detection or arbitration in v1 — a supersession is a claim, and the reading agent judges it.

Considered options: mutable entries (edit in place); LLM-assisted automatic contradiction detection and auto-supersession (Caura's model). Rejected: mutable entries destroy the "who knew what, when" provenance trail that is the point of the system; automatic arbitration is a research-grade feature that, if wrong, silently corrupts the store. Explicit supersession keeps the store append-only, provenance intact, and the read side simple.

Consequences: a corrected fact exists as two entries (the original, hidden by default, plus the successor). Search must rank the successor above the superseded entry (see SPEC.md §Retrieval).