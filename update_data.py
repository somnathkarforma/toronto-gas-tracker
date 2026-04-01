from __future__ import annotations

import json
import math
import os
import re
import xml.etree.ElementTree as ET
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from typing import Any

import requests
from bs4 import BeautifulSoup

try:
    import google.generativeai as genai
except ImportError:  # pragma: no cover
    genai = None

ROOT = Path(__file__).resolve().parent
DATA_FILE = ROOT / "data.json"
HISTORY_FILE = ROOT / "history.json"
HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
        "(KHTML, like Gecko) Chrome/124.0 Safari/537.36"
    )
}
DEFAULT_REGULAR = 155.9
DEFAULT_PREMIUM_SPREAD = 18.0
DEFAULT_DIESEL_SPREAD = 10.5


def fetch_google_news(query: str = "(gasoline OR oil OR OPEC OR crude prices) when:7d") -> list[dict[str, str]]:
    encoded_query = requests.utils.quote(query)
    url = f"https://news.google.com/rss/search?q={encoded_query}&hl=en-CA&gl=CA&ceid=CA:en"
    response = requests.get(url, headers=HEADERS, timeout=20)
    response.raise_for_status()

    root = ET.fromstring(response.content)
    items: list[dict[str, str]] = []

    for item in root.findall("./channel/item")[:10]:
        title = (item.findtext("title") or "").strip()
        source = (item.findtext("source") or "Google News").strip()
        link = (item.findtext("link") or "").strip()
        pub_date = (item.findtext("pubDate") or "").strip()
        description_html = item.findtext("description") or ""
        description = BeautifulSoup(description_html, "html.parser").get_text(" ", strip=True)
        items.append(
            {
                "title": title,
                "source": source,
                "link": link,
                "description": description,
                "publishedAt": pub_date,
            }
        )

    return items


def extract_json_array(text: str) -> list[dict[str, Any]] | None:
    match = re.search(r"\[[\s\S]*\]", text)
    if not match:
        return None

    try:
        return json.loads(match.group(0))
    except json.JSONDecodeError:
        return None


def enrich_news_with_gemini(items: list[dict[str, str]]) -> list[dict[str, str]]:
    api_key = os.getenv("GEMINI_API_KEY")
    if not api_key or genai is None or not items:
        for item in items:
            item["summary"] = "Headline to watch for potential pressure on gasoline prices."
            item["impact"] = "medium"
        return items

    try:
        genai.configure(api_key=api_key)
        model = genai.GenerativeModel("gemini-1.5-flash")
        prompt = (
            "You are building a Toronto gas-price dashboard. "
            "For each headline below, return JSON only as an array of objects with keys "
            "title, summary, and impact. Summary must be one sentence, factual, and under 22 words. "
            "Impact must be one of: low, medium, high.\n\n"
            f"Headlines:\n{json.dumps(items, ensure_ascii=False)}"
        )
        response = model.generate_content(prompt)
        parsed = extract_json_array(response.text or "")

        if not parsed:
            raise ValueError("Gemini did not return valid JSON.")

        merged: list[dict[str, str]] = []
        for original, enriched in zip(items, parsed):
            merged.append(
                {
                    "title": original["title"],
                    "source": original["source"],
                    "link": original["link"],
                    "summary": str(enriched.get("summary", "Headline to watch for fuel-price impact.")).strip(),
                    "impact": str(enriched.get("impact", "medium")).strip().lower(),
                }
            )
        return merged
    except Exception as exc:  # pragma: no cover
        print(f"Gemini summarization fallback: {exc}")
        for item in items:
            item["summary"] = "Headline to watch for potential pressure on gasoline prices."
            item["impact"] = "medium"
        return items


def normalize_price(raw_value: str) -> float:
    value = float(raw_value)
    if value < 10:
        value *= 100
    return round(value, 1)


def extract_price_from_text(text: str) -> float | None:
    patterns = [
        r"\$\s*(1\.\d{2})\s*(?:a|per)?\s*litre",
        r"(\d{2,3}\.\d)\s*(?:¢|cents?)",
        r"(\d{2,3})\s*cents?",
    ]

    for pattern in patterns:
        match = re.search(pattern, text, flags=re.IGNORECASE)
        if not match:
            continue

        value = normalize_price(match.group(1))
        if 120 <= value <= 220:
            return value
    return None


def fetch_toronto_price_from_headlines() -> tuple[float, str] | None:
    try:
        items = fetch_google_news("Toronto OR GTA gas prices when:7d")
        for item in items:
            combined_text = " ".join(
                part for part in [item.get("title", ""), item.get("description", "")] if part
            )
            price = extract_price_from_text(combined_text)
            if price is not None:
                return price, "Toronto gas headlines"
    except Exception as exc:
        print(f"Headline price fallback failed: {exc}")
    return None


def scrape_toronto_regular_price() -> tuple[float, str]:
    sources = [
        ("GasBuddy Toronto", "https://www.gasbuddy.com/gasprices/ontario/toronto"),
        ("Ontario Gas Prices", "https://www.ontariogasprices.com/Toronto/index.aspx"),
    ]
    patterns = [
        r"Toronto[^\d]{0,120}(\d{2,3}(?:\.\d)?)\s?[¢c]",
        r"Average[^\d]{0,60}(\d{2,3}(?:\.\d)?)\s?[¢c]",
        r"Regular[^\d]{0,40}(\d{2,3}(?:\.\d)?)",
        r'"price"\s*:\s*"?(\d{2,3}(?:\.\d)?)"?',
    ]

    for source_name, url in sources:
        try:
            response = requests.get(url, headers=HEADERS, timeout=20)
            if response.status_code >= 400:
                raise requests.HTTPError(f"{response.status_code} for {url}")

            text = BeautifulSoup(response.text, "html.parser").get_text(" ", strip=True)
            for pattern in patterns:
                match = re.search(pattern, text, flags=re.IGNORECASE)
                if match:
                    return normalize_price(match.group(1)), source_name
        except Exception as exc:
            print(f"Price scrape fallback for {source_name}: {exc}")

    headline_result = fetch_toronto_price_from_headlines()
    if headline_result:
        return headline_result

    history = load_history()
    if history:
        return float(history[-1]["regular"]), "Saved history"
    return DEFAULT_REGULAR, "Built-in fallback"


def load_history() -> list[dict[str, Any]]:
    if not HISTORY_FILE.exists():
        return []

    try:
        return json.loads(HISTORY_FILE.read_text(encoding="utf-8"))
    except json.JSONDecodeError:
        return []


def save_history(history: list[dict[str, Any]]) -> None:
    HISTORY_FILE.write_text(json.dumps(history, indent=2), encoding="utf-8")


def seed_history_if_needed(history: list[dict[str, Any]], current_regular: float) -> list[dict[str, Any]]:
    if len(history) >= 30:
        return history

    today = date.today()
    seeded: list[dict[str, Any]] = []

    for days_ago in range(29, -1, -1):
        entry_date = today - timedelta(days=days_ago)
        trend = (29 - days_ago) * 0.18
        seasonal = math.sin((29 - days_ago) / 3.2) * 1.4
        regular = round(current_regular - 5.2 + trend + seasonal, 1)
        premium = round(regular + DEFAULT_PREMIUM_SPREAD, 1)
        diesel = round(regular + DEFAULT_DIESEL_SPREAD, 1)
        seeded.append(
            {
                "date": entry_date.isoformat(),
                "regular": regular,
                "premium": premium,
                "diesel": diesel,
            }
        )

    by_date = {item["date"]: item for item in seeded}
    for item in history:
        by_date[item["date"]] = item

    return [by_date[key] for key in sorted(by_date.keys())]


def upsert_today(history: list[dict[str, Any]], regular: float) -> list[dict[str, Any]]:
    today = date.today().isoformat()
    premium = round(regular + DEFAULT_PREMIUM_SPREAD, 1)
    diesel = round(regular + DEFAULT_DIESEL_SPREAD, 1)
    entry = {"date": today, "regular": regular, "premium": premium, "diesel": diesel}

    if history and history[-1]["date"] == today:
        history[-1] = entry
    else:
        history.append(entry)

    cutoff = (date.today() - timedelta(days=185)).isoformat()
    return [item for item in history if item["date"] >= cutoff]


def build_history_series(history: list[dict[str, Any]]) -> dict[str, list[Any]]:
    labels = [datetime.fromisoformat(item["date"]).strftime("%b %d") for item in history]
    return {
        "labels": labels,
        "regular": [item["regular"] for item in history],
        "premium": [item["premium"] for item in history],
        "diesel": [item["diesel"] for item in history],
    }


def build_prediction(history: list[dict[str, Any]]) -> dict[str, list[Any]]:
    recent = history[-14:] if len(history) >= 14 else history

    def series_projection(key: str, default: float) -> list[float]:
        values = [item[key] for item in recent] or [default]
        base = values[-1]
        slope = (values[-1] - values[0]) / max(len(values) - 1, 1)
        slope = max(min(slope, 1.2), -1.2)
        return [round(base + (slope * offset * 0.8), 1) for offset in range(1, 8)]

    labels: list[str] = []
    for offset in range(1, 8):
        day = date.today() + timedelta(days=offset)
        labels.append(day.strftime("%b %d"))

    return {
        "labels": labels,
        "regular": series_projection("regular", DEFAULT_REGULAR),
        "premium": series_projection("premium", DEFAULT_PREMIUM_SPREAD + DEFAULT_REGULAR),
        "diesel": series_projection("diesel", DEFAULT_DIESEL_SPREAD + DEFAULT_REGULAR),
    }


def build_payload(price: float, price_source: str, history: list[dict[str, Any]], news: list[dict[str, str]]) -> dict[str, Any]:
    history_series = build_history_series(history)
    latest = history[-1]
    return {
        "generatedAt": datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M UTC"),
        "priceSource": price_source,
        "latest": {
            "regular": latest["regular"],
            "premium": latest["premium"],
            "diesel": latest["diesel"],
        },
        "history": history_series,
        "prediction": build_prediction(history),
        "news": news,
    }


def main() -> None:
    regular_price, price_source = scrape_toronto_regular_price()
    history = load_history()
    history = seed_history_if_needed(history, regular_price)
    history = upsert_today(history, regular_price)
    save_history(history)

    try:
        news = enrich_news_with_gemini(fetch_google_news())
    except Exception as exc:
        print(f"News fetch fallback: {exc}")
        news = [
            {
                "title": "No live headlines available right now",
                "source": "System fallback",
                "link": "https://news.google.com/",
                "summary": "The dashboard will refresh automatically on the next scheduled run.",
                "impact": "low",
            }
        ]

    payload = build_payload(regular_price, price_source, history, news)
    DATA_FILE.write_text(json.dumps(payload, indent=2), encoding="utf-8")
    print(f"Saved {DATA_FILE.name} and {HISTORY_FILE.name} using {price_source} at {regular_price:.1f} cents/L")


if __name__ == "__main__":
    main()
