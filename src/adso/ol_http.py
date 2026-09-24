"""Polite HTTP client shared by the enrichment fetchers (covers, metadata).

Open Library asks clients to identify themselves with a descriptive User-Agent;
the bounded timeouts and bounded 429 backoff exist because a scalar timeout plus
an unbounded retry loop once let an overnight fetch hang. Every enrichment
fetcher should go through :func:`request` so politeness stays in one place.
"""

from __future__ import annotations

import time

# A descriptive User-Agent is requested by Open Library so they can identify
# polite clients; see https://openlibrary.org/dev/docs/api/covers.
USER_AGENT = "Adso/0.1 (local-first book catalogue; +https://github.com/davidwhipps/adso)"
RATE_LIMIT_DELAY = 0.75

# (connect, read) timeouts: a stalled connection can never hang the whole run.
HTTP_TIMEOUT = (10, 30)


class EnrichmentHTTPError(RuntimeError):
    """A network-level failure reaching an enrichment source."""


def require_requests(error_cls: type[Exception] = EnrichmentHTTPError):
    try:
        import requests
    except ModuleNotFoundError as exc:  # pragma: no cover - exercised via extras
        raise error_cls(
            "Install the requests dependency before fetching enrichment data:\n"
            "    pip install -e '.[covers]'"
        ) from exc
    return requests


# Throttling (429) and transient server errors worth another try. Goodreads in
# particular answers a steady fraction of book-page requests with a 503 that
# succeeds on the next attempt.
RETRYABLE_STATUSES = frozenset({429, 500, 502, 503, 504})
MAX_ATTEMPTS = 3


def request(method: str, url: str, *, error_cls: type[Exception] = EnrichmentHTTPError, **kwargs):
    """HTTP wrapper with bounded timeouts and bounded retry/backoff.

    Both the connect/read timeout and the retry count (for 429 and transient
    5xx responses) are capped so that a slow, throttling or flaky host can never
    stall a sequential fetch. Network errors are raised as ``error_cls`` so each
    fetcher surfaces its own error type.
    """
    requests = require_requests(error_cls)
    headers = {"User-Agent": USER_AGENT, **kwargs.pop("headers", {})}
    kwargs.setdefault("timeout", HTTP_TIMEOUT)
    response = None
    for attempt in range(1, MAX_ATTEMPTS + 1):
        try:
            response = requests.request(method, url, headers=headers, **kwargs)
        except Exception as exc:  # noqa: BLE001 - network errors become a miss/error upstream
            raise error_cls(f"Could not reach {url}: {str(exc)[:300]}") from exc
        if response.status_code not in RETRYABLE_STATUSES:
            return response
        if attempt < MAX_ATTEMPTS:
            time.sleep(_retry_delay(response, attempt))
    return response  # still failing after retries -> treated as a miss upstream


def _retry_delay(response, attempt: int) -> float:
    """Honour a numeric Retry-After, else back off 1s, 2s, ...; never over 5s."""
    try:
        delay = float(response.headers.get("Retry-After") or attempt)
    except (TypeError, ValueError):  # e.g. an HTTP-date Retry-After
        delay = attempt
    return min(max(delay, 0), 5)
