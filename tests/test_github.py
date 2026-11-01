from __future__ import annotations

import httpx
import pytest
import respx

from gitpulse.github import API, GitHub, GitHubError, MemoryCache


class Sleeps:
    def __init__(self) -> None:
        self.calls: list[float] = []

    async def __call__(self, s: float) -> None:
        self.calls.append(s)


def gh(sleep: Sleeps | None = None, **kw: object) -> GitHub:
    return GitHub(
        "tok", client=httpx.AsyncClient(), sleep=sleep or Sleeps(), now=lambda: 1_000.0, **kw
    )  # type: ignore[arg-type]


@respx.mock
async def test_paginates_via_link_header_and_sends_auth() -> None:
    page2 = f"{API}/repos/o/r/pulls?per_page=100&page=2"
    route1 = respx.get(f"{API}/repos/o/r/pulls", params={"per_page": "100", "state": "all"}).mock(
        return_value=httpx.Response(
            200,
            json=[{"n": 1}, {"n": 2}],
            headers={"link": f'<{page2}>; rel="next", <x>; rel="last"'},
        )
    )
    respx.get(page2).mock(return_value=httpx.Response(200, json=[{"n": 3}]))
    client = gh()
    items = [i async for i in client.paginate("/repos/o/r/pulls", {"state": "all"})]
    assert [i["n"] for i in items] == [1, 2, 3]
    assert route1.calls[0].request.headers["authorization"] == "Bearer tok"
    assert client.stats.requests == 2


@respx.mock
async def test_unwraps_object_bodies() -> None:
    respx.get(f"{API}/repos/o/r/actions/runs").mock(
        return_value=httpx.Response(200, json={"total_count": 1, "workflow_runs": [{"id": 9}]})
    )
    assert [r["id"] async for r in gh().paginate("/repos/o/r/actions/runs")] == [9]


@respx.mock
async def test_conditional_requests_reuse_cached_body_on_304() -> None:
    cache = MemoryCache()
    route = respx.get(f"{API}/repos/o/r").mock(
        side_effect=[
            httpx.Response(200, json={"v": 1}, headers={"etag": '"abc"'}),
            httpx.Response(304),
        ]
    )
    client = gh(cache=cache)
    assert await client.get("/repos/o/r") == {"v": 1}
    assert await client.get("/repos/o/r") == {"v": 1}
    assert route.calls[1].request.headers["if-none-match"] == '"abc"'
    assert client.stats.not_modified == 1


@respx.mock
async def test_retries_5xx_and_network_errors_with_backoff() -> None:
    respx.get(f"{API}/x").mock(
        side_effect=[
            httpx.Response(502),
            httpx.ConnectError("boom"),
            httpx.Response(200, json={"ok": True}),
        ]
    )
    sleeps = Sleeps()
    client = gh(sleeps)
    assert await client.get("/x") == {"ok": True}
    assert client.stats.retries == 2 and len(sleeps.calls) == 2


@respx.mock
async def test_gives_up_after_max_retries() -> None:
    respx.get(f"{API}/x").mock(return_value=httpx.Response(503))
    with pytest.raises(GitHubError, match="gave up"):
        await gh(max_retries=2).get("/x")


@respx.mock
async def test_waits_for_primary_rate_limit_reset() -> None:
    respx.get(f"{API}/x").mock(
        side_effect=[
            httpx.Response(
                403,
                json={"message": "rate limit"},
                headers={"x-ratelimit-remaining": "0", "x-ratelimit-reset": "1030"},
            ),
            httpx.Response(200, json=[], headers={"x-ratelimit-remaining": "4999"}),
        ]
    )
    sleeps = Sleeps()
    client = gh(sleeps)
    await client.get("/x")
    assert sleeps.calls == [31.0] and client.stats.rate_limit_waits == 1


@respx.mock
async def test_honours_retry_after_and_preemptive_pause() -> None:
    respx.get(f"{API}/x").mock(
        side_effect=[
            httpx.Response(429, headers={"retry-after": "7"}),
            httpx.Response(
                200, json=[], headers={"x-ratelimit-remaining": "0", "x-ratelimit-reset": "1005"}
            ),
        ]
    )
    sleeps = Sleeps()
    await gh(sleeps).get("/x")
    assert sleeps.calls == [7.0, 6.0]  # Retry-After, then pause until reset (+1s)


@respx.mock
async def test_permission_errors_and_long_waits_raise() -> None:
    respx.get(f"{API}/private").mock(
        return_value=httpx.Response(404, json={"message": "Not Found"})
    )
    respx.get(f"{API}/forbidden").mock(
        return_value=httpx.Response(403, json={"message": "Resource not accessible"})
    )
    respx.get(f"{API}/slow").mock(
        return_value=httpx.Response(
            403, headers={"x-ratelimit-remaining": "0", "x-ratelimit-reset": "99999"}
        )
    )
    client = gh()
    with pytest.raises(GitHubError, match="Not Found") as e:
        await client.get("/private")
    assert e.value.status == 404
    with pytest.raises(GitHubError, match="not accessible"):
        await client.get("/forbidden")
    with pytest.raises(GitHubError, match="rate limited"):
        await client.get("/slow")
    await client.aclose()
