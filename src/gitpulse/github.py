"""Async GitHub REST client built for bulk extraction.

* Pagination follows the `Link: rel="next"` header (never guesses page numbers).
* Conditional requests: every page's ETag is cached; GitHub answers an unchanged page
  with 304, which does NOT count against the rate limit, so re-runs are nearly free.
* Rate limits: when `X-RateLimit-Remaining` hits 0 we sleep until `X-RateLimit-Reset`;
  secondary limits (403/429 with `Retry-After`) are honoured too.
* Transient 5xx / network errors retry with exponential backoff + full jitter.
"""

from __future__ import annotations

import asyncio
import random
import re
import time
from collections.abc import AsyncIterator, Awaitable, Callable
from dataclasses import dataclass, field
from typing import Any, Protocol

import httpx
import structlog

log = structlog.get_logger(__name__)
API = "https://api.github.com"
NEXT_LINK = re.compile(r'<([^>]+)>;\s*rel="next"')


class ETagCache(Protocol):
    def get(self, url: str) -> tuple[str, Any] | None: ...
    def put(self, url: str, etag: str, body: Any) -> None: ...


@dataclass
class MemoryCache:
    data: dict[str, tuple[str, Any]] = field(default_factory=dict)

    def get(self, url: str) -> tuple[str, Any] | None:
        return self.data.get(url)

    def put(self, url: str, etag: str, body: Any) -> None:
        self.data[url] = (etag, body)


@dataclass
class Stats:
    requests: int = 0
    not_modified: int = 0
    retries: int = 0
    rate_limit_waits: int = 0


class GitHubError(RuntimeError):
    def __init__(self, status: int, url: str, message: str) -> None:
        super().__init__(f"GitHub {status} for {url}: {message}")
        self.status = status


Sleep = Callable[[float], Awaitable[None]]


class GitHub:
    def __init__(
        self,
        token: str | None = None,
        *,
        cache: ETagCache | None = None,
        client: httpx.AsyncClient | None = None,
        max_retries: int = 5,
        sleep: Sleep = asyncio.sleep,
        now: Callable[[], float] = time.time,
        max_wait_s: float = 900,
    ) -> None:
        headers = {
            "Accept": "application/vnd.github+json",
            "X-GitHub-Api-Version": "2022-11-28",
            "User-Agent": "gitpulse",
        }
        if token:
            headers["Authorization"] = f"Bearer {token}"
        self.client = client or httpx.AsyncClient(timeout=30, headers=headers)
        if client is not None:
            self.client.headers.update(headers)
        self.cache = cache or MemoryCache()
        self.max_retries = max_retries
        self.sleep = sleep
        self.now = now
        self.max_wait_s = max_wait_s
        self.stats = Stats()

    async def aclose(self) -> None:
        await self.client.aclose()

    async def _request(self, url: str) -> tuple[Any, str | None]:
        """GET one page; returns (json body, next-page url)."""
        cached = self.cache.get(url)
        for attempt in range(self.max_retries + 1):
            headers = {"If-None-Match": cached[0]} if cached else {}
            try:
                res = await self.client.get(url, headers=headers)
            except httpx.TransportError as exc:
                await self._backoff(attempt, url, str(exc))
                continue
            self.stats.requests += 1
            nxt = m.group(1) if (m := NEXT_LINK.search(res.headers.get("link", ""))) else None

            if res.status_code == 304 and cached:
                self.stats.not_modified += 1
                return cached[1], nxt
            if res.status_code in (403, 429) and await self._rate_limited(res):
                continue
            if res.status_code >= 500:
                await self._backoff(attempt, url, f"HTTP {res.status_code}")
                continue
            if res.status_code >= 400:
                raise GitHubError(res.status_code, url, res.json().get("message", res.text[:200]))

            body = res.json()
            if etag := res.headers.get("etag"):
                self.cache.put(url, etag, body)
            await self._respect_remaining(res)
            return body, nxt
        raise GitHubError(0, url, f"gave up after {self.max_retries} retries")

    async def _backoff(self, attempt: int, url: str, reason: str) -> None:
        if attempt >= self.max_retries:
            raise GitHubError(0, url, f"gave up after {self.max_retries} retries: {reason}")
        self.stats.retries += 1
        delay = random.uniform(0, min(60.0, 2.0**attempt))  # full jitter
        log.warning(
            "github.retry", url=url, attempt=attempt + 1, delay=round(delay, 2), reason=reason
        )
        await self.sleep(delay)

    async def _rate_limited(self, res: httpx.Response) -> bool:
        """Sleep and return True if this 403/429 is a rate limit we can wait out."""
        if retry_after := res.headers.get("retry-after"):
            wait = float(retry_after)
        elif res.headers.get("x-ratelimit-remaining") == "0":
            wait = max(0.0, float(res.headers.get("x-ratelimit-reset", "0")) - self.now()) + 1
        else:
            return False  # a real permission error
        if wait > self.max_wait_s:
            raise GitHubError(res.status_code, str(res.url), f"rate limited for {wait:.0f}s")
        self.stats.rate_limit_waits += 1
        log.warning("github.rate_limited", wait_s=round(wait, 1))
        await self.sleep(wait)
        return True

    async def _respect_remaining(self, res: httpx.Response) -> None:
        # Pre-emptively pause when the budget is exhausted instead of eating a 403.
        if res.headers.get("x-ratelimit-remaining") == "0":
            reset = float(res.headers.get("x-ratelimit-reset", "0"))
            wait = max(0.0, reset - self.now()) + 1
            if wait <= self.max_wait_s:
                self.stats.rate_limit_waits += 1
                await self.sleep(wait)

    async def get(self, path: str) -> Any:
        body, _ = await self._request(f"{API}{path}")
        return body

    async def paginate(
        self, path: str, params: dict[str, str] | None = None
    ) -> AsyncIterator[dict[str, Any]]:
        """Yield every item across all pages. Handles list bodies and `{total_count, <key>: [...]}`."""
        query = "&".join(f"{k}={v}" for k, v in sorted((params or {}).items()))
        url: str | None = f"{API}{path}?per_page=100{'&' + query if query else ''}"
        while url:
            body, url = await self._request(url)
            items = (
                body
                if isinstance(body, list)
                else next((v for v in body.values() if isinstance(v, list)), [])
            )
            for item in items:
                yield item
