"""Input bounds and text rules shared by every surface (ADR 0040).

One validator set, used by the domain (``EntryDraft`` / ``EntryFilters``),
the services (search, governance, access) and, for the numeric bounds,
the REST schemas — so REST and MCP reject the same inputs for the same
reasons, **before** any embedder or store call. Pure: no I/O.

Why these exist: Postgres ``text`` cannot hold U+0000 (asyncpg raises, the
client saw a bare 500), a ``tsvector`` tops out at 1 MB, and nothing else
bounded field sizes, ``limit`` or ``offset``.
"""

from __future__ import annotations

import json
import re
import unicodedata
from collections.abc import Iterable
from typing import Any


class InvalidInput(ValueError):
    """An input the service refuses by rule (422 on REST, ``invalid_input``
    on MCP). A ``ValueError`` so existing ``except ValueError`` seams keep
    mapping it; surfaces catch it *first* where ``ValueError`` means
    something else (a conflict, a status)."""


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
MAX_SUPERSEDES = 16
MAX_ID_CHARS = 64
MAX_IDENTITY_CHARS = 256  # author / agent / actor strings

# -- request text -------------------------------------------------------------
MAX_QUERY_CHARS = 2_000
MAX_FILTER_VALUE_CHARS = 256
MAX_FILTER_VALUES = 32
MAX_NOTE_CHARS = 2_000
MAX_REASON_CHARS = 2_000
MAX_ALIAS_CHARS = 128

# -- names (ADR 0040) ---------------------------------------------------------
MAX_AGENT_NAME_CHARS = 63
MAX_FLEET_NAME_CHARS = 128
AGENT_NAME_RE = re.compile(r"[A-Za-z0-9][A-Za-z0-9._-]{0,62}")
# ADR 0033: the identities built-in keys and runners write under (admin
# keys, the org key, the dev runner, the mcp-http template credential).
# An agent registered under one would read that identity's ``self`` entries.
RESERVED_AGENT_NAMES = frozenset({"admin", "org", "dev", "shared"})

# A whole REST request body (bytes, JSON escapes included): above the worst
# legitimate write (body + payload + sources at their caps, escaped).
MAX_REQUEST_BODY_BYTES = 2 * 1024 * 1024


def check_no_nul(value: str, field: str) -> str:
    if "\x00" in value:
        raise InvalidInput(f"{field} must not contain NUL (U+0000)")
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


def check_pagination(limit: int | None, offset: int | None) -> None:
    """``1 <= limit <= MAX_LIMIT``, ``0 <= offset <= MAX_OFFSET`` (SPEC §5.3)."""
    if limit is not None and not 1 <= limit <= MAX_LIMIT:
        raise InvalidInput(f"limit must be between 1 and {MAX_LIMIT}")
    if offset is not None and not 0 <= offset <= MAX_OFFSET:
        raise InvalidInput(f"offset must be between 0 and {MAX_OFFSET}")


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
    """No NUL in any string (keys and values, nested), no NaN/Infinity,
    and at most ``MAX_PAYLOAD_BYTES`` of serialised JSON."""
    if payload is None:
        return
    stack: list[Any] = [payload]
    while stack:
        node = stack.pop()
        if isinstance(node, str):
            check_no_nul(node, "payload string")
        elif isinstance(node, dict):
            for key, val in node.items():
                check_no_nul(str(key), "payload key")
                stack.append(val)
        elif isinstance(node, list | tuple):
            stack.extend(node)
    try:
        size = len(json.dumps(payload, allow_nan=False, ensure_ascii=False).encode())
    except ValueError as exc:  # NaN / Infinity
        raise InvalidInput("payload must be plain JSON (no NaN or Infinity)") from exc
    except (TypeError, RecursionError) as exc:
        raise InvalidInput("payload must be plain JSON") from exc
    if size > MAX_PAYLOAD_BYTES:
        raise InvalidInput(f"payload must be at most {MAX_PAYLOAD_BYTES} bytes of JSON")


def _has_hidden_chars(value: str) -> bool:
    """Control (Cc), format / zero-width (Cf), surrogate, private-use or
    unassigned characters; the line/paragraph separators too."""
    return any(
        unicodedata.category(ch)[0] == "C" or unicodedata.category(ch) in ("Zl", "Zp")
        for ch in value
    )


def validate_agent_name(name: str) -> str:
    """An agent name (ADR 0040): ASCII ``[A-Za-z0-9][A-Za-z0-9._-]{0,62}``,
    taken **as given** (no normalisation, so a lookalike is rejected, never
    silently mapped onto another name), and not reserved under a
    case-insensitive comparison. Returns ``name``."""
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
    """A fleet name is free-form display text: non-blank, at most
    ``MAX_FLEET_NAME_CHARS``, with no control, zero-width or NUL characters."""
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
