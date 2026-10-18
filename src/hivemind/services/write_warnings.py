"""Advisory warnings on a write (SPEC.md §4.1): things a writer probably
did not mean, computed from the draft and the targets the write path has
already loaded. They never block a write.
"""

from __future__ import annotations

import re
from collections.abc import Collection, Iterable, Sequence
from dataclasses import dataclass
from datetime import timedelta
from typing import TYPE_CHECKING

from hivemind.domain.entry import Entry, EntryDraft, Source
from hivemind.domain.health import is_entry_ref

if TYPE_CHECKING:
    from hivemind.services.write import RelatedEntry

# Consolidating entries whose newest observation is older than this, under
# a fresh ``occurred_at``, ranks the result as newer than its evidence.
_FRESH_CONSOLIDATION_GAP = timedelta(days=1)

# Ids named in one message, so a long supersedes list stays short.
_IDS_IN_MESSAGE = 3

# A related entry at or above this similarity is a near-duplicate signal
# (ADR 0087). Measured on Qwen3-Embedding-8B: 0.90 catches most versions of
# one fact and almost no distinct same-system facts, which 0.85 still
# catches. Tuned for Qwen3; advisory only, the writer judges.
_NEAR_DUPLICATE_SIMILARITY = 0.90

# A local path: a home directory (/home/x, /Users/x, ~/x) or a Windows
# profile (C:\Users\x, C:/Users/x, or JSON-escaped C:\\Users\\x). Not
# preceded by a word character or a path/URL character, so URLs
# (github.com/home/x, file:///home/x), repo-relative paths (src/home/x,
# ./home/x) and bare words do not match. The POSIX roots are
# case-sensitive, so API routes like GET /users/42 do not match; a
# lower-case /home/<name> is indistinguishable from a real home directory
# and does match. Windows drive letters and ``Users`` are case-tolerant.
_NOT_AFTER = r"(?<![\w.\-/:~\\])"
_SEP = r"(?:\\{1,2}|/)"
_LOCAL_PATH = re.compile(
    rf"{_NOT_AFTER}(?:/(?:home|Users)/[\w.\-]+|~/[\w.\-]+|[A-Za-z]:{_SEP}(?i:users){_SEP}[\w.\-]+)"
)


@dataclass(frozen=True, slots=True)
class WriteWarning:
    """An advisory note on a write: a stable ``code`` and a short action."""

    code: str
    message: str


def has_local_path(text: str) -> bool:
    """Whether ``text`` contains a machine-local path."""
    return _LOCAL_PATH.search(text) is not None


def is_entry_link(source: Source) -> bool:
    """Whether ``source`` points at another Hivemind entry, not the world."""
    return is_entry_ref(source.ref)


def near_duplicate_warning(
    related: Sequence[RelatedEntry], linked: Collection[str] = ()
) -> tuple[WriteWarning, ...]:
    """A near-duplicate signal: a related entry at or above the threshold.
    Entries in ``linked`` (the draft's ``see_also``) are skipped: the writer
    already said how they relate."""
    candidates = [r for r in related if r.entry.id not in linked]
    if not candidates:
        return ()
    top = max(candidates, key=lambda r: r.similarity)
    if top.similarity < _NEAR_DUPLICATE_SIMILARITY:
        return ()
    return (
        WriteWarning(
            "near_duplicate",
            f"very similar to {top.entry.id} (similarity {top.similarity:.2f}): "
            "if it says the same, rate it instead of writing; if yours updates "
            "it, supersede it; if it is another point, link it with see_also",
        ),
    )


def _ids(entries: Iterable[Entry]) -> str:
    ids = [e.id for e in entries]
    shown = ", ".join(ids[:_IDS_IN_MESSAGE])
    return shown + (f" (+{len(ids) - _IDS_IN_MESSAGE} more)" if len(ids) > _IDS_IN_MESSAGE else "")


def write_warnings(
    draft: EntryDraft,
    superseded: Iterable[Entry] = (),
    linked: Iterable[Entry] = (),
) -> tuple[WriteWarning, ...]:
    """The warnings for ``draft``, given the entries it supersedes and the
    ones it links with ``see_also`` (empty when not loaded)."""
    superseded, linked = tuple(superseded), tuple(linked)
    found: list[WriteWarning] = []

    if draft.scope in ("org", "fleet") and has_local_path(f"{draft.summary}\n{draft.body or ''}"):
        found.append(
            WriteWarning(
                "local_path",
                f"a {draft.scope} entry names a local path; make it repo-relative, "
                "or put the local detail in a self entry that links here",
            )
        )

    if draft.scope == "fleet":
        # Only see_also can reach another fleet's entry: may_supersede
        # already refuses a foreign supersession target.
        foreign = [e for e in linked if e.scope == "fleet" and e.fleet_id != draft.fleet_id]
        if foreign:
            found.append(
                WriteWarning(
                    "foreign_link",
                    f"links another fleet's entry ({_ids(foreign)}); restate what applies "
                    "here instead of relaying it, or write it as self",
                )
            )

    occurred = draft.resolved_occurred_at()
    backdated = [e for e in superseded if e.occurred_at > occurred]
    if backdated:
        found.append(
            WriteWarning(
                "backdated_successor",
                f"occurred_at is earlier than the entry it supersedes ({_ids(backdated)}); "
                "a successor should not predate its predecessor",
            )
        )

    if len(superseded) >= 2 and occurred - max(e.occurred_at for e in superseded) > (
        _FRESH_CONSOLIDATION_GAP
    ):
        found.append(
            WriteWarning(
                "fresh_consolidation",
                "occurred_at is more than a day after the newest superseded entry; if this "
                "restates them without new evidence, set occurred_at to the newest "
                "observation; keep today's date only if you observed or re-checked it today",
            )
        )

    # A synthesis grounded only in other entries: sources that all link to
    # entries, or none at all while it supersedes or links entries.
    if (
        draft.scope in ("org", "fleet")
        and draft.kind.value in ("insight", "decision")
        and all(is_entry_link(src) for src in draft.sources)
        and (draft.sources or draft.supersedes or draft.see_also)
    ):
        found.append(
            WriteWarning(
                "no_primary_source",
                "no source outside Hivemind entries; add the primary source "
                "(file, URL, session) the evidence came from, or say in 'body' how it was observed",
            )
        )

    kept = {(s.type, s.ref) for s in draft.sources}
    dropped = [
        e for e in superseded if e.sources and not kept & {(s.type, s.ref) for s in e.sources}
    ]
    if dropped:
        found.append(
            WriteWarning(
                "sources_dropped",
                f"drops the sources of {_ids(dropped)}; carry them forward in 'sources'",
            )
        )
    return tuple(found)
