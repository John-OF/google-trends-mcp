"""Offline tests for server.py: no request ever reaches Google.

Network calls are replaced with httpx.MockTransport or by monkeypatching the
internal fetchers, so the suite is fast and deterministic.
"""

from __future__ import annotations

import functools
from datetime import UTC, datetime, timedelta

import httpx
import pytest

import server

# --------------------------------------------------------------------------- #
# Helpers
# --------------------------------------------------------------------------- #

RSS_FEED = """<?xml version="1.0" encoding="UTF-8"?>
<rss xmlns:ht="https://trends.google.com/trending/rss" version="2.0">
  <channel>
    <item>
      <title>tropical storm houston</title>
      <ht:approx_traffic>100,000+</ht:approx_traffic>
      <pubDate>Sun, 31 Aug 2025 10:00:00 +0000</pubDate>
      <ht:picture>https://example.com/pic.jpg</ht:picture>
      <ht:news_item>
        <ht:news_item_title>Storm “Erin” heads to Houston</ht:news_item_title>
        <ht:news_item_url>https://www.example.com/storm</ht:news_item_url>
        <ht:news_item_source>Example News</ht:news_item_source>
      </ht:news_item>
      <ht:news_item>
        <ht:news_item_title>Item without a link</ht:news_item_title>
      </ht:news_item>
    </item>
    <item>
      <title>messi</title>
      <ht:approx_traffic>500+</ht:approx_traffic>
      <pubDate>Sun, 31 Aug 2025 09:00:00 +0000</pubDate>
    </item>
  </channel>
</rss>
""".encode()


def mock_httpx(monkeypatch, handler):
    """Route every httpx.Client created by server.py through `handler`."""
    real_client = httpx.Client
    monkeypatch.setattr(
        server.httpx,
        "Client",
        functools.partial(real_client, transport=httpx.MockTransport(handler)),
    )


def trend(rank, title, volume, news=()):
    return {
        "rank": rank,
        "title": title,
        "volume": volume,
        "volume_label": f"{volume}+",
        "started": f"2025-08-31T0{rank}:00:00+00:00",
        "explore_url": server._build_explore_url(title, "US"),
        "picture": None,
        "picture_source": None,
        "news": [{"title": t, "source": "s", "url": f"https://example.com/{t}", "snippet": None, "image": None}
                 for t in news],
    }


def fake_trends():
    return [
        trend(1, "alpha", 20_000, news=["a1"]),
        trend(2, "beta", 500_000),
        trend(3, "gamma travel", 100_000, news=["g1", "g2", "g3"]),
    ]


@pytest.fixture
def feed(monkeypatch):
    monkeypatch.setattr(server, "_fetch_trending", lambda geo: fake_trends())


def weekly_series(values, start=datetime(2025, 1, 5, tzinfo=UTC)):
    return [(start + timedelta(weeks=i), float(v)) for i, v in enumerate(values)]


# --------------------------------------------------------------------------- #
# Small parsers
# --------------------------------------------------------------------------- #


@pytest.mark.parametrize(
    ("raw", "expected"),
    [("20,000+", 20000), ("500+", 500), ("", None), (None, None), ("n/a", None)],
)
def test_parse_traffic(raw, expected):
    assert server._parse_traffic(raw) == expected


def test_parse_pubdate():
    assert server._parse_pubdate("Sun, 31 Aug 2025 19:24:56 -0400") == "2025-08-31T23:24:56+00:00"
    assert server._parse_pubdate("not a date") == "not a date"
    assert server._parse_pubdate(None) is None


def test_explore_url_encodes_query():
    url = server._build_explore_url("barcelona vs rayo", "GB")
    assert url == "https://trends.google.com/trends/explore?q=barcelona+vs+rayo&geo=GB&date=now%207-d"


@pytest.mark.parametrize(
    ("sort", "expected"),
    [("recency", ["alpha", "beta", "gamma travel"]), ("volume", ["beta", "gamma travel", "alpha"])],
)
def test_sort_trends(sort, expected):
    assert [t["title"] for t in server._sort_trends(fake_trends(), sort)] == expected


# --------------------------------------------------------------------------- #
# Retry
# --------------------------------------------------------------------------- #


def test_retry_retries_transient_errors(monkeypatch):
    monkeypatch.setattr(server.time, "sleep", lambda _: None)
    calls = {"n": 0}

    def flaky():
        calls["n"] += 1
        if calls["n"] < 3:
            raise httpx.ConnectError("boom")
        return "ok"

    assert server._retry(flaky, attempts=3) == "ok"
    assert calls["n"] == 3


def test_retry_does_not_retry_permanent_errors(monkeypatch):
    monkeypatch.setattr(server.time, "sleep", lambda _: None)
    calls = {"n": 0}

    def broken():
        calls["n"] += 1
        raise ValueError("bad input")

    with pytest.raises(ValueError):
        server._retry(broken, attempts=3)
    assert calls["n"] == 1


def test_is_rate_limited():
    assert server._is_rate_limited(Exception("HTTP 429 Too Many Requests"))
    assert not server._is_rate_limited(Exception("HTTP 500"))


# --------------------------------------------------------------------------- #
# RSS feed (mocked transport)
# --------------------------------------------------------------------------- #


def test_fetch_trending_parses_feed_and_keeps_typographic_quotes(monkeypatch):
    seen = {}

    def handler(request):
        seen["url"] = str(request.url)
        return httpx.Response(200, content=RSS_FEED)

    mock_httpx(monkeypatch, handler)
    trends = server._fetch_trending("GB")

    assert seen["url"] == "https://trends.google.com/trending/rss?geo=GB"
    assert [t["title"] for t in trends] == ["tropical storm houston", "messi"]
    first = trends[0]
    assert first["rank"] == 1
    assert first["volume"] == 100_000
    assert first["volume_label"] == "100,000+"
    assert first["started"] == "2025-08-31T10:00:00+00:00"
    assert first["picture"] == "https://example.com/pic.jpg"
    assert "geo=GB" in first["explore_url"]
    # Typographic quotes survive because the XML is parsed from bytes.
    assert first["news"][0]["title"] == "Storm “Erin” heads to Houston"
    # Articles without a URL are dropped.
    assert [n["url"] for n in first["news"]] == ["https://www.example.com/storm"]
    assert trends[1]["news"] == []


def test_fetch_trending_raises_on_http_error(monkeypatch):
    monkeypatch.setattr(server.time, "sleep", lambda _: None)
    mock_httpx(monkeypatch, lambda request: httpx.Response(400))
    with pytest.raises(httpx.HTTPStatusError):
        server._fetch_trending("XX")


# --------------------------------------------------------------------------- #
# Tool: get_daily_trends
# --------------------------------------------------------------------------- #


def test_daily_trends_sorts_and_limits(feed):
    result = server.get_daily_trends.fn(geo=" us ", limit=2, sort="volume")

    assert result["ok"] is True
    assert result["geo"] == "US"
    assert result["source"] == "rss"
    assert result["total_available"] == 3
    assert result["count"] == 2
    assert [t["title"] for t in result["trends"]] == ["beta", "gamma travel"]
    assert result["trends"][0]["news_available"] is False
    assert result["trends"][1]["news_available"] is True
    assert all("news" not in t for t in result["trends"])


def test_daily_trends_can_include_news(feed):
    result = server.get_daily_trends.fn(include_news=True)

    assert [t["title"] for t in result["trends"]] == ["alpha", "beta", "gamma travel"]
    assert len(result["trends"][2]["news"]) == 3
    assert all("news_available" not in t for t in result["trends"])


def test_daily_trends_reports_feed_errors(monkeypatch):
    def broken(geo):
        raise httpx.ConnectError("offline")

    monkeypatch.setattr(server, "_fetch_trending", broken)
    result = server.get_daily_trends.fn(geo="US")

    assert result["ok"] is False
    assert "ConnectError" in result["error"]
    assert "hint" in result


def test_daily_trends_handles_empty_feed(monkeypatch):
    monkeypatch.setattr(server, "_fetch_trending", lambda geo: [])
    result = server.get_daily_trends.fn(geo="US")
    assert result["ok"] is False
    assert result["error"] == "Google returned no trends."


# --------------------------------------------------------------------------- #
# Tool: get_trend_articles
# --------------------------------------------------------------------------- #


def test_trend_articles_prefers_exact_match(feed):
    result = server.get_trend_articles.fn(trend="ALPHA")

    assert result["ok"] is True
    assert result["matched_by"] == "exact"
    assert result["trend"] == "alpha"
    assert result["urls"] == ["https://example.com/a1"]


def test_trend_articles_partial_match_and_limit(feed):
    result = server.get_trend_articles.fn(trend="travel", limit=2)

    assert result["trend"] == "gamma travel"
    assert result["matched_by"] == "partial"
    assert result["article_count"] == 2
    assert result["urls"] == ["https://example.com/g1", "https://example.com/g2"]


def test_trend_articles_without_news(feed):
    result = server.get_trend_articles.fn(trend="beta")
    assert result["ok"] is True
    assert result["article_count"] == 0
    assert result["note"] == "No articles are linked to this trend."


def test_trend_articles_unknown_trend_lists_alternatives(feed):
    result = server.get_trend_articles.fn(trend="nonexistent topic")

    assert result["ok"] is False
    assert result["available_trends"] == ["alpha", "beta", "gamma travel"]


def test_trend_articles_requires_a_title():
    assert server.get_trend_articles.fn(trend="   ")["ok"] is False


# --------------------------------------------------------------------------- #
# Metrics
# --------------------------------------------------------------------------- #


def test_series_metrics_no_data():
    assert server._series_metrics("x", [])["status"] == "no_data"
    assert server._series_metrics("x", weekly_series([0, 0, 0]))["status"] == "no_data"


def test_series_metrics_flat_series_is_stable():
    result = server._series_metrics("flat", weekly_series([50] * 12))

    assert result["status"] == "ok"
    assert result["average_interest"] == 50
    assert result["momentum_pct"] == 0
    assert result["slope_pct_per_period"] == 0
    assert result["volatility_pct"] == 0
    assert result["trend"] == "stable"
    assert result["is_breakout"] is False


def test_series_metrics_rising_breakout():
    result = server._series_metrics("up", weekly_series([10, 10, 10, 10, 20, 30, 40, 50, 92, 95, 98, 100]))

    assert result["trend"] == "rising"
    assert result["momentum_pct"] > 50
    assert result["is_breakout"] is True
    assert result["peak_interest"] == 100


def test_series_metrics_declining():
    result = server._series_metrics("down", weekly_series([100, 90, 80, 70, 60, 50, 40, 30, 20, 10]))
    assert result["trend"] == "declining"


def test_series_metrics_seasonality():
    start = datetime(2025, 1, 1, tzinfo=UTC)
    series = [(start.replace(month=m), float(m * 5)) for m in range(1, 13)]

    seasonality = server._series_metrics("seasonal", series)["seasonality"]

    assert seasonality["best_month"] == "December"
    assert seasonality["worst_month"] == "January"
    assert len(seasonality["monthly_average"]) == 12
    current = datetime.now(UTC).month
    assert seasonality["season_fit_pct"] == round(current * 5 / 60 * 100, 1)


def test_opportunity_score_formula():
    result = server._series_metrics("flat", weekly_series([50] * 12))
    season = result["seasonality"]["season_fit_pct"] or 60.0
    assert result["opportunity_score"] == round(0.40 * 50 + 0.35 * 50 + 0.25 * season)


def test_search_metrics_validates_keywords():
    assert server.get_search_metrics.fn(keywords=["  ", ""])["ok"] is False
    too_many = server.get_search_metrics.fn(keywords=["a", "b", "c", "d", "e", "f"])
    assert too_many["ok"] is False
    assert "at most 5" in too_many["error"]
