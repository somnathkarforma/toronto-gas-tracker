"""
update_data.py — Toronto Gas Tracker data pipeline
Runs daily via GitHub Actions. Fetches gas prices and news,
writes data.json and history.json for the static GitHub Pages site.
"""

from __future__ import annotations

import json
import logging
import math
import os
import re
import sys
import time
import xml.etree.ElementTree as ET
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo

import requests
from bs4 import BeautifulSoup

try:
    import google.generativeai as genai
except ImportError:
    genai = None

# ── Structured logging ────────────────────────────────────────────────────────
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
    datefmt="%Y-%m-%dT%H:%M:%SZ",
    handlers=[logging.StreamHandler(sys.stdout)],
)
log = logging.getLogger("gas_tracker")

# ── Paths and constants ───────────────────────────────────────────────────────
ROOT = Path(__file__).resolve().parent
DATA_FILE = ROOT / "data.json"
HISTORY_FILE = ROOT / "history.json"

HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0 Safari/537.36"
    ),
    "Accept-Language": "en-CA,en;q=0.9",
}

DEFAULT_REGULAR = 174.9
DEFAULT_PREMIUM_SPREAD = 18.0
DEFAULT_DIESEL_SPREAD = 10.5

TORONTO_TZ = ZoneInfo("America/Toronto")
CITYNEWS_GTA_URL = "https://toronto.citynews.ca/toronto-gta-gas-prices/"

HISTORY_RETENTION_DAYS = 185
MAX_NEWS_ITEMS = 10
HTTP_TIMEOUT = 20
HTTP_MAX_RETRIES = 3
HTTP_BACKOFF_BASE = 2.0


# ── Custom exception hierarchy ────────────────────────────────────────────────
class GasTrackerError(Exception):
    """Base exception for all gas tracker errors."""


class PriceFetchError(GasTrackerError):
    """Raised when all price-fetching strategies are exhausted."""


class NewsFetchError(GasTrackerError):
    """Raised when the news RSS feed cannot be retrieved."""


class DataWriteError(GasTrackerError):
    """Raised when data.json or history.json cannot be written."""


# ── HTTP with retry + exponential back-off ────────────────────────────────────
def http_get(url: str, *, timeout: int = HTTP_TIMEOUT, verify: bool = True) -> requests.Response:
    last_exc: Exception | None = None

    for attempt in range(1, HTTP_MAX_RETRIES + 1):
        try:
            log.debug("HTTP GET attempt %d/%d", attempt, HTTP_MAX_RETRIES)
            resp = requests.get(url, headers=HEADERS, timeout=timeout, verify=verify)
            resp.raise_for_status()
            return resp
        except requests.exceptions.SSLError as exc:
            log.warning("SSL error on attempt %d — aborting retries: %s", attempt, exc)
            raise
        except (requests.exceptions.ConnectionError, requests.exceptions.Timeout) as exc:
            last_exc = exc
            if attempt < HTTP_MAX_RETRIES:
                wait = HTTP_BACKOFF_BASE ** attempt
                log.warning("Transient error on attempt %d, retrying in %.1fs: %s", attempt, wait, exc)
                time.sleep(wait)
        except requests.exceptions.HTTPError as exc:
            last_exc = exc
            status = exc.response.status_code if exc.response is not None else "?"
            if exc.response is not None and exc.response.status_code < 500:
                log.warning("Non-retryable HTTP %s — skipping further attempts", status)
                raise
            if attempt < HTTP_MAX_RETRIES:
                wait = HTTP_BACKOFF_BASE ** attempt
                log.warning("HTTP %s on attempt %d, retrying in %.1fs", status, attempt, wait)
                time.sleep(wait)

    raise last_exc or requests.exceptions.RequestException("All HTTP retry attempts exhausted")


# ── News fetching ─────────────────────────────────────────────────────────────
def fetch_google_news(
    query: str = "(gasoline OR oil OR OPEC OR crude prices) when:7d",
) -> list[dict[str, str]]:
    encoded = requests.utils.quote(query)
    url = f"https://news.google.com/rss/search?q={encoded}&hl=en-CA&gl=CA&ceid=CA:en"
    log.info("Fetching news RSS for query: %r", query)

    try:
        resp = http_get(url)
    except Exception as exc:
        raise NewsFetchError(f"Failed to fetch Google News RSS: {exc}") from exc

    try:
        root = ET.fromstring(resp.content)
    except ET.ParseError as exc:
        raise NewsFetchError(f"RSS XML parse error: {exc}") from exc

    items: list[dict[str, str]] = []
    for item in root.findall("./channel/item")[:MAX_NEWS_ITEMS]:
        title = (item.findtext("title") or "").strip()
        source = (item.findtext("source") or "Google News").strip()
        link = (item.findtext("link") or "").strip()
        pub_date = (item.findtext("pubDate") or "").strip()
        desc_html = item.findtext("description") or ""
        description = BeautifulSoup(desc_html, "html.parser").get_text(" ", strip=True)
        items.append({
            "title": title,
            "source": source,
            "link": link,
            "description": description,
            "publishedAt": pub_date,
        })

    log.info("Fetched %d news items", len(items))
    return items


# ── Gemini enrichment ─────────────────────────────────────────────────────────
def _apply_fallback_enrichment(items: list[dict[str, str]]) -> list[dict[str, str]]:
    for item in items:
        item.setdefault("summary", "Headline to watch for potential pressure on gasoline prices.")
        item.setdefault("impact", "medium")
    return items


def enrich_news_with_gemini(items: list[dict[str, str]]) -> list[dict[str, str]]:
    api_key = os.getenv("GEMINI_API_KEY")

    if not api_key:
        log.warning("GEMINI_API_KEY not set — skipping AI enrichment")
        return _apply_fallback_enrichment(items)

    if genai is None:
        log.warning("google-generativeai not installed — skipping AI enrichment")
        return _apply_fallback_enrichment(items)

    if not items:
        return items

    try:
        genai.configure(api_key=api_key)
        model = genai.GenerativeModel("gemini-2.0-flash")

        payload = [
            {"title": i.get("title", ""), "source": i.get("source", "")}
            for i in items
        ]

        prompt = (
            "You are a Toronto gas-price dashboard assistant. "
            "For each headline below, return ONLY a JSON array of objects with keys: "
            "title (string, exact copy from input), summary (one factual sentence, max 22 words), "
            "and impact (exactly one of: low, medium, high). "
            "No markdown fences, no preamble, no explanation — raw JSON only.\n\n"
            f"Headlines:\n{json.dumps(payload, ensure_ascii=False)}"
        )

        response = model.generate_content(prompt)
        raw = (response.text or "").strip()

        raw = re.sub(r"^```(?:json)?\s*", "", raw)
        raw = re.sub(r"\s*```$", "", raw)

        parsed: list[dict] = json.loads(raw)

        if not isinstance(parsed, list) or len(parsed) != len(items):
            raise ValueError(f"Gemini returned {len(parsed)} items for {len(items)} inputs")

        VALID_IMPACTS = {"low", "medium", "high"}
        merged: list[dict[str, str]] = []
        for original, enriched in zip(items, parsed):
            impact = str(enriched.get("impact", "medium")).strip().lower()
            if impact not in VALID_IMPACTS:
                impact = "medium"
            merged.append({
                "title": original["title"],
                "source": original["source"],
                "link": original["link"],
                "publishedAt": original.get("publishedAt", ""),
                "summary": str(enriched.get("summary", "")).strip(),
                "impact": impact,
            })

        log.info("Gemini enrichment succeeded for %d items", len(merged))
        return merged

    except json.JSONDecodeError as exc:
        log.error("Gemini returned non-JSON response: %s", exc)
    except ValueError as exc:
        log.error("Gemini response validation failed: %s", exc)
    except Exception as exc:
        log.error("Gemini enrichment failed (%s): %s", type(exc).__name__, exc)

    return _apply_fallback_enrichment(items)


# ── Price normalisation ───────────────────────────────────────────────────────
def normalize_price(raw_value: str) -> float:
    value = float(raw_value)
    if value < 10:
        value *= 100
    return round(value, 1)


def extract_price_from_text(text: str) -> float | None:
    patterns = [
        r"\$\s*(1\.\d{2,3})\s*(?:a|per)?\s*litre",
        r"(\d{2,3}\.\d)\s*(?:¢|cents?)",
        r"(\d{2,3})\s*cents?\s*(?:per)?\s*litre",
    ]
    for pattern in patterns:
        match = re.search(pattern, text, flags=re.IGNORECASE)
        if not match:
            continue
        try:
            value = normalize_price(match.group(1))
            if 120 <= value <= 220:
                return value
        except (ValueError, TypeError):
            continue
    return None


# ── Price scraping strategies ─────────────────────────────────────────────────
def today_in_toronto() -> date:
    """Calendar date in Toronto (avoids UTC day skew when CI runs near midnight)."""
    return datetime.now(TORONTO_TZ).date()


def scrape_citynews_gta_regular() -> float | None:
    """En-Pro / CityNews GTA average for regular (same figure as citynews.ca)."""
    try:
        resp = http_get(CITYNEWS_GTA_URL)
        text = BeautifulSoup(resp.text, "html.parser").get_text(" ", strip=True)
        m = re.search(
            r"average of\s+(\d{2,3}(?:\.\d)?)\s*cent(?:\(s\))?/litre",
            text,
            flags=re.IGNORECASE,
        )
        if m:
            price = normalize_price(m.group(1))
            if 120 <= price <= 220:
                log.info("CityNews: forecast price %.1f¢/L (En-Pro GTA average)", price)
                return price
        idx = text.lower().find("historical values")
        if idx != -1:
            tail = text[idx : idx + 6000]
            m2 = re.search(r"(\d{2,3}(?:\.\d)?)\s*cent(?:\(s\))?/litre", tail)
            if m2:
                price = normalize_price(m2.group(1))
                if 120 <= price <= 220:
                    log.info("CityNews: latest historical price %.1f¢/L", price)
                    return price
        log.warning("CityNews: no GTA regular price found on page")
    except Exception as exc:
        log.warning("CityNews scrape failed: %s", exc)
    return None


def _try_scrape_source(source_name: str, url: str) -> float | None:
    patterns = [
        r"Toronto[^\d]{0,120}(\d{2,3}(?:\.\d)?)\s?[¢c]",
        r"Average[^\d]{0,60}(\d{2,3}(?:\.\d)?)\s?[¢c]",
        r"Regular[^\d]{0,40}(\d{2,3}(?:\.\d)?)",
        r'"price"\s*:\s*"?(\d{2,3}(?:\.\d)?)"?',
    ]
    try:
        resp = http_get(url)
        text = BeautifulSoup(resp.text, "html.parser").get_text(" ", strip=True)
        for pattern in patterns:
            match = re.search(pattern, text, flags=re.IGNORECASE)
            if match:
                price = normalize_price(match.group(1))
                if 120 <= price <= 220:
                    log.info("Price %.1f¢/L extracted from %s", price, source_name)
                    return price
        log.warning("No matching price pattern found on %s", source_name)
    except Exception as exc:
        log.warning("Price scrape failed for %s: %s", source_name, exc)
    return None


def fetch_toronto_price_from_headlines() -> tuple[float, str] | None:
    log.info("Attempting headline-based price extraction")
    try:
        items = fetch_google_news("Toronto OR GTA gas prices when:7d")
        for item in items:
            combined = " ".join(filter(None, [item.get("title"), item.get("description")]))
            price = extract_price_from_text(combined)
            if price is not None:
                log.info("Extracted price %.1f¢/L from headlines", price)
                return price, "Toronto gas headlines"
    except Exception as exc:
        log.warning("Headline price extraction failed: %s", exc)
    return None


def scrape_toronto_regular_price() -> tuple[float, str]:
    citynews = scrape_citynews_gta_regular()
    if citynews is not None:
        return citynews, "CityNews Toronto & GTA (En-Pro)"

    sources = [
        ("GasBuddy Toronto", "https://www.gasbuddy.com/gasprices/ontario/toronto"),
        ("Ontario Gas Prices", "https://www.ontariogasprices.com/Toronto/index.aspx"),
        ("Global Petrol Prices", "https://www.globalpetrolprices.com/Canada/gasoline_prices/"),
    ]

    for source_name, url in sources:
        price = _try_scrape_source(source_name, url)
        if price is not None:
            return price, source_name

    headline_result = fetch_toronto_price_from_headlines()
    if headline_result:
        return headline_result

    log.warning("All price sources exhausted — using hardcoded default of %.1f¢/L", DEFAULT_REGULAR)
    return DEFAULT_REGULAR, "Hardcoded fallback"


# ── History management ────────────────────────────────────────────────────────
def load_history() -> list[dict[str, Any]]:
    if not HISTORY_FILE.exists():
        log.info("No history file found — starting fresh")
        return []
    try:
        data = json.loads(HISTORY_FILE.read_text(encoding="utf-8"))
        if not isinstance(data, list):
            raise ValueError("history.json root is not a list")
        log.info("Loaded %d history entries from %s", len(data), HISTORY_FILE.name)
        return data
    except (json.JSONDecodeError, ValueError) as exc:
        log.error("Corrupt history.json — resetting: %s", exc)
        return []


def save_history(history: list[dict[str, Any]]) -> None:
    try:
        HISTORY_FILE.write_text(json.dumps(history, indent=2), encoding="utf-8")
        log.info("Saved %d history entries to %s", len(history), HISTORY_FILE.name)
    except OSError as exc:
        raise DataWriteError(f"Cannot write {HISTORY_FILE}: {exc}") from exc


def seed_history_if_needed(
    history: list[dict[str, Any]], current_regular: float
) -> list[dict[str, Any]]:
    if history:
        return history

    log.info("Seeding 30-day synthetic history anchored at %.1f¢/L", current_regular)
    today = today_in_toronto()
    seeded: list[dict[str, Any]] = []

    for days_ago in range(29, -1, -1):
        entry_date = today - timedelta(days=days_ago)
        trend = (29 - days_ago) * 0.18
        seasonal = math.sin((29 - days_ago) / 3.2) * 1.4
        regular = round(current_regular - 5.2 + trend + seasonal, 1)
        premium = round(regular + DEFAULT_PREMIUM_SPREAD, 1)
        diesel = round(regular + DEFAULT_DIESEL_SPREAD, 1)
        seeded.append({
            "date": entry_date.isoformat(),
            "regular": regular,
            "premium": premium,
            "diesel": diesel,
        })

    return seeded


def upsert_today(history: list[dict[str, Any]], regular: float) -> list[dict[str, Any]]:
    today = today_in_toronto().isoformat()
    premium = round(regular + DEFAULT_PREMIUM_SPREAD, 1)
    diesel = round(regular + DEFAULT_DIESEL_SPREAD, 1)
    entry: dict[str, Any] = {
        "date": today,
        "regular": regular,
        "premium": premium,
        "diesel": diesel,
    }

    if history and history[-1]["date"] == today:
        history[-1] = entry
        log.info("Updated today's history entry: %s", entry)
    else:
        history.append(entry)
        log.info("Appended today's history entry: %s", entry)

    cutoff = (today_in_toronto() - timedelta(days=HISTORY_RETENTION_DAYS)).isoformat()
    pruned = [item for item in history if item["date"] >= cutoff]
    if len(pruned) < len(history):
        log.info("Pruned %d old history entries (older than %s)", len(history) - len(pruned), cutoff)
    return pruned


# ── Chart data builders ───────────────────────────────────────────────────────
def build_history_series(history: list[dict[str, Any]]) -> dict[str, list[Any]]:
    labels = [
        datetime.fromisoformat(item["date"]).strftime("%b %d")
        for item in history
    ]
    return {
        "labels": labels,
        "regular": [item["regular"] for item in history],
        "premium": [item["premium"] for item in history],
        "diesel":  [item["diesel"]  for item in history],
    }


def build_prediction(history: list[dict[str, Any]]) -> dict[str, list[Any]]:
    recent = history[-14:] if len(history) >= 14 else history

    def project(key: str, default: float) -> list[float]:
        values = [item[key] for item in recent] or [default]
        base = values[-1]
        slope = (values[-1] - values[0]) / max(len(values) - 1, 1)
        slope = max(min(slope, 1.2), -1.2)
        return [round(base + slope * i * 0.8, 1) for i in range(1, 8)]

    labels = [
        (today_in_toronto() + timedelta(days=i)).strftime("%b %d")
        for i in range(1, 8)
    ]
    return {
        "labels": labels,
        "regular": project("regular", DEFAULT_REGULAR),
        "premium": project("premium", DEFAULT_REGULAR + DEFAULT_PREMIUM_SPREAD),
        "diesel":  project("diesel",  DEFAULT_REGULAR + DEFAULT_DIESEL_SPREAD),
    }


# ── Payload assembly and write ────────────────────────────────────────────────
def build_payload(
    price_source: str,
    history: list[dict[str, Any]],
    news: list[dict[str, str]],
) -> dict[str, Any]:
    latest = history[-1]
    return {
        "generatedAt": datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M UTC"),
        "priceSource": price_source,
        "latest": {
            "regular": latest["regular"],
            "premium": latest["premium"],
            "diesel":  latest["diesel"],
        },
        "history":    build_history_series(history),
        "prediction": build_prediction(history),
        "news":       news,
    }


def write_data_json(payload: dict[str, Any]) -> None:
    try:
        DATA_FILE.write_text(json.dumps(payload, indent=2, ensure_ascii=False), encoding="utf-8")
        log.info("Written %s (%.1f KB)", DATA_FILE.name, DATA_FILE.stat().st_size / 1024)
    except OSError as exc:
        raise DataWriteError(f"Cannot write {DATA_FILE}: {exc}") from exc


# ── Entry point ───────────────────────────────────────────────────────────────
def main() -> None:
    log.info("=== Gas Tracker update started ===")

    regular_price, price_source = scrape_toronto_regular_price()
    log.info("Price: %.1f¢/L  Source: %s", regular_price, price_source)

    history = load_history()
    history = seed_history_if_needed(history, regular_price)
    history = upsert_today(history, regular_price)
    save_history(history)

    try:
        raw_news = fetch_google_news()
        news = enrich_news_with_gemini(raw_news)
    except NewsFetchError as exc:
        log.error("News fetch failed — using fallback: %s", exc)
        news = [{
            "title": "No live headlines available right now",
            "source": "System fallback",
            "link": "https://news.google.com/",
            "publishedAt": "",
            "summary": "The dashboard will refresh automatically on the next scheduled run.",
            "impact": "low",
        }]

    payload = build_payload(price_source, history, news)
    write_data_json(payload)

    log.info("=== Gas Tracker update complete ===")


if __name__ == "__main__":
    try:
        main()
    except DataWriteError as exc:
        log.critical("FATAL: %s", exc)
        sys.exit(1)
    except Exception as exc:
        log.critical("Unexpected error: %s: %s", type(exc).__name__, exc, exc_info=True)
        sys.exit(1)
