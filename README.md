# Google Trends MCP

[![CI](https://github.com/John-OF/google-trends-mcp/actions/workflows/ci.yml/badge.svg)](https://github.com/John-OF/google-trends-mcp/actions/workflows/ci.yml)
![Python](https://img.shields.io/badge/python-3.11%20%7C%203.12%20%7C%203.13-blue)
[![License: MIT](https://img.shields.io/badge/license-MIT-green.svg)](LICENSE)

An MCP server (FastMCP, stdio) that turns Google Trends into an editorial
research tool for AI agents: find out what a country started searching for in
the last few hours, check whether a keyword has real and sustained demand, and
collect the source articles behind a trend, all before writing a single word.

Built for a daily blogging workflow (pick a topic → validate demand → gather
sources → hand them to a writing/publishing tool), but the tools are generic.

<p align="center">
  <img src="assets/demo.svg" width="800" alt="An agent calls get_daily_trends, get_search_metrics and get_trend_articles, picks the trend with the best opportunity score and collects three direct source URLs">
</p>
<p align="center"><sub>Real data from a run on 2026-11-20 (US). Regenerate with <code>python tools/make_demo.py</code>.</sub></p>

Google has **no official Trends API**. This server uses two public paths:

| Tool | Source | Returns |
|---|---|---|
| `get_daily_trends` | "Trending now" RSS feed | The ~10 trends that most recently took off in a country, with approximate search volume |
| `get_trend_articles` | Same feed | The articles from the **"In the news"** panel of a trend, with direct publisher URLs |
| `get_search_metrics` | [`pytrends`](https://github.com/GeneralMills/pytrends) (Interest Over Time + Related Queries) | Derived per-keyword metrics: momentum, seasonality, opportunity score |

> **Why only the RSS feed?** Until November 2026 the first two tools used the
> internal API behind [trends.google.com/trending](https://trends.google.com/trending)
> (`batchexecute`), which exposed the full 4 h / 24 h / 48 h / 7 d windows
> (hundreds of trends), growth percentages and the trend breakdown. Google then
> put that endpoint behind a reCAPTCHA Enterprise token: without it, every
> request returns `PERMISSION_DENIED`. Getting around a CAPTCHA is not something
> this project does, so it now relies only on the public feed, which still
> carries volumes and direct article links.

---

## Installation

Requires Python 3.11+ (developed on 3.12).

```bash
git clone https://github.com/John-OF/google-trends-mcp.git
cd google-trends-mcp
python -m venv .venv
```

Install the dependencies with the virtual environment's Python:

```powershell
# Windows
.venv\Scripts\python.exe -m pip install -r requirements.txt
```

```bash
# macOS / Linux
.venv/bin/python -m pip install -r requirements.txt
```

Check that it starts (it waits on stdio; press `Ctrl+C` to exit):

```bash
.venv/bin/python server.py          # Windows: .venv\Scripts\python.exe server.py
```

---

## Registering the server in your MCP client

Always point to the **virtual environment's** Python, not the system one. In the
examples below, replace `<path-to-repo>` with the absolute path where you cloned
the repository (on macOS/Linux, use `.venv/bin/python` instead of
`.venv/Scripts/python.exe`).

**Claude Code, user scope** (available in every project):

```bash
claude mcp add g-trends --scope user -- "<path-to-repo>/.venv/Scripts/python.exe" "<path-to-repo>/server.py"
```

**Claude Code, project scope:** copy `.mcp.json.example` to `.mcp.json` and fill
in the paths. It is loaded automatically when Claude Code opens the folder.

**Claude Desktop** (`claude_desktop_config.json`):

```json
{
  "mcpServers": {
    "g-trends": {
      "command": "<path-to-repo>/.venv/Scripts/python.exe",
      "args": ["<path-to-repo>/server.py"]
    }
  }
}
```

---

## Tools

### 1. `get_daily_trends(geo, limit, sort, include_news)`

The trends that most recently took off in a country, newest first.

| Parameter | Type | Default | Description |
|---|---|---|---|
| `geo` | str | `"US"` | ISO alpha-2 code: `US`, `GB`, `CA`, `AU`, `IE`, `NZ`… |
| `limit` | int (1-25) | `10` | Number of trends returned after sorting (the feed holds ~10) |
| `sort` | `recency` \| `volume` | `recency` | `recency` keeps the feed order (newest first) |
| `include_news` | bool | `false` | Attach each trend's articles inline (no extra requests) |

```json
{
  "ok": true,
  "geo": "US",
  "sort": "volume",
  "source": "rss",
  "fetched_at": "2026-11-20T05:02:11+00:00",
  "total_available": 10,
  "count": 3,
  "trends": [
    {
      "rank": 7,
      "title": "toyota recalls",
      "volume": 20000,
      "volume_label": "20000+",
      "started": "2026-11-20T04:30:00+00:00",
      "explore_url": "https://trends.google.com/trends/explore?q=toyota+recalls&geo=US&date=now%207-d",
      "picture": "https://encrypted-tbn0.gstatic.com/images?q=...",
      "picture_source": "Fox Business",
      "news_available": true
    }
  ]
}
```

`volume` is Google's absolute approximation (`20000+` → 20000), **not** the
relative 0-100 index returned by `get_search_metrics`. `rank` is the position in
the feed, which is ordered by start time.

### 2. `get_trend_articles(trend, geo, limit)`

The articles behind one specific trend: the **"In the news"** panel the website
shows when you click a trend. These are the URLs you hand to a writing tool as
source material.

| Parameter | Type | Default | Description |
|---|---|---|---|
| `trend` | str | — | Trend title; partial, case-insensitive matches work |
| `geo` | str | `"US"` | The same country you found it in |
| `limit` | int (1-10) | `10` | Maximum number of articles (the feed carries about 3 per trend) |

```json
{
  "ok": true,
  "geo": "US",
  "trend": "toyota recalls",
  "matched_by": "exact",
  "volume_label": "20000+",
  "article_count": 3,
  "articles": [
    {
      "title": "Over 314K older Toyota, Lexus vehicles recalled ...",
      "source": "Fox Business",
      "url": "https://www.foxbusiness.com/lifestyle/over-314k-older-toyota-lexus-vehicles-recalled-potentially-dangerous-airbag-issue",
      "snippet": null,
      "image": "https://encrypted-tbn0.gstatic.com/images?q=..."
    }
  ],
  "urls": ["https://www.foxbusiness.com/lifestyle/over-314k-older-toyota-lexus-vehicles-recalled-potentially-dangerous-airbag-issue", "..."]
}
```

`urls` is a flat list of links, ready to pass as-is to another tool. **They are
direct publisher URLs** (`foxbusiness.com`, `usatoday.com`, `billboard.com`),
not Google redirects, so they can be opened and read.

If the trend has already rotated out of the feed, the tool returns `ok: false`
along with `available_trends` (the current titles).

### 3. `get_search_metrics(keywords, geo, timeframe, include_related)`

Demand metrics for up to 5 keywords, compared against each other.

| Parameter | Type | Default | Description |
|---|---|---|---|
| `keywords` | list[str] (1-5) | — | Google's hard limit: 5 per request |
| `geo` | str | `"US"` | ISO alpha-2, or `""` for worldwide |
| `timeframe` | str | `"today 12-m"` | `now 7-d`, `today 1-m`, `today 3-m`, `today 12-m`, `today 5-y`, `all`, or `YYYY-MM-DD YYYY-MM-DD` |
| `include_related` | bool | `true` | Adds `top` and `rising` related queries (ideas for angles) |

Metrics computed per keyword:

| Field | Meaning |
|---|---|
| `average_interest` / `peak_interest` / `latest_interest` | Relative 0-100 index |
| `peak_date` | Date of the peak |
| `momentum_pct` | Last 4 points vs. the previous 4 (with weekly data ≈ month over month) |
| `slope_pct_per_period` | Least-squares slope, normalised to the average |
| `volatility_pct` | Coefficient of variation (one-off spikes vs. steady demand) |
| `trend` | `rising` / `stable` / `declining` |
| `is_breakout` | At its highs (≥90) and rising sharply (≥50 %) |
| `seasonality` | Average per month, best/worst month and the current month's `season_fit_pct` |
| `opportunity_score` | `0.40·average + 0.35·(50+momentum) + 0.25·season_fit`, each term clamped to 0-100 |

Plus a `ranking` already sorted by `opportunity_score`, and the `resolution`
(hourly/daily/weekly/monthly) of the series Google returned.

**The 0-100 values are relative to the keywords in that same call.** To compare
candidates, send them together in a single call; never compare numbers across
separate calls.

---

## Suggested workflow

1. `get_daily_trends(geo="US")` → the topics that just started trending. The
   feed rotates every few minutes, so checking a few times a day (or across
   several countries) widens the pool.
2. Filter the ones that fit your niche.
3. `get_search_metrics(keywords=[3-5 candidates], geo="US", timeframe="today 12-m")`
   → keep the highest `opportunity_score`; check `season_fit_pct` so you don't
   write about a topic in its worst month.
4. `get_trend_articles(trend="<the chosen one>")` → take `urls` and pass them to
   your writing/publishing tool (e.g. a WordPress MCP) as sources.
5. Use `related_queries.rising` for subheadings and long-tail keywords.

Example prompt:

> Get the current trends in the US, keep the travel-related ones, measure the top 5
> with `get_search_metrics` over 12 months, and for the one with the highest
> `opportunity_score` fetch its articles with `get_trend_articles`. Then write
> the post using those URLs as sources and the rising queries as H2s.

---

## Limits and caveats

- **Rate limiting (429):** `get_search_metrics` hits unofficial endpoints. The
  server retries with exponential backoff (3 attempts) and, if it still fails,
  returns `{"ok": false, "rate_limited": true}` instead of crashing. Space your
  calls out: a few per session, not dozens in a row.
- **`pytrends` is no longer maintained.** `get_search_metrics` depends on it, so
  if Google changes the endpoints it relies on, that tool will stop working and
  no upstream fix will come. The other two tools do not use `pytrends`.
- **The feed is small and has no filters.** It holds only the ~10 most recent
  trends per country, with no time window, category filter, growth percentage
  or trend breakdown; those now live behind the website's reCAPTCHA (see above).
  Filter the topics yourself.
- **Trends rotate.** Call `get_trend_articles` soon after `get_daily_trends`,
  or the topic may have left the feed.
- **Google News deliberately left out:** `news.google.com/rss/search` returns
  ~100 articles per term, but its links are `news.google.com/rss/articles/CBM...`
  redirects that do not resolve server-side (the hop happens in JavaScript).
  They would be useless to a writing tool. The "In the news" panel links do go
  straight to the publisher.
- **Encoding:** the XML is parsed from bytes, not from `response.text`;
  otherwise typographic quotes in headlines arrive corrupted and leak into the
  final article.
- **Partial points:** the last, still-incomplete period is dropped before
  computing metrics (otherwise it artificially drags `momentum_pct` down). It is
  reported in `partial_points_dropped`.
- **pytrends + urllib3 2.x:** pytrends 4.9.2's built-in retry is broken
  (`method_whitelist` was removed in urllib3 2.0). That is why retrying is
  implemented in `server.py` (`_retry`) and no `retries`/`backoff_factor` is
  passed to `TrendReq`.
- **FastMCP:** pinned to `<3.0`, following the project's own recommendation.

---

## Development

```bash
python -m pip install -r requirements-dev.txt
python -m pytest        # offline: every network call is mocked
python -m ruff check .
```

The tests never hit Google: HTTP is replaced with `httpx.MockTransport` and the
fetchers are monkeypatched, so the suite runs in about a second. CI runs lint
and tests on Python 3.11, 3.12 and 3.13.

---

## Disclaimer

This project is not affiliated with, endorsed by or sponsored by Google. It
relies on undocumented endpoints that may change or stop working at any time.
Use it responsibly and within Google's terms of service.

## License

[MIT](LICENSE) © 2026 John Orellana
