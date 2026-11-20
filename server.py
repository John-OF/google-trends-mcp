"""
G-Trends MCP
============
Servidor MCP (FastMCP, transporte stdio) para descubrir temas y validar demanda
de busqueda en Google Trends. Pensado para un flujo editorial diario de blog de
turismo en ingles dirigido a publico angloparlante.

Herramientas expuestas:
  1. get_daily_trends    -> tendencias mas recientes por pais (feed RSS)
  2. get_trend_articles  -> articulos del panel "Es noticia" de una tendencia (feed RSS)
  3. get_search_metrics  -> metricas de interes de busqueda por keyword (pytrends)

Ejecucion:  python server.py
"""

from __future__ import annotations

import math
import statistics
import time
from datetime import UTC, datetime
from email.utils import parsedate_to_datetime
from typing import Annotated, Any, Literal
from urllib.parse import quote_plus
from xml.etree import ElementTree

import httpx
from fastmcp import FastMCP
from pydantic import Field

mcp = FastMCP(
    name="g-trends",
    instructions=(
        "Google Trends toolkit for editorial research. Use `get_daily_trends` to "
        "list the trends that most recently took off in a country, with "
        "approximate search volumes; `get_search_metrics` "
        "to check whether a candidate keyword has real, sustained and "
        "seasonally-aligned demand before committing it to an article; and "
        "`get_trend_articles` to collect the source articles behind a chosen "
        "trend, with direct publisher URLs ready to hand to a writing tool."
    ),
)

# --------------------------------------------------------------------------- #
# Constantes / utilidades compartidas
# --------------------------------------------------------------------------- #

# Google Trends no tiene API oficial. El feed RSS de "trending now" es el unico
# endpoint publico que sigue abierto (el feed legacy /trendingsearches/daily/rss
# devuelve 404 desde 2026).
RSS_ENDPOINT = "https://trends.google.com/trending/rss"
HT = "{https://trends.google.com/trending/rss}"  # namespace de los campos ht:*

USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/125.0.0.0 Safari/537.36"
)

MONTHS = (
    "January", "February", "March", "April", "May", "June",
    "July", "August", "September", "October", "November", "December",
)


def _now_iso() -> str:
    return datetime.now(UTC).isoformat(timespec="seconds")


def _text(node: ElementTree.Element | None) -> str | None:
    if node is None or node.text is None:
        return None
    value = node.text.strip()
    return value or None


def _parse_traffic(raw: str | None) -> int | None:
    """'20,000+' -> 20000 ; devuelve None si no hay dato numerico."""
    if not raw:
        return None
    digits = "".join(ch for ch in raw if ch.isdigit())
    return int(digits) if digits else None


def _parse_pubdate(raw: str | None) -> str | None:
    if not raw:
        return None
    try:
        return parsedate_to_datetime(raw).astimezone(UTC).isoformat(timespec="seconds")
    except (TypeError, ValueError):
        return raw


def _clamp(value: float, low: float = 0.0, high: float = 100.0) -> float:
    return max(low, min(high, value))


def _is_rate_limited(exc: BaseException) -> bool:
    return "429" in str(exc) or "TooManyRequests" in type(exc).__name__


def _retry(operation, attempts: int = 3, base_delay: float = 2.0, max_seconds: float = 60.0):
    """Reintenta con backoff exponencial ante 429 / errores de red.

    No se usa el retry interno de pytrends 4.9.2: construye urllib3.Retry con
    `method_whitelist`, argumento eliminado en urllib3 2.x.

    `max_seconds` acota el tiempo total: sin el, tres intentos contra un Google
    lento pueden bloquear la tool varios minutos, y es preferible devolver el
    error para que el agente reintente mas tarde.
    """
    started = time.monotonic()
    for attempt in range(attempts):
        try:
            return operation()
        except Exception as exc:
            transient = _is_rate_limited(exc) or isinstance(
                exc, (TimeoutError, ConnectionError, httpx.TransportError)
            )
            last_attempt = attempt == attempts - 1
            if not transient or last_attempt or time.monotonic() - started > max_seconds:
                raise
            time.sleep(base_delay * (2 ** attempt))


# --------------------------------------------------------------------------- #
# Feed RSS de tendencias: fuente compartida por las tools 1 y 2
# --------------------------------------------------------------------------- #
# Hasta 2026-11 se usaba la API interna de la web (batchexecute, rpc i0OFE), que
# daba la ventana completa, el % de crecimiento y el desglose. Google la protegio
# con un token de reCAPTCHA Enterprise y sin el responde PERMISSION_DENIED, asi
# que el feed RSS publico es ahora la unica fuente.


def _build_explore_url(query: str, geo: str) -> str:
    """Enlace real a Google Trends para un termino.

    El <link> de cada item del RSS apunta al propio feed, no a la tendencia,
    asi que la URL util se construye aqui.
    """
    return f"https://trends.google.com/trends/explore?q={quote_plus(query)}&geo={geo}&date=now%207-d"


def _parse_news_item(node: ElementTree.Element) -> dict[str, Any]:
    """Un articulo del panel 'Es noticia'. La URL es directa al medio."""
    return {
        "title": _text(node.find(f"{HT}news_item_title")),
        "source": _text(node.find(f"{HT}news_item_source")),
        "url": _text(node.find(f"{HT}news_item_url")),
        "snippet": _text(node.find(f"{HT}news_item_snippet")),
        "image": _text(node.find(f"{HT}news_item_picture")),
    }


def _download_feed(geo: str) -> bytes:
    headers = {"User-Agent": USER_AGENT, "Accept": "application/rss+xml, application/xml;q=0.9"}
    with httpx.Client(timeout=20.0, headers=headers, follow_redirects=True) as client:
        response = client.get(RSS_ENDPOINT, params={"geo": geo})
        response.raise_for_status()
        return response.content


def _fetch_trending(geo: str) -> list[dict[str, Any]]:
    """Descarga y parsea el feed. Lanza excepcion si no se puede leer."""
    body = _retry(lambda: _download_feed(geo), attempts=2, max_seconds=30.0)

    # Se parsea sobre bytes a proposito: asi manda la declaracion de encoding
    # del XML. Con response.text las comillas tipograficas de los titulares
    # llegan corrompidas, y esos titulares alimentan el articulo final.
    root = ElementTree.fromstring(body)

    trends: list[dict[str, Any]] = []
    for rank, item in enumerate(root.iter("item"), start=1):
        title = _text(item.find("title")) or ""
        traffic_raw = _text(item.find(f"{HT}approx_traffic"))
        trends.append(
            {
                "rank": rank,
                "title": title,
                "volume": _parse_traffic(traffic_raw),
                "volume_label": traffic_raw,
                "started": _parse_pubdate(_text(item.find("pubDate"))),
                "explore_url": _build_explore_url(title, geo),
                "picture": _text(item.find(f"{HT}picture")),
                "picture_source": _text(item.find(f"{HT}picture_source")),
                "news": [n for n in map(_parse_news_item, item.findall(f"{HT}news_item")) if n["url"]],
            }
        )
    return trends


def _load_trends(geo: str) -> tuple[list[dict[str, Any]] | None, dict[str, Any] | None]:
    """(trends, None) si todo fue bien; (None, payload de error) si no."""
    try:
        trends = _fetch_trending(geo)
    except Exception as exc:
        return None, {
            "ok": False,
            "geo": geo,
            "error": f"Could not read the trends feed: {type(exc).__name__}: {exc}",
            "hint": "Check the country code (ISO alpha-2), or retry in a few minutes.",
        }
    if not trends:
        return None, {
            "ok": False,
            "geo": geo,
            "error": "Google returned no trends.",
            "hint": "Check the country code (ISO alpha-2).",
        }
    return trends, None


def _sort_trends(trends: list[dict[str, Any]], sort: str) -> list[dict[str, Any]]:
    if sort == "volume":
        return sorted(trends, key=lambda t: t["volume"] or 0, reverse=True)
    return trends  # 'recency' = el orden nativo del feed (mas reciente primero)


# --------------------------------------------------------------------------- #
# Tool 1: tendencias por pais
# --------------------------------------------------------------------------- #


@mcp.tool
def get_daily_trends(
    geo: Annotated[
        str,
        Field(description="ISO 3166-1 alpha-2 country code (e.g. 'US', 'GB', 'CA', 'AU', 'ES')."),
    ] = "US",
    limit: Annotated[int, Field(ge=1, le=25, description="Maximum number of trending topics to return.")] = 10,
    sort: Annotated[
        Literal["recency", "volume"],
        Field(description="'recency' keeps the feed order (newest trend first); 'volume' sorts by search volume."),
    ] = "recency",
    include_news: Annotated[
        bool,
        Field(description="Attach the news articles of each trend (no extra requests; makes the response larger)."),
    ] = False,
) -> dict[str, Any]:
    """Fetch the trending searches that most recently took off in a country.

    Source: Google Trends' public "Trending now" RSS feed, which lists the ~10
    trends that started most recently, each with an approximate search volume
    and the news articles behind it. Use this to pick a topic, then
    `get_trend_articles` for its sources and `get_search_metrics` to validate
    demand.

    Volumes are Google's absolute approximations ("20000+" -> 20000), NOT the
    relative 0-100 index that `get_search_metrics` returns.

    Args:
        geo: Country code. Use 'US' for a US-focused English-speaking audience.
        limit: How many topics to return, after sorting.
        sort: 'recency' (feed order) or 'volume'.
        include_news: Attach articles inline. Prefer leaving it off and calling
            `get_trend_articles` for the chosen topic.

    Returns:
        dict with `geo`, `total_available`, `count` and a `trends` list carrying
        title, volume, start time and the explore URL.
    """
    geo = geo.strip().upper()
    trends, error = _load_trends(geo)
    if error:
        return error

    results = []
    for trend in _sort_trends(trends, sort)[:limit]:
        entry = {k: v for k, v in trend.items() if k != "news"}
        if include_news:
            entry["news"] = trend["news"]
        else:
            entry["news_available"] = bool(trend["news"])
        results.append(entry)

    return {
        "ok": True,
        "geo": geo,
        "sort": sort,
        "source": "rss",
        "fetched_at": _now_iso(),
        "total_available": len(trends),
        "count": len(results),
        "trends": results,
    }


# --------------------------------------------------------------------------- #
# Tool 2: articulos de noticias de una tendencia concreta
# --------------------------------------------------------------------------- #


@mcp.tool
def get_trend_articles(
    trend: Annotated[
        str,
        Field(
            description=(
                "Trend title to look up, exactly as returned by `get_daily_trends` "
                "(a partial, case-insensitive match also works)."
            )
        ),
    ],
    geo: Annotated[
        str, Field(description="ISO 3166-1 alpha-2 country code. Must match the one used to find the trend.")
    ] = "US",
    limit: Annotated[int, Field(ge=1, le=10, description="Maximum number of articles to return.")] = 10,
) -> dict[str, Any]:
    """Get the news articles behind one trending topic - the sources for the article.

    These are the articles Google Trends shows for a trend in its "In the news"
    panel. URLs are DIRECT publisher links (billboard.com, espn.com, bbc.com,
    ...), never Google redirects, so they can be handed straight to a
    writing/publishing tool as source material.

    Typical flow: `get_daily_trends` to see what is trending -> pick a topic ->
    `get_trend_articles` to collect its sources -> write and publish.

    Args:
        trend: The trend title (exact or partial, case-insensitive).
        geo: Same country code used with `get_daily_trends`.
        limit: Maximum number of articles.

    Returns:
        dict with the matched trend, its `articles` (title, source, url, image,
        snippet) and `urls`: a flat list of the links, ready to pass on.
    """
    geo = geo.strip().upper()
    needle = trend.strip().lower()
    if not needle:
        return {"ok": False, "error": "You must provide a trend title."}

    trends, error = _load_trends(geo)
    if error:
        return error

    # Coincidencia exacta primero; si no, la primera parcial (la mas reciente).
    matched = next((t for t in trends if t["title"].lower() == needle), None)
    matched_by = "exact"
    if matched is None:
        matched = next(
            (t for t in trends if needle in t["title"].lower() or t["title"].lower() in needle),
            None,
        )
        matched_by = "partial"

    if matched is None:
        return {
            "ok": False,
            "geo": geo,
            "error": f"Trend '{trend}' is not among the {len(trends)} current trends for {geo}.",
            "hint": (
                "The feed only holds the most recent trends and they rotate quickly. "
                "Call `get_daily_trends` again and use one of the returned titles."
            ),
            "available_trends": [t["title"] for t in trends],
        }

    articles = matched["news"][:limit]
    payload = {
        "ok": True,
        "geo": geo,
        "trend": matched["title"],
        "matched_by": matched_by,
        "rank": matched["rank"],
        "volume": matched["volume"],
        "volume_label": matched["volume_label"],
        "started": matched["started"],
        "explore_url": matched["explore_url"],
        "source": "rss",
        "fetched_at": _now_iso(),
        "article_count": len(articles),
        "articles": articles,
        "urls": [a["url"] for a in articles],
        "note": (
            "No articles are linked to this trend."
            if not articles
            else "Direct publisher URLs, ready to use as sources."
        ),
    }
    return payload


# --------------------------------------------------------------------------- #
# Tool 3: metricas de busqueda por keyword
# --------------------------------------------------------------------------- #

def _series_metrics(keyword: str, series: list[tuple[datetime, float]]) -> dict[str, Any]:
    """Calcula el bloque de metricas de una serie temporal de interes (0-100)."""
    values = [value for _, value in series]
    dates = [date for date, _ in series]
    n = len(values)

    if n == 0 or max(values) == 0:
        return {
            "keyword": keyword,
            "status": "no_data",
            "note": "Google Trends reports insufficient volume for this keyword in this country/period.",
        }

    average = statistics.fmean(values)
    peak = max(values)
    peak_index = values.index(peak)
    latest = values[-1]

    # Momentum: ultimos 4 puntos vs los 4 previos (con datos semanales ~ mes vs mes).
    window = max(1, min(4, n // 2))
    recent = statistics.fmean(values[-window:])
    previous = statistics.fmean(values[-2 * window: -window]) if n >= 2 * window else None
    momentum_pct = round((recent - previous) / previous * 100, 1) if previous else None

    # Pendiente por minimos cuadrados sobre el indice del punto, normalizada al promedio.
    x_mean = (n - 1) / 2
    denominator = sum((i - x_mean) ** 2 for i in range(n))
    slope = (
        sum((i - x_mean) * (values[i] - average) for i in range(n)) / denominator
        if denominator
        else 0.0
    )
    slope_pct_per_period = round(slope / average * 100, 2) if average else 0.0

    # Volatilidad = coeficiente de variacion.
    volatility_pct = round(statistics.pstdev(values) / average * 100, 1) if average else 0.0

    # Estacionalidad: promedio por mes calendario dentro de la ventana consultada.
    monthly: dict[int, list[float]] = {}
    for date, value in series:
        monthly.setdefault(date.month, []).append(value)
    monthly_avg = {MONTHS[m - 1]: round(statistics.fmean(v), 1) for m, v in sorted(monthly.items())}
    best_month = max(monthly_avg, key=monthly_avg.get) if monthly_avg else None
    worst_month = min(monthly_avg, key=monthly_avg.get) if monthly_avg else None

    current_month = MONTHS[datetime.now(UTC).month - 1]
    current_month_avg = monthly_avg.get(current_month)
    best_month_avg = monthly_avg.get(best_month) if best_month else None
    season_fit = (
        round(current_month_avg / best_month_avg * 100, 1)
        if current_month_avg and best_month_avg
        else None
    )

    # Etiqueta de tendencia a partir de pendiente + momentum.
    signal = slope_pct_per_period * 4 + (momentum_pct or 0)
    if signal > 25:
        trend_label = "rising"
    elif signal < -25:
        trend_label = "declining"
    else:
        trend_label = "stable"

    # Score compuesto 0-100: demanda sostenida + momentum + encaje estacional.
    demand_component = _clamp(average)
    momentum_component = _clamp(50 + (momentum_pct or 0))
    season_component = _clamp(season_fit if season_fit is not None else 60.0)
    opportunity_score = round(
        0.40 * demand_component + 0.35 * momentum_component + 0.25 * season_component
    )

    return {
        "keyword": keyword,
        "status": "ok",
        "average_interest": round(average, 1),
        "peak_interest": round(peak, 1),
        "peak_date": dates[peak_index].date().isoformat(),
        "latest_interest": round(latest, 1),
        "momentum_pct": momentum_pct,
        "slope_pct_per_period": slope_pct_per_period,
        "volatility_pct": volatility_pct,
        "trend": trend_label,
        "is_breakout": bool(latest >= 90 and (momentum_pct or 0) >= 50),
        "seasonality": {
            "monthly_average": monthly_avg,
            "best_month": best_month,
            "worst_month": worst_month,
            "current_month": current_month,
            "season_fit_pct": season_fit,
        },
        "opportunity_score": opportunity_score,
    }


@mcp.tool
def get_search_metrics(
    keywords: Annotated[
        list[str],
        Field(min_length=1, max_length=5, description="1-5 keywords to compare (Google Trends hard limit is 5)."),
    ],
    geo: Annotated[
        str,
        Field(description="ISO 3166-1 alpha-2 country code, or '' for worldwide. Use 'US' for a US audience."),
    ] = "US",
    timeframe: Annotated[
        str,
        Field(
            description=(
                "Google Trends timeframe: 'now 7-d', 'today 1-m', 'today 3-m', 'today 12-m', "
                "'today 5-y', 'all', or an explicit range 'YYYY-MM-DD YYYY-MM-DD'."
            )
        ),
    ] = "today 12-m",
    include_related: Annotated[
        bool,
        Field(description="Also return top and rising related queries per keyword (extra article angles)."),
    ] = True,
) -> dict[str, Any]:
    """Compute search-demand metrics for up to 5 keywords from Google Trends.

    Pulls the interest-over-time curve and derives, per keyword: average and peak
    interest, current momentum, trend slope, volatility, month-by-month
    seasonality and a composite `opportunity_score` (0-100). Use it to decide
    whether a trending topic is worth an article now, and which of several
    candidate angles has the strongest demand.

    IMPORTANT: interest values are Google's relative 0-100 index, normalised
    across the keywords in THIS single request and this timeframe. They are not
    absolute search volumes and are not comparable between separate calls, so
    pass competing candidates together in one call to rank them.

    `opportunity_score` = 0.40*average_interest + 0.35*(50+momentum_pct)
                        + 0.25*season_fit_pct, each term clamped to 0-100.

    Args:
        keywords: 1-5 search terms, compared against each other.
        geo: Country code ('US' by default), or '' for worldwide.
        timeframe: Google Trends timeframe string.
        include_related: Attach top/rising related queries per keyword.

    Returns:
        dict with per-keyword `metrics`, a `ranking` by opportunity score, and
        the `resolution` (hourly/daily/weekly/monthly) of the underlying series.
    """
    terms = [k.strip() for k in keywords if k and k.strip()]
    if not terms:
        return {"ok": False, "error": "You must provide at least one keyword."}
    if len(terms) > 5:
        return {
            "ok": False,
            "error": f"Google Trends accepts at most 5 keywords per request (received: {len(terms)}).",
        }

    geo = geo.strip().upper()

    # Import diferido: pytrends arrastra pandas y encarece el arranque del servidor.
    from pytrends.request import TrendReq

    def fetch() -> tuple[Any, Any]:
        # Sesion nueva en cada intento: fuerza un token de widget limpio tras un 429.
        client = TrendReq(hl="en-US", tz=0, timeout=(10, 20))
        client.build_payload(terms, cat=0, timeframe=timeframe, geo=geo, gprop="")
        return client, client.interest_over_time()

    try:
        pytrends, frame = _retry(fetch)
    except Exception as exc:  # pytrends lanza excepciones propias y de requests
        message = str(exc)
        rate_limited = _is_rate_limited(exc)
        return {
            "ok": False,
            "keywords": terms,
            "geo": geo,
            "timeframe": timeframe,
            "error": f"{type(exc).__name__}: {message}",
            "rate_limited": rate_limited,
            "hint": (
                "Google is rate limiting requests (429). Wait a few minutes and retry, "
                "or reduce the call frequency."
                if rate_limited
                else "Check the `timeframe` format and the country code."
            ),
        }

    if frame is None or frame.empty:
        return {
            "ok": False,
            "keywords": terms,
            "geo": geo,
            "timeframe": timeframe,
            "error": "Google Trends returned no data (insufficient volume for these keywords).",
        }

    # Se descartan los puntos parciales: el periodo aun incompleto hunde el momentum.
    partial_points = 0
    if "isPartial" in frame.columns:
        flags = frame["isPartial"].fillna(False).astype(bool)
        partial_points = int(flags.sum())
        frame = frame[~flags].drop(columns=["isPartial"])

    if frame.empty:
        return {"ok": False, "error": "Only partial data points were returned; widen the `timeframe`."}

    # Resolucion real de la serie (Google la elige segun el timeframe).
    resolution = "unknown"
    if len(frame.index) > 1:
        delta_days = (frame.index[1] - frame.index[0]).days
        resolution = {0: "hourly", 1: "daily", 7: "weekly"}.get(delta_days)
        if resolution is None:
            resolution = "monthly" if delta_days >= 28 else f"{delta_days}-day"

    metrics: list[dict[str, Any]] = []
    for term in terms:
        if term not in frame.columns:
            metrics.append(
                {"keyword": term, "status": "no_data", "note": "Google returned no series for this keyword."}
            )
            continue
        series = [
            (timestamp.to_pydatetime(), float(value))
            for timestamp, value in frame[term].items()
            if not (isinstance(value, float) and math.isnan(value))
        ]
        metrics.append(_series_metrics(term, series))

    if include_related:
        related_error: str | None = None
        try:
            related = _retry(pytrends.related_queries, attempts=2)
        except Exception as exc:
            related, related_error = None, f"{type(exc).__name__}: {exc}"

        for entry in metrics:
            block = (related or {}).get(entry["keyword"]) or {}
            payload: dict[str, Any] = {}
            for bucket in ("top", "rising"):
                table = block.get(bucket)
                if table is not None and not table.empty:
                    payload[bucket] = table.head(10).to_dict(orient="records")
            if related_error:
                payload["error"] = related_error
            entry["related_queries"] = payload

    ranking = sorted(
        (m for m in metrics if m.get("status") == "ok"),
        key=lambda m: m["opportunity_score"],
        reverse=True,
    )

    return {
        "ok": True,
        "geo": geo or "WORLDWIDE",
        "timeframe": timeframe,
        "resolution": resolution,
        "data_points": int(len(frame.index)),
        "partial_points_dropped": partial_points,
        "fetched_at": _now_iso(),
        "scale_note": (
            "Interest is Google's relative 0-100 index within this request only; "
            "not comparable across separate calls."
        ),
        "metrics": metrics,
        "ranking": [
            {"keyword": m["keyword"], "opportunity_score": m["opportunity_score"], "trend": m["trend"]}
            for m in ranking
        ],
    }


if __name__ == "__main__":
    mcp.run()
