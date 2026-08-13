from __future__ import annotations
import argparse
import csv
import datetime as dt
import logging
import re
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Optional
from urllib.parse import urlencode

try:
    import requests
except ImportError:
    requests = None

from bs4 import BeautifulSoup

BASE_URL = "https://www.avito.ru/moskva_i_mo/zapchasti_i_aksessuary"
SEARCH_PARAMS = {"state": "new", "s": "104"}
REGION_KEYWORDS = ("москв", "моск. обл", "московская обл")
NEW_KEYWORDS = ("нов",)
TOP_N = 5
TIMEOUT = 10
USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36"
)
FIXTURES_DIR = Path(__file__).parent / "fixtures"
DEFAULT_ARTICLES = ("223112R020", "233002F700")
CSV_FIELDS = (
    "артикул",
    "поисковый_запрос",
    "заголовок",
    "цена",
    "город_или_регион",
    "состояние",
    "ссылка",
    "место_по_цене",
    "статус",
    "дата_время_проверки",
)

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    datefmt="%H:%M:%S",
)
log = logging.getLogger("avito_parser")


@dataclass(frozen=True)
class Listing:
    title: str
    price: Optional[int]
    location: str
    condition: str
    link: str


def build_search_url(query: str) -> str:
    return f"{BASE_URL}?{urlencode({'q': query, **SEARCH_PARAMS})}"


def fetch_live(url: str) -> Optional[str]:
    if requests is None:
        log.warning("requests недоступен — живой запрос пропущен")
        return None
    try:
        response = requests.get(
            url,
            headers={"User-Agent": USER_AGENT, "Accept-Language": "ru-RU,ru;q=0.9"},
            timeout=TIMEOUT,
        )
    except requests.RequestException as exc:
        log.warning("Сетевая ошибка для %s: %s", url, exc)
        return None
    if response.status_code != 200:
        log.warning("Avito вернул код %s для %s", response.status_code, url)
        return None
    lowered = response.text.lower()
    if "captcha" in lowered or "доступ ограничен" in lowered:
        log.warning("Похоже на капчу/блокировку для %s", url)
        return None
    return response.text


def get_page(article: str, url: str) -> tuple[Optional[str], str]:
    html = fetch_live(url)
    if html is not None:
        return html, "live"
    fixture = FIXTURES_DIR / f"{article}.html"
    if fixture.exists():
        log.info("Использую сохранённую страницу fixtures/%s.html", article)
        return fixture.read_text(encoding="utf-8"), "fixture"
    return None, "none"


def extract_price(raw: Optional[str]) -> Optional[int]:
    if not raw:
        return None
    digits = re.sub(r"\D", "", raw)
    return int(digits) if digits else None


def absolute_link(href: str) -> str:
    return href if href.startswith("http") else f"https://www.avito.ru{href}"


def parse_avito_cards(soup: BeautifulSoup) -> list[Listing]:
    listings = []
    for card in soup.select('[data-marker="item"]'):
        title_el = card.select_one('[itemprop="name"]') or card.select_one(
            '[data-marker="item-title"]'
        )
        link_el = (
            card.select_one('a[data-marker="item-title"]')
            or card.select_one("a[itemprop='url']")
            or card.select_one("a[href]")
        )
        price_el = card.select_one('[data-marker="item-price"]') or card.select_one(
            'meta[itemprop="price"]'
        )
        location_el = card.select_one('[data-marker="item-address"]')
        condition_el = card.select_one('[data-marker="item-condition"]')
        price_raw = None
        if price_el is not None:
            price_raw = (
                price_el.get("content") if price_el.name == "meta" else price_el.get_text()
            )
        listings.append(
            Listing(
                title=title_el.get_text(strip=True) if title_el else "",
                price=extract_price(price_raw),
                location=location_el.get_text(strip=True) if location_el else "",
                condition=condition_el.get_text(strip=True) if condition_el else "",
                link=absolute_link(link_el["href"]) if link_el and link_el.get("href") else "",
            )
        )
    return listings


def parse_fixture_cards(soup: BeautifulSoup) -> list[Listing]:
    listings = []
    for card in soup.select("article.avito-item"):
        url = card.get("data-url") or ""
        listings.append(
            Listing(
                title=card.get("data-title") or "",
                price=extract_price(card.get("data-price")),
                location=card.get("data-location") or "",
                condition=card.get("data-condition") or "",
                link=absolute_link(url) if url else "",
            )
        )
    return listings


def parse_listings(html: str) -> list[Listing]:
    soup = BeautifulSoup(html, "html.parser")
    return parse_avito_cards(soup) or parse_fixture_cards(soup)


def link_key(link: str) -> str:
    return link.split("?", 1)[0].split("#", 1)[0].rstrip("/")


def deduplicate(listings: list[Listing]) -> list[Listing]:
    best: dict = {}
    order: list = []
    for item in listings:
        key = link_key(item.link) if item.link else (item.title, item.price)
        if key not in best:
            best[key] = item
            order.append(key)
        elif item.price is not None and (best[key].price is None or item.price < best[key].price):
            best[key] = item
    return [best[key] for key in order]


def matches_filters(item: Listing) -> bool:
    if item.price is None:
        return False
    if not item.condition or not any(k in item.condition.lower() for k in NEW_KEYWORDS):
        return False
    if not item.location or not any(k in item.location.lower() for k in REGION_KEYWORDS):
        return False
    return True


def filter_and_rank(listings: list[Listing]) -> list[Listing]:
    valid = [item for item in deduplicate(listings) if matches_filters(item)]
    valid.sort(key=lambda item: item.price or 0)
    return valid[:TOP_N]


def status_row(article: str, url: str, status: str, checked_at: str) -> dict:
    return {
        "артикул": article,
        "поисковый_запрос": article,
        "заголовок": "",
        "цена": "",
        "город_или_регион": "",
        "состояние": "",
        "ссылка": url,
        "место_по_цене": "",
        "статус": status,
        "дата_время_проверки": checked_at,
    }


def listing_row(
    article: str, item: Listing, rank: int, source: str, checked_at: str
) -> dict:
    return {
        "артикул": article,
        "поисковый_запрос": article,
        "заголовок": item.title,
        "цена": item.price,
        "город_или_регион": item.location,
        "состояние": item.condition,
        "ссылка": item.link,
        "место_по_цене": rank,
        "статус": f"ок ({source})",
        "дата_время_проверки": checked_at,
    }


def process_article(article: str) -> list[dict]:
    checked_at = dt.datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    url = build_search_url(article)
    html, source = get_page(article, url)

    if html is None:
        return [status_row(article, url, "ошибка", checked_at)]

    top = filter_and_rank(parse_listings(html))
    if not top:
        return [status_row(article, url, "не найдено", checked_at)]

    return [
        listing_row(article, item, rank, source, checked_at)
        for rank, item in enumerate(top, start=1)
    ]


def read_articles(path: Path) -> list[str]:
    with path.open(encoding="utf-8") as handle:
        return [row[0].strip() for row in csv.reader(handle) if row and row[0].strip()]


def write_csv(rows: list[dict], path: Path) -> None:
    with path.open("w", encoding="utf-8-sig", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=CSV_FIELDS)
        writer.writeheader()
        writer.writerows(rows)


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Мини-парсер объявлений Avito")
    parser.add_argument("--articles", type=Path, default=None)
    parser.add_argument("--out", type=Path, default=Path("result.csv"))
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    articles = read_articles(args.articles) if args.articles else list(DEFAULT_ARTICLES)
    rows = [row for article in articles for row in process_article(article)]
    write_csv(rows, args.out)
    log.info("Готово: %s строк записано в %s", len(rows), args.out)
    return 0

if __name__ == "__main__":
    sys.exit(main())