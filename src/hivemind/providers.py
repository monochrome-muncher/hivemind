"""Shared plumbing for the embedder and extractor clients (ADR 0014, 0041).

* Errors carry the exception class name only: httpx messages can hold the ``Authorization`` header
  or URL userinfo.
* Transient failures (timeouts, network errors, 429, 5xx) retry with jittered backoff and a capped
  ``Retry-After``; other 4xx, redirects and config errors fail fast.
* One overall deadline per call, retries included.
"""

from __future__ import annotations

import asyncio
import logging
import random
import unicodedata
from collections.abc import Awaitable, Callable
from typing import Any

import httpx

logger = logging.getLogger(__name__)


# The longest a provider's Retry-After may make us wait between attempts.
MAX_RETRY_AFTER_SECONDS = 10.0

Sleep = Callable[[float], Awaitable[None]]


def clean_api_key(key: str) -> str:
    """Strip whitespace from an API key and reject control characters (not valid in a header).
    Errors never include the key."""
    key = key.strip()
    if not key.isascii():
        # httpx encodes headers as ASCII: a non-ASCII character would raise UnicodeEncodeError,
        # echoing it.
        raise ValueError("API key contains a non-ASCII character")
    if any(unicodedata.category(ch).startswith("C") for ch in key):
        raise ValueError("API key contains a control character")
    return key


def clean_endpoint(url: str) -> str:
    """Strip whitespace from an endpoint URL and reject inner whitespace or control characters.

    A mounted Secret's trailing newline otherwise surfaces as a raw InvalidURL. Errors never include
    the URL (userinfo)."""
    url = url.strip()
    if any(ch.isspace() or unicodedata.category(ch).startswith("C") for ch in url):
        raise ValueError("endpoint URL contains whitespace or a control character")
    return url


def default_deadline(timeout: float, retries: int, backoff: float = 0.5) -> float:
    """The default per-call budget: every attempt at full timeout plus every un-jittered backoff
    (DEPLOY.md §5 worst case)."""
    retries = max(0, retries)
    return float((retries + 1) * timeout + backoff * (2**retries - 1))


def _retry_after(response: httpx.Response) -> float | None:
    raw = response.headers.get("retry-after")
    if raw is None:
        return None
    try:
        seconds = float(raw.strip())
    except ValueError:
        return None  # an HTTP-date form: not worth parsing, use the backoff
    if seconds != seconds or seconds < 0:  # NaN / negative
        return None
    return min(seconds, MAX_RETRY_AFTER_SECONDS)


async def post_with_retries[E: Exception](
    client: httpx.AsyncClient,
    url: str,
    *,
    payload: dict[str, Any],
    headers: dict[str, str],
    retries: int,
    backoff: float,
    sleep: Sleep,
    deadline: float,
    label: str,
    error: Callable[[str, int | None], E],
    give_up_level: int = logging.ERROR,
    jitter: Callable[[], float] | None = None,
) -> httpx.Response:
    """POST ``payload`` and return the 2xx response; every failure raises the caller's
    ``error(message, status)``.

    Messages are safe to log and show: no headers, URLs or provider text.
    """
    rand = jitter if jitter is not None else random.random
    try:
        async with asyncio.timeout(deadline):
            return await _attempts(
                client,
                url,
                payload=payload,
                headers=headers,
                retries=max(0, retries),
                backoff=backoff,
                sleep=sleep,
                label=label,
                error=error,
                give_up_level=give_up_level,
                rand=rand,
            )
    except TimeoutError:
        raise error(f"{label} exceeded its {deadline:g}s overall deadline", None) from None


async def _attempts[E: Exception](
    client: httpx.AsyncClient,
    url: str,
    *,
    payload: dict[str, Any],
    headers: dict[str, str],
    retries: int,
    backoff: float,
    sleep: Sleep,
    label: str,
    error: Callable[[str, int | None], E],
    give_up_level: int,
    rand: Callable[[], float],
) -> httpx.Response:
    last: E | None = None
    retry_after: float | None = None
    for attempt in range(retries + 1):
        retry_after = None
        try:
            response = await client.post(url, json=payload, headers=headers)
        except (httpx.TimeoutException, httpx.NetworkError, httpx.RemoteProtocolError) as exc:
            # Transient. Class name only: never the message.
            last = error(f"{label} failed ({type(exc).__name__})", None)
        except (httpx.HTTPError, httpx.InvalidURL, UnicodeError) as exc:
            # Client-side / configuration failure: fail fast, class name only.
            raise error(f"{label} failed ({type(exc).__name__})", None) from None
        else:
            status = response.status_code
            if status >= 500 or status == 429:
                last = error(f"{label} returned {status}", status)
                retry_after = _retry_after(response)
            elif status >= 300:
                # 4xx, or a 3xx (httpx does not follow redirects).
                raise error(f"{label} returned {status}", status)
            else:
                return response
        if attempt < retries:
            logger.warning(
                "%s failed (attempt %d/%d), retrying: %s", label, attempt + 1, retries + 1, last
            )
            delay = backoff * (2**attempt) * (0.5 + 0.5 * rand())
            if retry_after is not None:
                delay = max(delay, retry_after)
            await sleep(delay)
    assert last is not None  # only reachable when the budget is exhausted
    logger.log(
        give_up_level, "%s failed after %d attempt(s), giving up: %s", label, retries + 1, last
    )
    raise last
