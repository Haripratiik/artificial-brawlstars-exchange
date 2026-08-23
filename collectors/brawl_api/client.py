"""A polite, rate-limited client for the official Brawl Stars API.

Three things this client takes seriously, because each is a way an unattended
crawler quietly dies:

**Rate limiting.** The documented ceiling is around ten requests per second per
key. We default well under it. A token bucket paces requests rather than a
fixed sleep, so bursts after an idle stretch are still bounded and steady-state
throughput does not depend on how long each response happened to take.

**The 403.** By far the most common failure mode is not a bug in this code: it
is the API key being bound to a different IP than the machine is calling from.
Supercell keys are IP-locked, and a laptop on a dynamic address will start
failing the moment the address changes. A bare 403 is a baffling error to debug
at 3am, so it is caught and re-raised with the actual explanation.

**Backoff.** 429s and 5xx are retried with exponential backoff and jitter;
404s are not, because a player tag that no longer exists will never exist again
and retrying it wastes budget for the whole crawl.
"""

from __future__ import annotations

import asyncio
import random
from dataclasses import dataclass
from typing import Any

import httpx

__all__ = [
    "BrawlClient",
    "ClientConfig",
    "BrawlAPIError",
    "InvalidAPIKey",
    "KeyNotAuthorizedForIP",
    "NotFound",
    "DIRECT_BASE_URL",
    "PROXY_BASE_URL",
    "PROXY_WHITELIST_IP",
]

DIRECT_BASE_URL = "https://api.brawlstars.com/v1"

# RoyaleAPI operate a community proxy for the Supercell APIs. Whitelisting
# their fixed address instead of your own turns the IP lock from a deployment
# constraint into a non-issue: the key is valid from anywhere, because every
# request reaches Supercell from the proxy's address rather than yours.
#
# This is what makes a laptop a viable collector host, and it is why the
# project does not require paid infrastructure to start gathering data.
PROXY_BASE_URL = "https://bsproxy.royaleapi.dev/v1"
PROXY_WHITELIST_IP = "45.79.218.79"


class BrawlAPIError(Exception):
    """Any non-recoverable API failure."""


class NotFound(BrawlAPIError):
    """The tag does not exist. Permanent; never retried."""


class InvalidAPIKey(BrawlAPIError):
    """The key itself is not recognized, regardless of where it was called from."""

    def __init__(self, detail: str = "") -> None:
        super().__init__(
            "The API key was rejected (403) as invalid.\n"
            "Supercell reported the key itself is not recognized -- this is not an IP "
            "problem. Check BRAWL_API_KEY for a truncated paste, a stray newline, or a "
            "key that was deleted at https://developer.brawlstars.com/ .\n"
            f"{detail}"
        )


class KeyNotAuthorizedForIP(BrawlAPIError):
    """The key is not valid for the address the request reached Supercell from."""

    def __init__(self, detail: str = "", *, via_proxy: bool = False) -> None:
        if via_proxy:
            guidance = (
                "You are routing through the RoyaleAPI proxy, so the address Supercell "
                f"sees is the proxy's, not yours. The key must therefore allow-list "
                f"{PROXY_WHITELIST_IP} -- and only that. If you allow-listed your own IP "
                "instead, the key will fail exactly like this."
            )
        else:
            guidance = (
                "You are calling Supercell directly, so the key must allow-list this "
                "machine's current public IP. Check it at https://api.ipify.org and add "
                "it at https://developer.brawlstars.com/ . If this used to work, a "
                "dynamic address has probably changed under you -- consider --proxy, "
                "which removes the problem entirely."
            )
        super().__init__(
            "The API key was rejected (403). Supercell keys are locked to the IP "
            "addresses declared when the key was created.\n"
            f"{guidance}\n{detail}"
        )


class _TokenBucket:
    """Paces requests to a sustained rate with a small burst allowance."""

    def __init__(self, rate_per_second: float, burst: int) -> None:
        self._rate = rate_per_second
        self._capacity = float(burst)
        self._tokens = float(burst)
        self._lock = asyncio.Lock()
        self._updated = 0.0

    async def take(self) -> None:
        async with self._lock:
            loop = asyncio.get_running_loop()
            now = loop.time()
            if self._updated == 0.0:
                self._updated = now
            self._tokens = min(
                self._capacity, self._tokens + (now - self._updated) * self._rate
            )
            self._updated = now
            if self._tokens < 1.0:
                wait = (1.0 - self._tokens) / self._rate
                await asyncio.sleep(wait)
                self._tokens = 0.0
                self._updated = loop.time()
            else:
                self._tokens -= 1.0


@dataclass(frozen=True, slots=True)
class ClientConfig:
    # Default deliberately below the ~10/s ceiling. Headroom costs a little
    # throughput and buys a crawler that is never the reason a key gets
    # throttled.
    requests_per_second: float = 7.5
    burst: int = 10
    timeout_seconds: float = 20.0
    max_retries: int = 5
    # Route through the RoyaleAPI proxy instead of calling Supercell directly.
    # Changes which IP the key must allow-list; nothing else about the API.
    use_proxy: bool = False

    @property
    def base_url(self) -> str:
        return PROXY_BASE_URL if self.use_proxy else DIRECT_BASE_URL


class BrawlClient:
    """Async client over the endpoints the collector actually needs."""

    def __init__(self, api_key: str, config: ClientConfig | None = None) -> None:
        if not api_key or not api_key.strip():
            raise ValueError(
                "an API key is required; create one at https://developer.brawlstars.com/ "
                "and set BRAWL_API_KEY"
            )
        self._config = config or ClientConfig()
        self._bucket = _TokenBucket(self._config.requests_per_second, self._config.burst)
        self._client = httpx.AsyncClient(
            base_url=self._config.base_url,
            headers={
                "Authorization": f"Bearer {api_key.strip()}",
                "Accept": "application/json",
            },
            timeout=self._config.timeout_seconds,
        )

    async def __aenter__(self) -> BrawlClient:
        return self

    async def __aexit__(self, *exc_info: object) -> None:
        await self.close()

    async def close(self) -> None:
        await self._client.aclose()

    async def _get(self, path: str) -> Any:
        last_error: Exception | None = None
        for attempt in range(self._config.max_retries):
            await self._bucket.take()
            try:
                response = await self._client.get(path)
            except httpx.TransportError as transport_error:
                last_error = transport_error
                await self._backoff(attempt)
                continue

            if response.status_code == 200:
                return response.json()
            if response.status_code == 404:
                raise NotFound(path)
            if response.status_code == 403:
                raise _diagnose_403(response.text, via_proxy=self._config.use_proxy)
            if response.status_code == 429 or response.status_code >= 500:
                last_error = BrawlAPIError(f"{response.status_code} on {path}")
                await self._backoff(attempt)
                continue
            raise BrawlAPIError(f"{response.status_code} on {path}: {response.text[:200]}")

        raise BrawlAPIError(f"giving up on {path} after {self._config.max_retries} attempts") from last_error

    async def _backoff(self, attempt: int) -> None:
        # Full jitter. Without it, many in-flight requests that hit a 429
        # together would retry together and hit it again.
        delay = min(2.0**attempt, 30.0)
        await asyncio.sleep(random.uniform(0.0, delay))

    # -- endpoints ---------------------------------------------------------

    async def battlelog(self, player_tag: str) -> list[dict[str, Any]]:
        """The player's most recent battles. Capped at 25 by the API itself."""
        payload = await self._get(f"/players/{_encode_tag(player_tag)}/battlelog")
        return payload.get("items", [])

    async def player(self, player_tag: str) -> dict[str, Any]:
        return await self._get(f"/players/{_encode_tag(player_tag)}")

    async def player_rankings(self, country_code: str = "global") -> list[dict[str, Any]]:
        """Top players for a country, or globally. The only way to enumerate tags."""
        payload = await self._get(f"/rankings/{country_code}/players")
        return payload.get("items", [])

    async def brawlers(self) -> list[dict[str, Any]]:
        payload = await self._get("/brawlers")
        return payload.get("items", [])

    async def event_rotation(self) -> list[dict[str, Any]]:
        return await self._get("/events/rotation")


async def current_public_ip(timeout: float = 5.0) -> str | None:
    """This machine's public IP, or None if it cannot be determined.

    Only useful on the direct (non-proxy) route, where the key must allow-list
    exactly this address. On a home connection it changes without warning, and
    the whole failure is "the portal wants a number you do not have to hand" --
    so we fetch it and print it rather than telling the user to go find it.
    """
    try:
        async with httpx.AsyncClient(timeout=timeout) as probe:
            response = await probe.get("https://api.ipify.org")
            if response.status_code == 200:
                return response.text.strip()
    except httpx.HTTPError:
        return None
    return None


def _diagnose_403(body: str, *, via_proxy: bool) -> BrawlAPIError:
    """Tell a bad key apart from a wrong IP, since both come back as 403.

    Supercell distinguishes them in the response body -- an IP rejection carries
    reason ``accessDenied.invalidIp`` and names the offending address, while an
    unrecognized key is a plain ``accessDenied``. Surfacing the difference turns
    the single most common setup failure from a guessing game into a fix.
    """
    detail = body[:300]
    haystack = body.lower()
    looks_like_ip_problem = "invalidip" in haystack or "ip address" in haystack

    if looks_like_ip_problem:
        return KeyNotAuthorizedForIP(detail, via_proxy=via_proxy)
    if "invalid authorization" in haystack or "accessdenied" in haystack:
        return InvalidAPIKey(detail)
    # Unrecognized 403 shape: report both possibilities rather than guess wrong.
    return KeyNotAuthorizedForIP(detail, via_proxy=via_proxy)


def _encode_tag(tag: str) -> str:
    """Player tags start with '#', which has to be percent-encoded in a path."""
    cleaned = tag.strip().upper()
    if not cleaned.startswith("#"):
        cleaned = "#" + cleaned
    return cleaned.replace("#", "%23")
