"""Input bounds and text rules shared by every surface (ADR 0040), so REST and MCP reject the same
inputs before any embedder or store call. Postgres ``text`` cannot hold U+0000; a ``tsvector`` tops
out at 1 MB."""

from __future__ import annotations

import json
import re
import unicodedata
import uuid
from collections.abc import Iterable
from datetime import UTC, datetime
from typing import Any


class InvalidInput(ValueError):
    """An input refused by rule (422 on REST, ``invalid_input`` on MCP). A ``ValueError`` so pure
    parse blocks still map it; around a service call surfaces catch ``InvalidInput`` itself."""


# -- pagination (SPEC §5.3) ---------------------------------------------------
MAX_LIMIT = 100
MAX_OFFSET = 10_000

# -- entry fields (SPEC §4.1) -------------------------------------------------
MAX_BODY_CHARS = 100_000
MAX_TAGS = 32
MAX_TAG_CHARS = 64
MAX_SOURCES = 32
MAX_SOURCE_REF_CHARS = 2_048
MAX_PAYLOAD_BYTES = 65_536
MAX_PAYLOAD_DEPTH = 32
# Caller timestamps stay in this range: asyncpg overflows on offsets that leave Postgres' own.
MIN_DATETIME = datetime(1900, 1, 1, tzinfo=UTC)
MAX_DATETIME = datetime(2200, 1, 1, tzinfo=UTC)
MAX_SUPERSEDES = 16
MAX_SEE_ALSO = 5  # entries one write may link to (ADR 0057)
MAX_PINS_PER_FLEET = 10  # a fleet's briefing stays short (ADR 0058)
MAX_PINS_PER_AGENT = 3  # pins one agent holds in its fleet (ADR 0096)
MAX_FEEDBACK_IDS = 16  # entries one batch feedback call may name (ADR 0053)
MAX_GET_IDS = 10  # entries one batch read may name (ADR 0055)
MAX_ID_CHARS = 64
MAX_IDENTITY_CHARS = 256  # author / agent / actor strings

# -- request text -------------------------------------------------------------
MAX_QUERY_CHARS = 2_000
MAX_QUERY_INSTRUCTION_CHARS = 500  # the optional embedder task line (ADR 0097)
MAX_FILTER_VALUE_CHARS = 256
MAX_FILTER_VALUES = 32
MAX_NOTE_CHARS = 2_000
MAX_REASON_CHARS = 2_000
MAX_ALIAS_CHARS = 128

# -- names (ADR 0040) ---------------------------------------------------------
MAX_AGENT_NAME_CHARS = 63
MAX_FLEET_NAME_CHARS = 128
AGENT_NAME_RE = re.compile(rf"[A-Za-z0-9][A-Za-z0-9._-]{{0,{MAX_AGENT_NAME_CHARS - 1}}}")
# ADR 0033: identities built-in keys and runners write under; an agent registered as one would
# read that identity's ``self`` entries.
RESERVED_AGENT_NAMES = frozenset({"admin", "org", "dev", "shared"})

# A whole REST body in bytes: above the worst legitimate write (all caps hit, escaped).
MAX_REQUEST_BODY_BYTES = 2 * 1024 * 1024


_SURROGATE_RE = re.compile("[\ud800-\udfff]")


def check_no_nul(value: str, field: str) -> str:
    """Reject what Postgres cannot store in ``text``/``jsonb``: U+0000 and lone surrogates."""
    if "\x00" in value:
        raise InvalidInput(f"{field} must not contain NUL (U+0000)")
    if _SURROGATE_RE.search(value):
        raise InvalidInput(f"{field} must not contain lone surrogate characters")
    return value


def check_text(value: str | None, field: str, max_chars: int) -> str | None:
    """Optional free text: no NUL, at most ``max_chars`` characters."""
    if value is None:
        return None
    check_no_nul(value, field)
    if len(value) > max_chars:
        raise InvalidInput(f"{field} must be at most {max_chars} characters (got {len(value)})")
    return value


def check_query(query: str) -> str:
    check_text(query, "query", MAX_QUERY_CHARS)
    return query


def check_query_instruction(instruction: str) -> str:
    """The task line before a search query for an instruction-aware embedder (ADR 0097): one
    line."""
    check_text(instruction, "query_instruction", MAX_QUERY_INSTRUCTION_CHARS)
    if "\n" in instruction or "\r" in instruction:
        raise InvalidInput("query_instruction must be a single line (no newlines)")
    return instruction


def check_pagination(limit: int | None, offset: int | None) -> None:
    """``1 <= limit <= MAX_LIMIT``, ``0 <= offset <= MAX_OFFSET`` (SPEC §5.3)."""
    if limit is not None and not 1 <= limit <= MAX_LIMIT:
        raise InvalidInput(f"limit must be between 1 and {MAX_LIMIT}")
    if offset is not None and not 0 <= offset <= MAX_OFFSET:
        raise InvalidInput(f"offset must be between 0 and {MAX_OFFSET}")


def check_datetime(value: datetime | None, field: str) -> None:
    """A caller timestamp must fall in ``[1900, 2200)`` UTC (naive means UTC)."""
    if value is None:
        return
    try:
        utc = value.astimezone(UTC) if value.tzinfo is not None else value.replace(tzinfo=UTC)
    except (OverflowError, ValueError) as exc:
        raise InvalidInput(f"{field} is out of range") from exc
    if not MIN_DATETIME <= utc < MAX_DATETIME:
        raise InvalidInput(f"{field} must be between the years 1900 and 2199")


def utcnow() -> datetime:
    return datetime.now(UTC)


# What asyncpg's ``uuid`` codec accepts; ``uuid.UUID`` is laxer and asyncpg answers the extras
# with a ``DataError``.
_UUID_TEXT_RE = re.compile(r"[0-9A-Fa-f-]{32,36}")


def is_valid_uuid(value: str) -> bool:
    """Whether ``value`` is a spelling the ``uuid`` columns accept. Both stores use it, so a refused
    spelling is "no such entry", never a 500; upper case and no hyphens name the entry."""
    if not isinstance(value, str) or not _UUID_TEXT_RE.fullmatch(value):
        return False
    try:
        uuid.UUID(value)
    except ValueError:
        return False
    return True


def canonical_uuid(value: str) -> str | None:
    """``value`` in Postgres's canonical ``uuid`` text form, or ``None``; compare ids read back
    from the database in this form."""
    try:
        return str(uuid.UUID(value))
    except ValueError, TypeError:
        return None


def check_id(value: str, field: str) -> str:
    check_text(value, field, MAX_ID_CHARS)
    return value


def check_filter_values(values: Iterable[str], field: str) -> None:
    items = tuple(values)
    if len(items) > MAX_FILTER_VALUES:
        raise InvalidInput(f"{field} must have at most {MAX_FILTER_VALUES} values")
    for item in items:
        check_text(item, field, MAX_FILTER_VALUE_CHARS)


def check_tags(tags: Iterable[str]) -> None:
    items = tuple(tags)
    if len(items) > MAX_TAGS:
        raise InvalidInput(f"at most {MAX_TAGS} tags (got {len(items)})")
    for tag in items:
        if not tag.strip():
            raise InvalidInput("tags must be non-blank")
        check_text(tag, "tag", MAX_TAG_CHARS)


def check_payload(payload: dict[str, Any] | None) -> None:
    """No NUL in any nested string, no NaN/Infinity, at most ``MAX_PAYLOAD_BYTES`` of JSON."""
    if payload is None:
        return
    stack: list[tuple[Any, int]] = [(payload, 1)]
    while stack:
        node, depth = stack.pop()
        if isinstance(node, str):
            check_no_nul(node, "payload string")
        elif isinstance(node, dict | list | tuple):
            if depth > MAX_PAYLOAD_DEPTH:
                raise InvalidInput(f"payload must be nested at most {MAX_PAYLOAD_DEPTH} levels")
            if isinstance(node, dict):
                for key, val in node.items():
                    check_no_nul(str(key), "payload key")
                    stack.append((val, depth + 1))
            else:
                stack.extend((item, depth + 1) for item in node)
    try:
        compact = json.dumps(payload, allow_nan=False, ensure_ascii=False, separators=(",", ":"))
        size = len(compact.encode())
    except ValueError as exc:  # NaN / Infinity
        raise InvalidInput("payload must be plain JSON (no NaN or Infinity)") from exc
    except (TypeError, RecursionError) as exc:
        raise InvalidInput("payload must be plain JSON") from exc
    if size > MAX_PAYLOAD_BYTES:
        raise InvalidInput(f"payload must be at most {MAX_PAYLOAD_BYTES} bytes of JSON")


# Invisible or display-deceiving format characters (Cf) banned in names: zero-width space, bidi
# controls, word joiner, BOM, soft hyphen. ZWNJ and ZWJ stay allowed: Persian, Indic and emoji need
# them.
_BANNED_FORMAT_CHARS = frozenset(
    "\u00ad\u061c\u180e\u200b\u200e\u200f\u2060\u2061\u2062\u2063\u2064\ufeff"
    + "".join(chr(c) for c in (*range(0x202A, 0x202F), *range(0x2066, 0x206A)))
)


def _has_hidden_chars(value: str) -> bool:
    """Control, surrogate, private-use and separator characters, plus the format characters
    above."""
    for ch in value:
        category = unicodedata.category(ch)
        if category in ("Cc", "Cs", "Co", "Zl", "Zp") or ch in _BANNED_FORMAT_CHARS:
            return True
    return False


def validate_agent_name(name: str) -> str:
    """An agent name (ADR 0040): ASCII, taken as given (no normalisation, so a lookalike is
    rejected, never mapped onto another name), and not reserved (case-insensitive)."""
    if not AGENT_NAME_RE.fullmatch(name):
        raise InvalidInput(
            "agent name must be 1-63 ASCII characters: letters, digits, '.', '_' or '-', "
            "starting with a letter or digit"
        )
    if name.casefold() in RESERVED_AGENT_NAMES:
        raise InvalidInput(
            f"agent name {name!r} is reserved for a built-in identity (ADR 0033); pick another"
        )
    return name


def validate_fleet_name(name: str) -> str:
    """A fleet name: non-blank display text within ``MAX_FLEET_NAME_CHARS``, no hidden
    characters."""
    if not name.strip():
        raise InvalidInput("fleet name must be non-blank")
    if len(name) > MAX_FLEET_NAME_CHARS:
        raise InvalidInput(f"fleet name must be at most {MAX_FLEET_NAME_CHARS} characters")
    if _has_hidden_chars(name):
        raise InvalidInput("fleet name must not contain control or zero-width characters")
    return name


def validate_owner_alias(alias: str | None) -> str | None:
    if alias is None:
        return None
    check_text(alias, "owner_alias", MAX_ALIAS_CHARS)
    if _has_hidden_chars(alias):
        raise InvalidInput("owner_alias must not contain control or zero-width characters")
    return alias
