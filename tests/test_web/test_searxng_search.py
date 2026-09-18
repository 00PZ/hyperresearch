"""SearXNG search HTTP: JSON contract, no redirects, filter-then-limit."""

from __future__ import annotations

import os

import httpx
import pytest

from hyperresearch.runtime.errors import SearchBlockError
from hyperresearch.web.base import get_provider
from hyperresearch.web.searxng import SearchCallResult, searxng_search

ORIGIN = "http://127.0.0.1:8888"


def _client(handler) -> httpx.Client:
    return httpx.Client(
        transport=httpx.MockTransport(handler),
        follow_redirects=False,
        timeout=30.0,
    )


def _search(handler, query: str = "ion trap", max_results: int = 10) -> SearchCallResult:
    return searxng_search(ORIGIN, query, max_results, client=_client(handler))


def test_searxng_is_not_a_fetch_backend() -> None:
    with pytest.raises(ValueError, match="Unknown web provider"):
        get_provider("searxng")


def test_200_json_maps_hits_and_caps_snippet() -> None:
    long = "x" * 600
    requests: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        return httpx.Response(
            200,
            json={
                "results": [
                    {
                        "url": "https://example.com/a",
                        "title": "Alpha",
                        "content": long,
                    }
                ]
            },
        )

    result = _search(handler)
    assert len(result.hits) == 1
    assert result.hits[0].url == "https://example.com/a"
    assert result.hits[0].title == "Alpha"
    assert result.hits[0].content == "x" * 500
    assert result.unresponsive_engines == []
    assert len(requests) == 1
    assert requests[0].url.path.endswith("/search")
    assert requests[0].url.params["q"] == "ion trap"
    assert requests[0].url.params["format"] == "json"
    assert "authorization" not in {k.lower() for k in requests[0].headers}


def test_url_dedup_first_wins_then_limit() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            json={
                "results": [
                    {"url": "https://example.com/a", "title": "first", "content": "1"},
                    {"url": "https://example.com/a", "title": "dup", "content": "2"},
                    {"url": "https://example.com/b", "title": "b", "content": "3"},
                    {"url": "https://example.com/c", "title": "c", "content": "4"},
                ]
            },
        )

    result = _search(handler, max_results=2)
    assert [h.url for h in result.hits] == ["https://example.com/a", "https://example.com/b"]
    assert result.hits[0].title == "first"


def test_malformed_entries_skipped_null_title_kept() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            json={
                "results": [
                    "not-an-object",
                    {"title": "no url", "content": "x"},
                    {"url": None, "title": "null url", "content": "x"},
                    {"url": 123, "title": "num url", "content": "x"},
                    {"url": "   ", "title": "blank", "content": "x"},
                    {"url": "https://example.com/keep", "title": None, "content": ["not", "str"]},
                    {"url": "https://example.com/keep2", "title": 12, "content": None},
                ]
            },
        )

    result = _search(handler, max_results=10)
    assert [h.url for h in result.hits] == [
        "https://example.com/keep",
        "https://example.com/keep2",
    ]
    assert result.hits[0].title == ""
    assert result.hits[0].content == ""
    assert result.hits[1].title == ""
    assert result.hits[1].content == ""


def test_empty_results_is_success() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={"results": []})

    result = _search(handler)
    assert result.hits == []
    assert result.unresponsive_engines == []


def test_empty_results_all_engines_unresponsive_is_success() -> None:
    engines = [["google", "CAPTCHA"], ["brave", "timeout"]]

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            json={"results": [], "unresponsive_engines": engines},
        )

    result = _search(handler)
    assert result.hits == []
    assert result.unresponsive_engines == engines


def test_hits_plus_unresponsive_engines_is_success() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            json={
                "results": [{"url": "https://example.com/a", "title": "A", "content": "b"}],
                "unresponsive_engines": [["google", "CAPTCHA"]],
            },
        )

    result = _search(handler)
    assert len(result.hits) == 1
    assert result.unresponsive_engines == [["google", "CAPTCHA"]]


def test_malformed_unresponsive_engine_rows_dropped() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            json={
                "results": [],
                "unresponsive_engines": [
                    ["google", "CAPTCHA"],
                    "nope",
                    ["", "x"],
                    ["brave"],
                    ["bing", "CAPTCHA", "extra"],
                ],
            },
        )

    result = _search(handler)
    assert result.unresponsive_engines == [["google", "CAPTCHA"]]


@pytest.mark.parametrize(
    "payload",
    [
        pytest.param(lambda r: httpx.Response(500, text="err"), id="500"),
        pytest.param(lambda r: httpx.Response(403, text="no json"), id="403"),
        pytest.param(
            lambda r: httpx.Response(302, headers={"Location": "http://10.0.0.9/secret"}),
            id="302",
        ),
        pytest.param(
            lambda r: httpx.Response(200, text="<html>wall</html>", headers={"content-type": "text/html"}),
            id="html",
        ),
        pytest.param(lambda r: httpx.Response(200, text="{not json}"), id="truncated"),
        pytest.param(lambda r: httpx.Response(200, json=[1, 2]), id="array"),
    ],
)
def test_fail_closed_http_and_body(payload) -> None:
    with pytest.raises(SearchBlockError) as ei:
        _search(payload)
    assert ei.value.blocked_on == "search"
    assert ei.value.blocked_reason == "searxng_http"


def test_302_to_private_host_is_exactly_one_request() -> None:
    seen: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(str(request.url))
        return httpx.Response(302, headers={"Location": "http://10.0.0.9/secret"})

    with pytest.raises(SearchBlockError) as ei:
        _search(handler)
    assert ei.value.blocked_reason == "searxng_http"
    assert len(seen) == 1


def test_timeout_is_searxng_http() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        raise httpx.TimeoutException("timeout")

    with pytest.raises(SearchBlockError) as ei:
        _search(handler)
    assert ei.value.blocked_reason == "searxng_http"


def test_dns_failure_is_searxng_http() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("dns")

    with pytest.raises(SearchBlockError) as ei:
        _search(handler)
    assert ei.value.blocked_reason == "searxng_http"


def test_unresponsive_engines_non_list_is_searxng_http() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={"results": [], "unresponsive_engines": {"google": "CAPTCHA"}})

    with pytest.raises(SearchBlockError) as ei:
        _search(handler)
    assert ei.value.blocked_reason == "searxng_http"


def test_missing_results_is_searxng_http() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={"query": "q"})

    with pytest.raises(SearchBlockError) as ei:
        _search(handler)
    assert ei.value.blocked_reason == "searxng_http"


def test_500_is_not_retried_inside_one_attempt() -> None:
    seen: list[int] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(1)
        return httpx.Response(500, text="nope")

    with pytest.raises(SearchBlockError):
        _search(handler)
    assert seen == [1]


@pytest.mark.searxng_live
def test_live_searxng_json_object_results_list() -> None:
    url = os.environ["SEARXNG_URL"]
    result = searxng_search(url, "example", max_results=3)
    assert isinstance(result, SearchCallResult)
    assert isinstance(result.hits, list)
    assert isinstance(result.unresponsive_engines, list)
