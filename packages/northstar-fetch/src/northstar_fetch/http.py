"""Shared HTTP machinery: rate limiting, retry, and OAuth token management.

Providers here have real and differing constraints. NSRDB blocks a key for an
hour when its daily request ceiling is exceeded. ERCOT issues bearer tokens that
expire after 3600 seconds, which is shorter than a multi-year fetch. Both are
routine operating conditions rather than exceptional ones, so they are handled
here instead of being raised to the caller.

Reference: design document ``19_external_data_acquisition`` sections 4.1, 4.4
and 7.
"""

from __future__ import annotations

import logging
import random
import time
from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Any

import requests

LOGGER = logging.getLogger(__name__)

# HTTP statuses worth retrying: explicit rate limiting plus transient
# server-side faults. Anything else is a client error and retrying will not help.
RETRYABLE_STATUS = frozenset({429, 500, 502, 503, 504})


def _retry_after_seconds(response) -> float | None:
    """Read a ``Retry-After`` header, if the server sent one.

    The header may be a delay in seconds or an HTTP date. Only the numeric form
    is honoured; a date is ignored rather than parsed, because a wrong parse
    would produce a worse delay than the exponential fallback.

    Args:
        response: The HTTP response.

    Returns:
        Seconds to wait, or ``None`` when absent or unparseable.
    """
    raw = getattr(response, "headers", {}).get("Retry-After")
    if raw is None:
        return None
    try:
        seconds = float(raw)
    except (TypeError, ValueError):
        return None
    # Cap it: a server asking for an hour should not silently stall a backfill.
    return min(max(seconds, 0.0), 300.0)


class RateLimitError(RuntimeError):
    """Raised when the client-side daily request ceiling is reached.

    Distinct from a provider 429 because it is self-imposed: the run should stop
    cleanly and resume later rather than risk the provider blocking the key.
    """


class FetchError(RuntimeError):
    """Raised when a request fails permanently after exhausting retries."""


class RateLimiter:
    """Enforces a minimum request interval and a daily request ceiling.

    The ceiling is deliberately set below the provider's published limit so that
    a run cannot exhaust the key and trigger a block that would delay the next
    run by an hour.
    """

    def __init__(self, *, min_interval_s: float, max_requests_per_day: int) -> None:
        """Configure the limiter.

        Args:
            min_interval_s: Minimum wall-clock spacing between requests.
            max_requests_per_day: Client-side ceiling on request count.
        """
        self.min_interval_s = min_interval_s
        self.max_requests_per_day = max_requests_per_day
        self._last_request_at: float | None = None
        self._count = 0

    @property
    def request_count(self) -> int:
        """Number of requests issued since the limiter was created.

        Returns:
            The running request count.
        """
        return self._count

    def acquire(self) -> None:
        """Block until another request may be issued.

        Raises:
            RateLimitError: If the daily ceiling has already been reached.
        """
        if self._count >= self.max_requests_per_day:
            raise RateLimitError(
                f"client-side ceiling of {self.max_requests_per_day} requests "
                "reached; resume the fetch in the next window"
            )
        if self._last_request_at is not None:
            elapsed = time.monotonic() - self._last_request_at
            remaining = self.min_interval_s - elapsed
            if remaining > 0:
                time.sleep(remaining)
        self._last_request_at = time.monotonic()
        self._count += 1


@dataclass
class RetryPolicy:
    """Exponential backoff with jitter.

    Jitter matters when several partitions fail together, which is the normal
    shape of a rate-limit event: without it, every retry lands simultaneously and
    trips the limit again.

    Attributes:
        max_attempts: Total attempts including the first.
        base_delay_s: Delay before the first retry.
        max_delay_s: Ceiling on any single delay.
        jitter: Fractional random spread applied to each delay.
    """

    # ERCOT throttles for longer than five exponential attempts from a 2s base
    # can outlast: that sequence gives up after roughly 60 seconds, and a real
    # backfill saw 18 partitions abandoned to HTTP 429. Eight attempts reach
    # about four minutes, which clears it.
    max_attempts: int = 8
    base_delay_s: float = 2.0
    max_delay_s: float = 3600.0
    jitter: float = 0.25

    def delay_for(self, attempt: int) -> float:
        """Compute the delay before a given retry attempt.

        Args:
            attempt: One-based index of the retry about to be made.

        Returns:
            Delay in seconds, capped and jittered.
        """
        raw = min(self.base_delay_s * (2 ** (attempt - 1)), self.max_delay_s)
        spread = raw * self.jitter
        return max(0.0, raw + random.uniform(-spread, spread))


class HttpClient:
    """Requests session wrapper applying rate limiting and retry.

    Args:
        rate_limiter: Limiter governing request pacing and volume.
        retry_policy: Backoff configuration.
        timeout_s: Per-request timeout.
        session: Optional session to reuse. Injectable so tests can supply a
            stub without patching module globals.
    """

    def __init__(
        self,
        *,
        rate_limiter: RateLimiter,
        retry_policy: RetryPolicy | None = None,
        timeout_s: int = 60,
        session: Any | None = None,
    ) -> None:
        """Initialise the client."""
        self.rate_limiter = rate_limiter
        self.retry_policy = retry_policy or RetryPolicy()
        self.timeout_s = timeout_s
        self.session = session or requests.Session()

    def get(
        self,
        url: str,
        *,
        params: dict[str, Any] | None = None,
        headers: dict[str, str] | None = None,
        on_unauthorized: Callable[[], dict[str, str]] | None = None,
    ) -> Any:
        """Issue a GET request, retrying transient failures.

        Args:
            url: Absolute request URL.
            params: Query parameters.
            headers: Request headers.
            on_unauthorized: Callback invoked on a 401, expected to refresh
                credentials and return replacement headers. Used for ERCOT
                bearer tokens, which expire mid-fetch as a matter of course.

        Returns:
            The successful response object.

        Raises:
            FetchError: If every attempt fails.
            RateLimitError: If the client-side ceiling is reached.
        """
        active_headers = dict(headers or {})
        last_detail = "no attempts made"

        for attempt in range(1, self.retry_policy.max_attempts + 1):
            self.rate_limiter.acquire()
            try:
                response = self.session.get(
                    url,
                    params=params,
                    headers=active_headers,
                    timeout=self.timeout_s,
                )
            except requests.RequestException as error:
                retry_after = None
                last_detail = f"transport error: {error}"
                LOGGER.warning("%s attempt %d: %s", url, attempt, last_detail)
            else:
                status = response.status_code
                retry_after = None
                if status == 401 and on_unauthorized is not None:
                    LOGGER.info("token rejected, refreshing and retrying")
                    active_headers.update(on_unauthorized())
                    continue
                if 300 <= status < 400:
                    # `requests` follows redirects by default, so a 3xx
                    # arriving here means the redirect was not followed - a
                    # relative Location, a cross-host hop, or an auth challenge.
                    # Returning it as success hands a redirect body to
                    # `response.json()`, which fails with a JSON decode error
                    # that says nothing about the real cause. Observed as an
                    # HTTP 302 against ERCOT.
                    raise FetchError(
                        f"{url}: HTTP {status} redirect to "
                        f"{response.headers.get('Location', '(no Location header)')} "
                        "- not followed"
                    )
                if status < 300:
                    return response
                last_detail = f"HTTP {status}"
                if status not in RETRYABLE_STATUS:
                    raise FetchError(f"{url}: {last_detail} (not retryable)")
                # A 429 usually carries Retry-After. Guessing a backoff when
                # the server has stated the answer is needless.
                retry_after = _retry_after_seconds(response)
                LOGGER.warning(
                    "%s attempt %d: %s%s",
                    url,
                    attempt,
                    last_detail,
                    f" (Retry-After {retry_after:.0f}s)" if retry_after else "",
                )

            if attempt < self.retry_policy.max_attempts:
                delay = retry_after or self.retry_policy.delay_for(attempt)
                LOGGER.info("backing off %.1fs before retry", delay)
                time.sleep(delay)

        raise FetchError(
            f"{url}: failed after {self.retry_policy.max_attempts} attempts "
            f"({last_detail})"
        )


class ErcotTokenManager:
    """Obtains and refreshes ERCOT Public API bearer tokens.

    ERCOT authenticates through an Azure B2C resource-owner password credentials
    flow. Tokens live for 3600 seconds, which is shorter than a multi-year price
    fetch, so transparent refresh is a functional requirement rather than an
    optimisation.
    """

    TOKEN_URL = (
        "https://ercotb2c.b2clogin.com/ercotb2c.onmicrosoft.com/"
        "B2C_1_PUBAPI-ROPC-FLOW/oauth2/v2.0/token"
    )
    # Refreshed this far before nominal expiry so a long request cannot straddle
    # the boundary and fail mid-flight.
    REFRESH_MARGIN_S = 300

    def __init__(
        self,
        *,
        username: str,
        password: str,
        subscription_key: str,
        client_id: str,
        session: Any | None = None,
        timeout_s: int = 60,
    ) -> None:
        """Configure the token manager.

        Args:
            username: ERCOT account username.
            password: ERCOT account password.
            subscription_key: API subscription key sent on every data request.
            client_id: B2C application client identifier.
            session: Optional injectable session for testing.
            timeout_s: Token request timeout.
        """
        self.username = username
        self.password = password
        self.subscription_key = subscription_key
        self.client_id = client_id
        self.session = session or requests.Session()
        self.timeout_s = timeout_s
        self._token: str | None = None
        self._expires_at: datetime | None = None

    def _is_expired(self) -> bool:
        """Determine whether the cached token needs replacing.

        Returns:
            ``True`` when no token is held, or when it expires within the
            refresh margin.
        """
        if self._token is None or self._expires_at is None:
            return True
        margin = timedelta(seconds=self.REFRESH_MARGIN_S)
        return datetime.now(UTC) + margin >= self._expires_at

    def refresh(self) -> str:
        """Acquire a new bearer token unconditionally.

        Returns:
            The new access token.

        Raises:
            FetchError: If the token endpoint rejects the request or returns a
                payload without an access token.
        """
        payload = {
            "grant_type": "password",
            "username": self.username,
            "password": self.password,
            "scope": f"openid {self.client_id} offline_access",
            "client_id": self.client_id,
            "response_type": "id_token",
        }
        response = self.session.post(self.TOKEN_URL, data=payload, timeout=self.timeout_s)
        if response.status_code >= 400:
            raise FetchError(f"ERCOT token request failed: HTTP {response.status_code}")
        body = response.json()
        token = body.get("access_token") or body.get("id_token")
        if not token:
            raise FetchError("ERCOT token response contained no token")
        lifetime = int(body.get("expires_in", 3600))
        self._token = token
        self._expires_at = datetime.now(UTC) + timedelta(seconds=lifetime)
        LOGGER.info("ERCOT token acquired, expires in %ds", lifetime)
        return token

    def token(self) -> str:
        """Return a valid token, refreshing if necessary.

        Returns:
            A bearer token that is valid for at least the refresh margin.
        """
        if self._is_expired():
            return self.refresh()
        assert self._token is not None
        return self._token

    def headers(self) -> dict[str, str]:
        """Build the authentication headers for a data request.

        Returns:
            Bearer authorization plus the subscription key header that the
            ERCOT gateway requires alongside it.
        """
        return {
            "Authorization": f"Bearer {self.token()}",
            "Ocp-Apim-Subscription-Key": self.subscription_key,
        }

    def refresh_headers(self) -> dict[str, str]:
        """Force a token refresh and return fresh headers.

        Suitable as the ``on_unauthorized`` callback for :meth:`HttpClient.get`.

        Returns:
            Headers built from a newly acquired token.
        """
        self.refresh()
        return self.headers()
