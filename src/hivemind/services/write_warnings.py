"""Advisory warnings on a write (SPEC.md §4.1), computed from the draft and the targets already
loaded. They never block a write."""

from __future__ import annotations

import re
from collections.abc import Collection, Iterable, Sequence
from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import TYPE_CHECKING

from hivemind.domain.access import SCOPE_FLEET, SCOPE_ORG
from hivemind.domain.entry import Entry, EntryDraft, EntryState, Source
from hivemind.domain.health import is_entry_ref

if TYPE_CHECKING:
    from hivemind.services.write import RelatedEntry

# A fresh ``occurred_at`` this long after the newest superseded observation ranks a consolidation
# as newer than its evidence.
_FRESH_CONSOLIDATION_GAP = timedelta(days=1)

# Ids named in one message.
_IDS_IN_MESSAGE = 3

# Near-duplicate threshold (ADR 0087). Measured on Qwen3-Embedding-8B: 0.90 catches most versions
# of one fact and almost no distinct same-system facts, which 0.85 still catches. Tuned for Qwen3.
NEAR_DUPLICATE_SIMILARITY = 0.90

# Open-question threshold (ADR 0099). Provisional: to be checked against Qwen3 measurements.
_OPEN_QUESTION_SIMILARITY = 0.70

# Tags that mark a gap or surprise waiting for an answer.
_OPEN_TAGS = frozenset({"open-question", "anomaly"})

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


# Common function words of English and of the languages agents most often write in instead, minus
# any that are also English words, abbreviations, commands or locale codes ("die", "per", "du", "de").
# A text needs several of them, outnumbering English ones two to one, so terse English summaries
# full of names and English text quoting a foreign error stay quiet.
_FUNCTION_WORDS = {
    "english": "the and is are was were of to in on for with by from that this it not be has have "
    "a an when after before because but or",
    "german": "der das und nicht ist mit auf für eine einen einem dem des sich wird wurde werden auch "
    "oder aber wenn nach bei durch zwischen statt über sind weil dass noch kein keine zum zur vom "
    "beim wir nur",
    "french": "le la les une est avec dans sur qui que sont au aux mais cette pas parce très sans "
    "être nous ces leur été",
    "spanish": "el los las una para por pero como está porque también muy cuando fue",
    "italian": "il gli della delle che sono nel alla è",
    "portuguese": "uma não são",
    "dutch": "het een niet voor zijn wordt werd maar ook dat",
    "scandinavian": "og ikke er på av til som det den med ble har etter jeg fordi nå når fra også "
    "eller hvis blir være denne dette vår efter och inte är att för ett från när också",
}
_ENGLISH_FUNCTION_WORDS = frozenset(_FUNCTION_WORDS["english"].split())
_FOREIGN_FUNCTION_WORDS = frozenset(
    word
    for language, words in _FUNCTION_WORDS.items()
    if language != "english"
    for word in words.split()
)
_MIN_FOREIGN_WORDS = 3
_MIN_NON_LATIN_LETTERS = 20
_LATIN_END = "\u0250"
_WORDS = re.compile(r"[^\W\d_]+")


@dataclass(frozen=True, slots=True)
class WriteWarning:
    """An advisory note on a write: a stable ``code`` and a short action."""

    code: str
    message: str


def has_local_path(text: str) -> bool:
    """Whether ``text`` contains a machine-local path."""
    return _LOCAL_PATH.search(text) is not None


def looks_non_english(text: str) -> bool:
    """Whether ``text`` is written in another language than English: mostly non-Latin letters, or
    another language's function words outnumbering English ones."""
    words = [w.lower() for w in _WORDS.findall(text)]
    letters = sum(len(w) for w in words)
    latin = sum(ch < _LATIN_END for w in words for ch in w)
    if letters >= _MIN_NON_LATIN_LETTERS and latin * 2 < letters:
        return True
    foreign = sum(w in _FOREIGN_FUNCTION_WORDS for w in words)
    english = sum(w in _ENGLISH_FUNCTION_WORDS for w in words)
    return foreign >= _MIN_FOREIGN_WORDS and foreign > 2 * english


def is_entry_link(source: Source) -> bool:
    """Whether ``source`` points at another Hivemind entry, not the world."""
    return is_entry_ref(source.ref)


def near_duplicate_warning(
    related: Sequence[RelatedEntry], linked: Collection[str] = ()
) -> tuple[WriteWarning, ...]:
    """A related entry at or above the threshold; ``linked`` entries (``see_also``) are skipped."""
    candidates = [r for r in related if r.entry.id not in linked]
    if not candidates:
        return ()
    top = max(candidates, key=lambda r: r.similarity)
    if top.similarity < NEAR_DUPLICATE_SIMILARITY:
        return ()
    return (
        WriteWarning(
            "near_duplicate",
            f"very similar to {top.entry.id} (similarity {top.similarity:.2f}): "
            "if it says the same, rate it instead of writing; if yours updates "
            "it, supersede it; if it is another point, link it with see_also",
        ),
    )


def _id_key(entry_id: str) -> str:
    """An entry id with case and hyphens folded away (uuid spellings)."""
    return entry_id.replace("-", "").lower()


def answers_open_question_warning(
    related: Sequence[RelatedEntry],
    draft_tags: Collection[str] = (),
    linked: Collection[str] = (),
) -> tuple[WriteWarning, ...]:
    """The write may answer an active ``related`` ``open-question`` or ``anomaly`` entry at or above
    the floor. ``linked`` entries are skipped, and a draft tagged so itself gets none (two questions
    are not an answer). At most one warning, naming the closest."""
    if _OPEN_TAGS & set(draft_tags):
        return ()
    skip = {_id_key(i) for i in linked}
    candidates = [
        r
        for r in related
        if r.entry.state is EntryState.ACTIVE
        and _OPEN_TAGS & set(r.entry.tags)
        and r.similarity >= _OPEN_QUESTION_SIMILARITY
        and _id_key(r.entry.id) not in skip
    ]
    if not candidates:
        return ()
    top = max(candidates, key=lambda r: r.similarity)
    others = len(candidates) - 1
    more = f" (+{others} more nearby)" if others else ""
    return (
        WriteWarning(
            "answers_open_question",
            f"may answer open question {top.entry.id} (similarity {top.similarity:.2f}){more}: "
            "if it does, write with supersedes (if you may) or see_also to it",
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
    """The warnings for ``draft``, given the entries it supersedes and links (empty if not
    loaded)."""
    superseded, linked = tuple(superseded), tuple(linked)
    occurred = draft.resolved_occurred_at()
    found = (
        _local_path_warning(draft),
        _not_english_warning(draft),
        _foreign_link_warning(draft, linked),
        _backdated_successor_warning(occurred, superseded),
        _fresh_consolidation_warning(occurred, superseded),
        _no_primary_source_warning(draft),
        _sources_dropped_warning(draft, superseded),
    )
    return tuple(w for w in found if w is not None)


def _local_path_warning(draft: EntryDraft) -> WriteWarning | None:
    if draft.scope in (SCOPE_ORG, SCOPE_FLEET) and has_local_path(
        f"{draft.summary}\n{draft.body or ''}"
    ):
        return WriteWarning(
            "local_path",
            f"a {draft.scope} entry names a local path; make it repo-relative, "
            "or put the local detail in a self entry that links here",
        )
    return None


def _not_english_warning(draft: EntryDraft) -> WriteWarning | None:
    if not looks_non_english(f"{draft.summary}\n{draft.body or ''}"):
        return None
    return WriteWarning(
        "not_english",
        "not in English: write it again in English (names, quotes and errors stay as they are), "
        "with 'supersedes' naming this entry if it was stored. "
        "Keep answering your user in the language they wrote in",
    )


def _foreign_link_warning(draft: EntryDraft, linked: Sequence[Entry]) -> WriteWarning | None:
    if draft.scope != SCOPE_FLEET:
        return None
    # Only see_also can reach another fleet's entry: ``may_supersede`` refuses a foreign target.
    foreign = [e for e in linked if e.scope == SCOPE_FLEET and e.fleet_id != draft.fleet_id]
    if not foreign:
        return None
    return WriteWarning(
        "foreign_link",
        f"links another fleet's entry ({_ids(foreign)}); restate what applies "
        "here instead of relaying it, or write it as self",
    )


def _backdated_successor_warning(
    occurred: datetime, superseded: Sequence[Entry]
) -> WriteWarning | None:
    backdated = [e for e in superseded if e.occurred_at > occurred]
    if not backdated:
        return None
    return WriteWarning(
        "backdated_successor",
        f"occurred_at is earlier than the entry it supersedes ({_ids(backdated)}); "
        "a successor should not predate its predecessor",
    )


def _fresh_consolidation_warning(
    occurred: datetime, superseded: Sequence[Entry]
) -> WriteWarning | None:
    if len(superseded) >= 2 and occurred - max(e.occurred_at for e in superseded) > (
        _FRESH_CONSOLIDATION_GAP
    ):
        return WriteWarning(
            "fresh_consolidation",
            "occurred_at is more than a day after the newest superseded entry; if this "
            "restates them without new evidence, set occurred_at to the newest "
            "observation; keep today's date only if you observed or re-checked it today",
        )
    return None


def _no_primary_source_warning(draft: EntryDraft) -> WriteWarning | None:
    # A synthesis grounded only in other entries.
    if (
        draft.scope in (SCOPE_ORG, SCOPE_FLEET)
        and draft.kind.value in ("insight", "decision")
        and all(is_entry_link(src) for src in draft.sources)
        and (draft.sources or draft.supersedes or draft.see_also)
    ):
        return WriteWarning(
            "no_primary_source",
            "no source outside Hivemind entries; add the primary source "
            "(file, URL, session) the evidence came from, or say in 'body' how it was observed",
        )
    return None


def _sources_dropped_warning(draft: EntryDraft, superseded: Sequence[Entry]) -> WriteWarning | None:
    kept = {(s.type, s.ref) for s in draft.sources}
    dropped = [
        e for e in superseded if e.sources and not kept & {(s.type, s.ref) for s in e.sources}
    ]
    if not dropped:
        return None
    return WriteWarning(
        "sources_dropped",
        f"drops the sources of {_ids(dropped)}; carry them forward in 'sources'",
    )
