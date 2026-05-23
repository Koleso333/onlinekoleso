#!/usr/bin/env python3
"""
Подтягивает актуальные тарифы СПб с peterburg.center и обновляет
podorozhray/tariffs.json. Запускается GitHub Action раз в неделю.

Если всё развалилось (ни одна категория не извлеклась) — скрипт выходит
с кодом 1, файл не трогается, workflow падает и GitHub шлёт уведомление.
Частичный фейл (одна категория не нашлась, остальные ок) — норма, тогда
обновляем только то что нашли.
"""
from __future__ import annotations

import json
import re
import sys
from dataclasses import dataclass
from datetime import date
from pathlib import Path

import requests

TARIFFS_JSON = Path(__file__).parent.parent / "podorozhray" / "tariffs.json"

USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
    "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0 Safari/537.36"
)
TIMEOUT = 20
SANITY_MIN = 20
SANITY_MAX = 300

# Для каждой категории — список (URL, [regex...]). URL качается один раз,
# регэкспы пробуются по порядку, берётся первый сработавший.
# Поле `price` в регэкспе — обязательное.
SOURCES: dict[str, list[tuple[str, list[str]]]] = {
    "metro": [
        (
            "https://peterburg.center/ln/stoimost-proezda-v-sankt-peterburge.html",
            [
                r"по\s+Подорожнику\s+(?:один\s+проезд\s+)?стоит\s+(?P<price>\d{2,3})\s*рубл",
                r"Подорожник[^.]{0,60}?\(один\s+проезд\)\s*(?P<price>\d{2,3})\s*рубл",
                r"метро[^.]{0,200}?Подорожник[^.]{0,40}?(?P<price>\d{2,3})\s*рубл",
            ],
        ),
    ],
    "ground": [
        (
            "https://peterburg.center/ln/stoimost-proezda-v-sankt-peterburge.html",
            [
                # Секция "трамваи и автобусы", затем Подорожник + цена.
                r"трамва[^.]{0,500}?Подорожник[^.]{0,40}?(?P<price>\d{2,3})\s*рубл",
                r"автобус[^.]{0,500}?Подорожник[^.]{0,40}?(?P<price>\d{2,3})\s*рубл",
            ],
        ),
    ],
    "suburb": [
        # Пригород на этой странице явно не указан. Когда найдём источник —
        # добавим сюда (URL, [regex...]). Сейчас оставляем текущее значение.
    ],
}


@dataclass
class FetchResult:
    price: int
    source_url: str


_cache: dict[str, str] = {}


def fetch_html(url: str) -> str:
    if url in _cache:
        return _cache[url]
    headers = {"User-Agent": USER_AGENT, "Accept-Language": "ru,en;q=0.5"}
    r = requests.get(url, headers=headers, timeout=TIMEOUT)
    r.raise_for_status()
    # Сервер не всегда корректно объявляет charset; requests тогда падает на
    # ISO-8859-1. Используем apparent_encoding (chardet) или форсим utf-8.
    r.encoding = r.apparent_encoding or "utf-8"
    _cache[url] = normalize(r.text)
    return _cache[url]


def normalize(text: str) -> str:
    text = re.sub(r"<script\b.*?</script>", " ", text, flags=re.DOTALL | re.IGNORECASE)
    text = re.sub(r"<style\b.*?</style>", " ", text, flags=re.DOTALL | re.IGNORECASE)
    text = re.sub(r"<[^>]+>", " ", text)
    text = re.sub(r"&nbsp;|&#160;", " ", text)
    text = re.sub(r"\s+", " ", text)
    return text


def extract_price(item_id: str) -> FetchResult | None:
    for url, patterns in SOURCES.get(item_id, []):
        try:
            text = fetch_html(url)
        except Exception as e:
            print(f"  [{item_id}] fetch {url} failed: {e}", file=sys.stderr)
            continue
        for pattern in patterns:
            m = re.search(pattern, text, flags=re.IGNORECASE)
            if not m:
                continue
            try:
                price = int(m.group("price"))
            except (ValueError, IndexError):
                continue
            if not (SANITY_MIN <= price <= SANITY_MAX):
                print(f"  [{item_id}] price {price} out of sanity range, skipping", file=sys.stderr)
                continue
            return FetchResult(price=price, source_url=url)
        print(f"  [{item_id}] no pattern matched at {url}", file=sys.stderr)
    return None


def load_current() -> dict:
    return json.loads(TARIFFS_JSON.read_text(encoding="utf-8"))


def save(data: dict) -> None:
    text = json.dumps(data, ensure_ascii=False, indent=2) + "\n"
    TARIFFS_JSON.write_text(text, encoding="utf-8")


def main() -> int:
    current = load_current()
    items_by_id = {item["id"]: item for item in current["items"]}
    changed: list[str] = []
    failed: list[str] = []

    for item_id in items_by_id:
        if not SOURCES.get(item_id):
            print(f"[{item_id}] no source configured, keeping {items_by_id[item_id]['price']}")
            continue
        result = extract_price(item_id)
        if result is None:
            failed.append(item_id)
            continue
        old = items_by_id[item_id]["price"]
        if old != result.price:
            print(f"[{item_id}] {old} -> {result.price} (from {result.source_url})")
            items_by_id[item_id]["price"] = result.price
            changed.append(item_id)
        else:
            print(f"[{item_id}] unchanged ({old})")

    configured = [i for i in items_by_id if SOURCES.get(i)]
    if failed and len(failed) == len(configured):
        print(f"\nALL configured categories failed: {failed}", file=sys.stderr)
        return 1
    if failed:
        print(f"\nPartial failure for: {failed} — kept existing values.", file=sys.stderr)

    if changed:
        current["updated"] = date.today().isoformat()
        save(current)
        print(f"\nUpdated tariffs.json ({len(changed)} item(s) changed)")
    else:
        print("\nNo changes.")

    return 0


if __name__ == "__main__":
    sys.exit(main())
