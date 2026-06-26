"""
Monitor de precios y stock para productos de Stradivarius y Amazon.

Requisitos:
    pip install -r requirements.txt
    playwright install chromium

Configuracion:
    Copia .env.example como .env y rellena tus credenciales.
    Tambien puedes exportar las variables de entorno manualmente.
"""

from __future__ import annotations

import argparse
import asyncio
import gzip
import json
import logging
import os
import random
import re
import smtplib
import sqlite3
from dataclasses import dataclass
from datetime import datetime, timezone
from email.mime.multipart import MIMEMultipart
from email.mime.text import MIMEText
from pathlib import Path
from urllib.parse import urlparse
from urllib.parse import parse_qs
from typing import Any

import requests
from apscheduler.schedulers.blocking import BlockingScheduler
from playwright.async_api import Browser, Page, Response, async_playwright
from playwright_stealth import Stealth


APP_DIR = Path(__file__).resolve().parent
LOG_PATH = APP_DIR / "app.log"

USER_AGENTS = [
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/125.0.0.0 Safari/537.36",
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 13_6) AppleWebKit/605.1.15 "
    "(KHTML, like Gecko) Version/17.4 Safari/605.1.15",
    "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36",
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64; rv:126.0) Gecko/20100101 Firefox/126.0",
]

COLOR_NAMES = {
    "amarillo", "azul", "beige", "blanco", "burdeos", "camel", "crudo",
    "dorado", "gris", "kaki", "lila", "marron", "marino", "morado",
    "naranja", "negro", "plateado", "rojo", "rosa", "verde",
}
CLOTHING_SIZES = ("XXS", "XS", "S", "M", "L", "XL", "XXL")
SOLD_OUT_WORDS = ("agotado", "sin stock", "no disponible", "out of stock", "sold out")
AMAZON_DOMAINS = ("amazon.", "amzn.")


@dataclass
class Settings:
    product_urls: list[str]
    check_interval_minutes: int
    database_path: Path
    telegram_bot_token: str
    telegram_chat_id: str
    email_enabled: bool
    smtp_host: str
    smtp_port: int
    smtp_username: str
    smtp_password: str
    email_from: str
    email_to: str


@dataclass
class ProductSnapshot:
    url: str
    name: str
    price: float | None
    currency: str
    in_stock: bool
    sizes: dict[str, bool]
    source: str
    raw_summary: str
    color: str = ""
    reference: str = ""
    product_type: str = ""
    measurements: dict[str, str] | None = None
    image_urls: list[str] | None = None
    status: str = "ok"
    error: str = ""


def setup_logging() -> None:
    logging.basicConfig(
        filename=LOG_PATH,
        level=logging.INFO,
        format="%(asctime)s [%(levelname)s] %(message)s",
        encoding="utf-8",
    )
    console = logging.StreamHandler()
    console.setLevel(logging.INFO)
    console.setFormatter(logging.Formatter("%(asctime)s [%(levelname)s] %(message)s"))
    logging.getLogger().addHandler(console)


def load_dotenv(path: Path = APP_DIR / ".env") -> None:
    """Carga un .env simple sin anadir una dependencia extra."""
    if not path.exists():
        return

    for line in path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        os.environ.setdefault(key.strip(), value.strip().strip('"').strip("'"))


def get_settings() -> Settings:
    load_dotenv()
    urls = [
        item.strip()
        for item in os.getenv("PRODUCT_URLS", "").split(",")
        if item.strip()
    ]

    return Settings(
        product_urls=urls,
        check_interval_minutes=int(os.getenv("CHECK_INTERVAL_MINUTES", "5")),
        database_path=APP_DIR / os.getenv("DATABASE_PATH", "stradivarius_monitor.db"),
        telegram_bot_token=os.getenv("TELEGRAM_BOT_TOKEN", ""),
        telegram_chat_id=os.getenv("TELEGRAM_CHAT_ID", ""),
        email_enabled=os.getenv("EMAIL_ENABLED", "false").lower() == "true",
        smtp_host=os.getenv("SMTP_HOST", "smtp.gmail.com"),
        smtp_port=int(os.getenv("SMTP_PORT", "587")),
        smtp_username=os.getenv("SMTP_USERNAME", ""),
        smtp_password=os.getenv("SMTP_PASSWORD", ""),
        email_from=os.getenv("EMAIL_FROM", ""),
        email_to=os.getenv("EMAIL_TO", ""),
    )


def init_db(db_path: Path) -> None:
    with sqlite3.connect(db_path) as conn:
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS products (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                url TEXT NOT NULL UNIQUE,
                name TEXT,
                current_price REAL,
                currency TEXT,
                in_stock INTEGER,
                sizes_json TEXT,
                last_checked_at TEXT,
                created_at TEXT NOT NULL
            )
            """
        )
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS price_stock_history (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                product_id INTEGER NOT NULL,
                checked_at TEXT NOT NULL,
                price REAL,
                currency TEXT,
                in_stock INTEGER NOT NULL,
                sizes_json TEXT,
                source TEXT,
                raw_summary TEXT,
                color TEXT,
                reference TEXT,
                product_type TEXT,
                measurements_json TEXT,
                image_urls_json TEXT,
                FOREIGN KEY(product_id) REFERENCES products(id)
            )
            """
        )
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS users (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                name TEXT,
                telegram_chat_id TEXT,
                email TEXT,
                created_at TEXT NOT NULL
            )
            """
        )
        ensure_column(conn, "products", "scrape_status", "TEXT NOT NULL DEFAULT 'pending'")
        ensure_column(conn, "products", "last_error", "TEXT")
        ensure_column(conn, "products", "color", "TEXT")
        ensure_column(conn, "products", "reference", "TEXT")
        ensure_column(conn, "products", "product_type", "TEXT")
        ensure_column(conn, "products", "measurements_json", "TEXT")
        ensure_column(conn, "products", "image_urls_json", "TEXT")
        ensure_column(conn, "price_stock_history", "color", "TEXT")
        ensure_column(conn, "price_stock_history", "reference", "TEXT")
        ensure_column(conn, "price_stock_history", "product_type", "TEXT")
        ensure_column(conn, "price_stock_history", "measurements_json", "TEXT")
        ensure_column(conn, "price_stock_history", "image_urls_json", "TEXT")
        conn.commit()


def ensure_column(conn: sqlite3.Connection, table: str, column: str, definition: str) -> None:
    columns = {row[1] for row in conn.execute(f"PRAGMA table_info({table})")}
    if column not in columns:
        conn.execute(f"ALTER TABLE {table} ADD COLUMN {column} {definition}")


def get_latest_snapshot(db_path: Path, url: str) -> dict[str, Any] | None:
    with sqlite3.connect(db_path) as conn:
        conn.row_factory = sqlite3.Row
        row = conn.execute(
            """
            SELECT h.price, h.currency, h.in_stock, h.checked_at
            FROM price_stock_history h
            JOIN products p ON p.id = h.product_id
            WHERE p.url = ?
            ORDER BY h.checked_at DESC, h.id DESC
            LIMIT 1
            """,
            (url,),
        ).fetchone()
        return dict(row) if row else None


def save_to_db(db_path: Path, snapshot: ProductSnapshot) -> None:
    now = datetime.now(timezone.utc).isoformat()
    sizes_json = json.dumps(snapshot.sizes, ensure_ascii=False)
    measurements_json = json.dumps(snapshot.measurements or {}, ensure_ascii=False)
    image_urls_json = json.dumps(snapshot.image_urls or [], ensure_ascii=False)

    with sqlite3.connect(db_path) as conn:
        conn.execute(
            """
            INSERT INTO products (
                url, name, current_price, currency, in_stock, sizes_json,
                last_checked_at, created_at, scrape_status, last_error,
                color, reference, product_type, measurements_json, image_urls_json
            )
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(url) DO UPDATE SET
                name = excluded.name,
                current_price = excluded.current_price,
                currency = excluded.currency,
                in_stock = excluded.in_stock,
                sizes_json = excluded.sizes_json,
                last_checked_at = excluded.last_checked_at,
                scrape_status = excluded.scrape_status,
                last_error = excluded.last_error,
                color = excluded.color,
                reference = excluded.reference,
                product_type = excluded.product_type,
                measurements_json = excluded.measurements_json,
                image_urls_json = excluded.image_urls_json
            """,
            (
                snapshot.url,
                snapshot.name,
                snapshot.price,
                snapshot.currency,
                int(snapshot.in_stock),
                sizes_json,
                now,
                now,
                snapshot.status,
                snapshot.error,
                snapshot.color,
                snapshot.reference,
                snapshot.product_type,
                measurements_json,
                image_urls_json,
            ),
        )
        product_id = conn.execute(
            "SELECT id FROM products WHERE url = ?",
            (snapshot.url,),
        ).fetchone()[0]
        conn.execute(
            """
            INSERT INTO price_stock_history (
                product_id, checked_at, price, currency, in_stock,
                sizes_json, source, raw_summary, color, reference,
                product_type, measurements_json, image_urls_json
            )
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                product_id,
                now,
                snapshot.price,
                snapshot.currency,
                int(snapshot.in_stock),
                sizes_json,
                snapshot.source,
                snapshot.raw_summary,
                snapshot.color,
                snapshot.reference,
                snapshot.product_type,
                measurements_json,
                image_urls_json,
            ),
        )
        conn.commit()


def parse_price(value: Any) -> tuple[float | None, str]:
    if value is None:
        return None, "EUR"
    if isinstance(value, (int, float)):
        amount = float(value)
        if amount > 1000:
            amount = amount / 100
        return amount, "EUR"

    text = str(value)
    currency = "EUR" if "€" in text or "EUR" in text.upper() else "EUR"
    match = re.search(r"(\d+(?:[.,]\d{1,2})?)", text.replace(".", "").replace(",", "."))
    if not match:
        return None, currency
    amount = float(match.group(1))
    if amount > 1000 and re.fullmatch(r"\d+", text.strip()):
        amount = amount / 100
    return amount, currency


def is_amazon_url(url: str) -> bool:
    host = urlparse(url).netloc.lower()
    return any(domain in host for domain in AMAZON_DOMAINS)


def is_stradivarius_url(url: str) -> bool:
    return "stradivarius." in urlparse(url).netloc.lower()


def asin_from_url(url: str) -> str | None:
    patterns = [
        r"/dp/([A-Z0-9]{10})",
        r"/gp/product/([A-Z0-9]{10})",
        r"/product/([A-Z0-9]{10})",
        r"[?&]asin=([A-Z0-9]{10})",
    ]
    for pattern in patterns:
        match = re.search(pattern, url, flags=re.IGNORECASE)
        if match:
            return match.group(1).upper()
    return None


def flatten_json(value: Any) -> list[dict[str, Any]]:
    found: list[dict[str, Any]] = []
    if isinstance(value, dict):
        found.append(value)
        for child in value.values():
            found.extend(flatten_json(child))
    elif isinstance(value, list):
        for item in value:
            found.extend(flatten_json(item))
    return found


def find_product_in_json(payload: Any, url: str) -> ProductSnapshot | None:
    """Intenta extraer producto/precio/tallas desde cualquier JSON interceptado."""
    detail_snapshot = extract_product_detail_snapshot(payload, url)
    if detail_snapshot:
        return detail_snapshot

    candidates = flatten_json(payload)
    scored: list[tuple[int, dict[str, Any]]] = []

    for item in candidates:
        keys = {str(key).lower() for key in item.keys()}
        score = 0
        if {"name", "price"} & keys:
            score += 2
        if {"sizes", "sku", "availability", "stock", "stocks"} & keys:
            score += 2
        if {"detail", "product", "bundleproductsummary"} & keys:
            score += 1
        if score >= 3:
            scored.append((score, item))

    for _, item in sorted(scored, key=lambda pair: pair[0], reverse=True):
        name = first_non_empty(
            item.get("name"),
            item.get("productName"),
            item.get("displayName"),
            item.get("title"),
        )
        price_value = first_non_empty(
            item.get("price"),
            item.get("currentPrice"),
            item.get("salePrice"),
            item.get("amount"),
            item.get("unitPrice"),
        )

        if not name and "product" in item and isinstance(item["product"], dict):
            product = item["product"]
            name = first_non_empty(product.get("name"), product.get("productName"))
            price_value = first_non_empty(price_value, product.get("price"), product.get("currentPrice"))

        price, currency = parse_price(price_value)
        sizes = normalize_sizes(extract_sizes_from_json(item), item)
        in_stock = infer_stock_from_json(item, sizes)

        if name or price is not None or sizes:
            return ProductSnapshot(
                url=url,
                name=normalize_product_name(str(name or ""), url),
                price=price,
                currency=currency,
                in_stock=in_stock,
                sizes=sizes,
                source="json_interceptado",
                raw_summary=json.dumps(item, ensure_ascii=False)[:1500],
                status="ok",
            )

    return None


def selected_color_id_from_url(url: str) -> str:
    query = parse_qs(urlparse(url).query)
    return first_non_empty(*(query.get("colorId") or [])) or ""


def same_color_id(left: Any, right: Any) -> bool:
    if left in (None, "") or right in (None, ""):
        return False
    left_text = str(left).strip()
    right_text = str(right).strip()
    return left_text == right_text or left_text.lstrip("0") == right_text.lstrip("0")


def availability_from_visibility(value: Any) -> bool:
    visibility = str(value or "").upper()
    return visibility in {"SHOW", "RUNNING_OUT", "BACK_SOON"}


def extract_product_detail_snapshot(payload: Any, url: str) -> ProductSnapshot | None:
    if not isinstance(payload, dict) or "detail" not in payload:
        return None

    selected_color_id = selected_color_id_from_url(url) or str(payload.get("mainColorid") or "")
    products = payload.get("bundleProductSummaries") or [payload]
    product = select_product_for_color(products, selected_color_id) or payload
    color = select_color(product, selected_color_id) or select_color(payload, selected_color_id)
    if not isinstance(color, dict):
        return None

    sizes, measurements = sizes_from_color(color)
    price, currency = price_from_sizes(color.get("sizes") or [])
    product_visibility = first_non_empty(color.get("visibilityValue"), product.get("visibilityValue"), payload.get("visibilityValue"))
    in_stock = any(sizes.values()) if sizes else availability_from_visibility(product_visibility)
    detail = product.get("detail") if isinstance(product.get("detail"), dict) else payload.get("detail", {})
    name = normalize_product_name(
        first_non_empty(product.get("name"), color.get("shortDescription"), payload.get("name")),
        url,
    )
    reference = first_non_empty(color.get("reference"), detail.get("reference"), payload.get("detail", {}).get("reference"), "")
    product_type = first_non_empty(product.get("productType"), payload.get("productType"), "")
    summary = {
        "name": name,
        "color": color.get("name"),
        "reference": reference,
        "visibility": product_visibility,
        "sizes": sizes,
        "measurements": measurements,
    }

    return ProductSnapshot(
        url=url,
        name=name,
        price=price,
        currency=currency,
        in_stock=in_stock,
        sizes=sizes,
        source="json_detalle_producto",
        raw_summary=json.dumps(summary, ensure_ascii=False)[:1500],
        color=str(color.get("name") or ""),
        reference=str(reference or ""),
        product_type=str(product_type or ""),
        measurements=measurements,
        status="ok",
    )


def select_product_for_color(products: Any, color_id: str) -> dict[str, Any] | None:
    if not isinstance(products, list):
        return None
    for product in products:
        if not isinstance(product, dict):
            continue
        if select_color(product, color_id):
            return product
    return products[0] if products and isinstance(products[0], dict) else None


def select_color(product: dict[str, Any], color_id: str) -> dict[str, Any] | None:
    detail = product.get("detail")
    colors = detail.get("colors") if isinstance(detail, dict) else None
    if not isinstance(colors, list) or not colors:
        return None
    for color in colors:
        if isinstance(color, dict) and same_color_id(color.get("id"), color_id):
            return color
    return colors[0] if isinstance(colors[0], dict) else None


def sizes_from_color(color: dict[str, Any]) -> tuple[dict[str, bool], dict[str, str]]:
    sizes: dict[str, bool] = {}
    measurements: dict[str, str] = {}
    raw_sizes = color.get("sizes") or []
    if not isinstance(raw_sizes, list):
        return sizes, measurements

    for size in sorted((item for item in raw_sizes if isinstance(item, dict)), key=lambda item: item.get("position") or 0):
        label = clean_text(str(size.get("name") or size.get("description") or "Talla unica"))
        if not label:
            continue
        available = availability_from_visibility(size.get("visibilityValue"))
        sizes[label] = sizes.get(label, False) or available
        if not measurements:
            measurements = measurements_from_size(size)

    return sizes, measurements


def measurements_from_size(size: dict[str, Any]) -> dict[str, str]:
    dimensions = size.get("skuDimensions")
    if not isinstance(dimensions, list):
        return {}
    values: dict[str, str] = {}
    for item in dimensions:
        if not isinstance(item, dict):
            continue
        name = clean_text(str(item.get("dimensionName") or "")).capitalize()
        value = item.get("value")
        if name and value not in (None, ""):
            values[name] = f"{value:g} cm" if isinstance(value, (int, float)) else str(value)
    return values


def price_from_sizes(raw_sizes: Any) -> tuple[float | None, str]:
    if not isinstance(raw_sizes, list):
        return None, "EUR"
    for size in raw_sizes:
        if isinstance(size, dict):
            price, currency = parse_price(size.get("price"))
            if price is not None:
                return price, currency
    return None, "EUR"


def product_code_from_url(url: str) -> str | None:
    match = re.search(r"l\d{8}", url.lower())
    return match.group(0) if match else None


def product_name_from_url(url: str) -> str:
    path = urlparse(url).path
    slug = path.rsplit("/", 1)[-1].split("-l", 1)[0]
    if not slug:
        return "Producto Stradivarius"
    return " ".join(part.capitalize() for part in slug.split("-") if part)


def is_weak_product_name(value: str | None) -> bool:
    text = clean_text(value).lower()
    if not text:
        return True
    if text in COLOR_NAMES:
        return True
    if text in {"stradivarius", "producto stradivarius"}:
        return True
    return len(text) <= 2


def normalize_product_name(value: str | None, url: str) -> str:
    cleaned = clean_text(value)
    return product_name_from_url(url) if is_weak_product_name(cleaned) else cleaned


def default_sizes_from_url(url: str) -> dict[str, bool]:
    path = urlparse(url).path.lower()
    if any(word in path for word in ("bolso", "bag", "collar", "pendientes", "cinturon", "perfume")):
        return {"Talla unica": True}
    return {}


def is_access_denied_text(text: str) -> bool:
    lowered = text.lower()
    return "access denied" in lowered or "bm-verify" in lowered or "errors.edgesuite.net" in lowered


def sitemap_product_url(product_code: str) -> str | None:
    sitemap_url = "https://www.stradivarius.com/5/info/sitemaps/sitemap-products-st-es-0.xml.gz"
    try:
        response = requests.get(
            sitemap_url,
            headers={"User-Agent": random.choice(USER_AGENTS), "Accept": "application/xml,text/xml,*/*"},
            timeout=20,
        )
        response.raise_for_status()
        text = gzip.decompress(response.content).decode("utf-8", "ignore")
        match = re.search(r"<loc>([^<]*" + re.escape(product_code) + r"[^<]*)</loc>", text)
        return match.group(1) if match else None
    except Exception as exc:
        logging.info("No se pudo consultar sitemap de Stradivarius: %s", exc)
        return None


def blocked_snapshot(url: str, body_text: str = "") -> ProductSnapshot:
    code = product_code_from_url(url)
    sitemap_url = sitemap_product_url(code) if code else None
    canonical_url = sitemap_url or url
    sizes = default_sizes_from_url(url)
    return ProductSnapshot(
        url=url,
        name=product_name_from_url(canonical_url),
        price=None,
        currency="EUR",
        in_stock=False,
        sizes=sizes,
        source="bloqueo_stradivarius",
        raw_summary=body_text[:1500],
        status="blocked",
        error=(
            "Stradivarius/Akamai ha bloqueado la consulta automatizada. "
            "No se pueden leer precio, stock ni tallas hasta que la web permita cargar datos reales."
        ),
    )


def first_non_empty(*values: Any) -> Any:
    for value in values:
        if value not in (None, "", [], {}):
            return value
    return None


def extract_sizes_from_json(item: dict[str, Any]) -> dict[str, bool]:
    sizes: dict[str, bool] = {}
    possible_lists = [
        item.get("sizes"),
        item.get("sizeList"),
        item.get("attributes"),
        item.get("skus"),
        item.get("stocks"),
    ]

    for possible in possible_lists:
        if not isinstance(possible, list):
            continue
        for size_item in possible:
            if not isinstance(size_item, dict):
                continue
            label = first_non_empty(
                size_item.get("name"),
                size_item.get("size"),
                size_item.get("sizeName"),
                size_item.get("description"),
                size_item.get("label"),
                size_item.get("displayName"),
                size_item.get("value"),
            )
            if not label:
                continue
            sizes[clean_text(str(label))] = infer_stock_from_json(size_item, {})

    return sizes


def normalize_sizes(sizes: dict[str, bool], source: dict[str, Any] | None = None) -> dict[str, bool]:
    normalized: dict[str, bool] = {}
    for raw_label, available in sizes.items():
        label = clean_text(str(raw_label)).upper()
        if not label or len(label) > 24:
            continue
        normalized[label] = bool(available)

    return normalized


def infer_stock_from_json(item: dict[str, Any], sizes: dict[str, bool]) -> bool:
    if sizes:
        return any(sizes.values())

    for key in (
        "inStock", "available", "availability", "isAvailable", "isBuyable",
        "buyable", "sellable", "stock", "quantity", "stockQuantity", "units",
    ):
        if key not in item:
            continue
        value = item[key]
        if isinstance(value, bool):
            return value
        if isinstance(value, (int, float)):
            return value > 0
        text = str(value).lower()
        if any(word in text for word in ("in_stock", "available", "disponible", "true", "buyable", "sellable")):
            return True
        if any(word in text for word in ("out_of_stock", "agotado", "false", "sold")):
            return False

    return False


async def collect_json_responses(page: Page, bucket: list[Any]) -> None:
    async def on_response(response: Response) -> None:
        content_type = response.headers.get("content-type", "")
        if "json" not in content_type.lower():
            return
        try:
            url = response.url.lower()
            if any(term in url for term in ("product", "catalog", "stock", "price", "availability")):
                bucket.append(await response.json())
        except Exception as exc:
            logging.debug("No se pudo leer JSON interceptado: %s", exc)

    page.on("response", on_response)


async def safe_text(locator: Any) -> str | None:
    try:
        return await locator.text_content(timeout=1500)
    except Exception:
        return None


async def extract_from_dom(page: Page, url: str) -> ProductSnapshot:
    title = await page.title()
    h1_text = await safe_text(page.locator("h1").first)
    name = normalize_product_name(h1_text or title, url)

    body_text = await page.locator("body").inner_text(timeout=10000)
    if is_access_denied_text(body_text) or is_access_denied_text(title):
        return blocked_snapshot(url, body_text)

    price, currency = parse_price(body_text)
    sizes = await extract_sizes_from_dom(page)
    if not sizes:
        sizes = default_sizes_from_url(url)

    lowered = body_text.lower()
    has_sold_out_text = any(word in lowered for word in SOLD_OUT_WORDS)
    in_stock = any(sizes.values()) if sizes else not has_sold_out_text

    return ProductSnapshot(
        url=url,
        name=name,
        price=price,
        currency=currency,
        in_stock=in_stock,
        sizes=sizes,
        source="dom_renderizado",
        raw_summary=body_text[:1500],
        image_urls=await collect_product_images(page),
        status="ok",
    )


async def extract_sizes_from_dom(page: Page) -> dict[str, bool]:
    sizes: dict[str, bool] = {}
    selectors = [
        "button:has-text('XXS'), button:has-text('XS'), button:has-text('S'), button:has-text('M'), button:has-text('L'), button:has-text('XL'), button:has-text('XXL')",
        "[data-testid*='size'] button",
        "[class*='size'] button",
        "[aria-label*='talla' i]",
    ]

    for selector in selectors:
        try:
            buttons = page.locator(selector)
            count = min(await buttons.count(), 40)
            for index in range(count):
                button = buttons.nth(index)
                label = clean_text(await button.inner_text(timeout=1000))
                if not label:
                    label = clean_text(await button.get_attribute("aria-label") or "")
                label = label.upper().replace("TALLA", "").strip(" :")
                if label not in CLOTHING_SIZES and len(label) > 20:
                    continue
                disabled = await button.is_disabled()
                aria_disabled = await button.get_attribute("aria-disabled")
                class_name = (await button.get_attribute("class") or "").lower()
                data_disabled = (await button.get_attribute("data-disabled") or "").lower()
                unavailable = (
                    disabled
                    or aria_disabled == "true"
                    or data_disabled == "true"
                    or any(word in class_name for word in ("disabled", "unavailable", "sold", "out-of-stock"))
                )
                sizes[label] = not unavailable
        except Exception:
            continue

    return normalize_sizes(sizes)


async def collect_product_images(page: Page, limit: int = 3) -> list[str]:
    try:
        urls = await page.locator("img").evaluate_all(
            """
            images => images
              .map(img => img.currentSrc || img.src || img.getAttribute('data-src') || '')
              .filter(Boolean)
            """
        )
    except Exception:
        return []

    product_images: list[str] = []
    for raw_url in urls:
        url = str(raw_url)
        lowered = url.lower()
        if not url.startswith("http"):
            continue
        if not any(term in lowered for term in ("stradivarius", "static", "/photos", "/p/")):
            continue
        if url not in product_images:
            product_images.append(url)
        if len(product_images) >= limit:
            break
    return product_images


async def collect_amazon_images(page: Page, limit: int = 3) -> list[str]:
    try:
        urls = await page.evaluate(
            """
            () => {
              const found = [];
              const push = value => {
                if (value && typeof value === 'string' && value.startsWith('http') && !found.includes(value)) {
                  found.push(value);
                }
              };
              const landing = document.querySelector('#landingImage');
              if (landing) {
                push(landing.getAttribute('data-old-hires'));
                push(landing.currentSrc || landing.src);
                const dynamic = landing.getAttribute('data-a-dynamic-image');
                if (dynamic) {
                  try { Object.keys(JSON.parse(dynamic)).forEach(push); } catch (_) {}
                }
              }
              document.querySelectorAll('#altImages img, img.a-dynamic-image').forEach(img => {
                push(img.getAttribute('data-old-hires'));
                push(img.currentSrc || img.src);
              });
              return found;
            }
            """
        )
    except Exception:
        return []
    return [str(url) for url in urls[:limit]]


async def first_text(page: Page, selectors: list[str], timeout: int = 1500) -> str:
    for selector in selectors:
        try:
            text = await page.locator(selector).first.text_content(timeout=timeout)
            cleaned = clean_text(text)
            if cleaned:
                return cleaned
        except Exception:
            continue
    return ""


async def check_amazon_product(browser: Browser, url: str) -> ProductSnapshot:
    user_agent = random.choice(USER_AGENTS)
    context = await browser.new_context(
        user_agent=user_agent,
        locale="es-ES",
        timezone_id="Europe/Madrid",
        viewport={"width": random.randint(1280, 1440), "height": random.randint(800, 960)},
    )
    page = await context.new_page()
    try:
        await asyncio.sleep(random.uniform(1.0, 3.0))
        response = await page.goto(url, wait_until="domcontentloaded", timeout=60000)
        await page.wait_for_timeout(3500)
        title = await page.title()
        body_text = await page.locator("body").inner_text(timeout=10000)
        lowered = body_text.lower()
        if response and response.status in (403, 429, 503):
            return blocked_amazon_snapshot(url, f"HTTP {response.status}: {body_text[:800]}")
        if any(term in lowered for term in ("introduce los caracteres", "captcha", "robot check", "not a robot")):
            return blocked_amazon_snapshot(url, body_text)

        name = await first_text(page, ["#productTitle", "#title", "h1"])
        if not name:
            name = clean_text(title.replace("Amazon.es", "").replace(": ", " "))
        price_text = await first_text(
            page,
            [
                "#corePrice_feature_div .a-offscreen",
                "#apex_desktop .a-price .a-offscreen",
                "#priceblock_ourprice",
                "#priceblock_dealprice",
                ".a-price .a-offscreen",
            ],
        )
        price, currency = parse_price(price_text)
        availability = await first_text(page, ["#availability", "#outOfStock", "#desktop_buybox", "#buybox"])
        availability_lower = availability.lower()
        sold_out = any(term in availability_lower for term in (
            "actualmente no disponible", "no disponible", "agotado", "sin stock", "unavailable",
        ))
        in_stock = False
        if not sold_out:
            in_stock = any(term in availability_lower for term in (
                "en stock", "disponible", "añadir al carrito", "add to cart", "entrega",
            ))
            if not in_stock:
                try:
                    in_stock = await page.locator("#add-to-cart-button").count() > 0
                except Exception:
                    in_stock = False

        asin = asin_from_url(url) or ""
        images = await collect_amazon_images(page)
        summary = {
            "name": name,
            "price_text": price_text,
            "availability": availability,
            "asin": asin,
            "images": images,
        }
        return ProductSnapshot(
            url=url,
            name=name or "Producto Amazon",
            price=price,
            currency=currency,
            in_stock=in_stock,
            sizes={},
            source="amazon_dom",
            raw_summary=json.dumps(summary, ensure_ascii=False)[:1500],
            reference=asin,
            product_type="Amazon",
            image_urls=images,
            status="ok",
        )
    finally:
        await context.close()


def blocked_amazon_snapshot(url: str, body_text: str = "") -> ProductSnapshot:
    asin = asin_from_url(url) or ""
    return ProductSnapshot(
        url=url,
        name=f"Producto Amazon {asin}".strip(),
        price=None,
        currency="EUR",
        in_stock=False,
        sizes={},
        source="bloqueo_amazon",
        raw_summary=body_text[:1500],
        reference=asin,
        product_type="Amazon",
        status="blocked",
        error=(
            "Amazon ha bloqueado o limitado la lectura automatizada. "
            "No se pudieron leer precio y disponibilidad con fiabilidad."
        ),
    )


def clean_text(value: str | None) -> str:
    return re.sub(r"\s+", " ", value or "").strip()


async def check_product(browser: Browser, url: str) -> ProductSnapshot:
    if is_amazon_url(url):
        return await check_amazon_product(browser, url)

    user_agent = random.choice(USER_AGENTS)
    context = await browser.new_context(
        user_agent=user_agent,
        locale="es-ES",
        timezone_id="Europe/Madrid",
        viewport={"width": random.randint(1280, 1440), "height": random.randint(800, 960)},
    )
    page = await context.new_page()
    intercepted_json: list[Any] = []
    await collect_json_responses(page, intercepted_json)

    try:
        await asyncio.sleep(random.uniform(2.0, 6.0))
        response = await page.goto(url, wait_until="domcontentloaded", timeout=60000)
        if response and response.status in (403, 429):
            return blocked_snapshot(url, f"HTTP {response.status}")
        try:
            await page.wait_for_load_state("networkidle", timeout=30000)
        except Exception:
            logging.info("La pagina no alcanzo networkidle, se intentara extraer lo disponible.")
        await asyncio.sleep(random.uniform(1.5, 4.0))

        for payload in intercepted_json:
            snapshot = find_product_in_json(payload, url)
            if snapshot:
                snapshot.image_urls = await collect_product_images(page)
                return snapshot

        return await extract_from_dom(page, url)
    finally:
        await context.close()


def build_alerts(previous: dict[str, Any] | None, current: ProductSnapshot) -> list[str]:
    if not previous:
        return []

    alerts: list[str] = []
    previous_price = previous.get("price")
    previous_stock = bool(previous.get("in_stock"))

    if previous_price is not None and current.price is not None and current.price < float(previous_price):
        alerts.append(
            f"Bajada de precio: {previous_price:.2f} -> {current.price:.2f} {current.currency}"
        )

    if not previous_stock and current.in_stock:
        alerts.append("Reposicion de stock: antes agotado, ahora disponible")

    return alerts


def format_alert_message(snapshot: ProductSnapshot, reasons: list[str]) -> str:
    sizes_text = "No detectadas"
    if snapshot.sizes:
        sizes_text = ", ".join(
            f"{size}: {'SI' if available else 'NO'}"
            for size, available in snapshot.sizes.items()
        )

    store = "Amazon" if is_amazon_url(snapshot.url) else "Stradivarius"
    return (
        f"Alerta {store}\n"
        f"Producto: {snapshot.name}\n"
        f"Motivo: {' | '.join(reasons)}\n"
        f"Precio actual: {snapshot.price if snapshot.price is not None else 'No detectado'} {snapshot.currency}\n"
        f"Stock general: {'Disponible' if snapshot.in_stock else 'Agotado'}\n"
        f"Tallas: {sizes_text}\n"
        f"URL: {snapshot.url}"
    )


def send_telegram_alert(settings: Settings, message: str) -> None:
    # Coloca TELEGRAM_BOT_TOKEN y TELEGRAM_CHAT_ID en .env.
    # Nunca subas ese archivo a repositorios publicos.
    if not settings.telegram_bot_token or not settings.telegram_chat_id:
        logging.info("Telegram no configurado; alerta omitida.")
        return

    endpoint = f"https://api.telegram.org/bot{settings.telegram_bot_token}/sendMessage"
    response = requests.post(
        endpoint,
        json={"chat_id": settings.telegram_chat_id, "text": message},
        timeout=20,
    )
    response.raise_for_status()


def send_email_alert(settings: Settings, subject: str, message: str) -> None:
    # Para Gmail usa SMTP_HOST=smtp.gmail.com, SMTP_PORT=587 y una contrasena de aplicacion.
    # Coloca usuario, contrasena, remitente y destinatario en .env.
    if not settings.email_enabled:
        return
    from_address = settings.email_from or settings.smtp_username
    required = [
        settings.smtp_host,
        settings.smtp_username,
        settings.smtp_password,
        from_address,
        settings.email_to,
    ]
    if not all(required):
        raise RuntimeError("Email activado, pero faltan credenciales SMTP.")

    email = MIMEMultipart()
    email["From"] = from_address
    email["To"] = settings.email_to
    email["Subject"] = subject
    email.attach(MIMEText(message, "plain", "utf-8"))

    if settings.smtp_port == 465:
        server_context = smtplib.SMTP_SSL(settings.smtp_host, settings.smtp_port, timeout=30)
    else:
        server_context = smtplib.SMTP(settings.smtp_host, settings.smtp_port, timeout=30)

    with server_context as server:
        if settings.smtp_port != 465:
            server.starttls()
        server.login(settings.smtp_username, settings.smtp_password)
        server.send_message(email)


async def run_cycle(settings: Settings, urls: list[str]) -> None:
    init_db(settings.database_path)
    async with Stealth().use_async(async_playwright()) as playwright:
        browser = await playwright.chromium.launch(
            headless=True,
            args=[
                "--disable-blink-features=AutomationControlled",
                "--disable-dev-shm-usage",
                "--no-sandbox",
            ],
        )
        try:
            for url in urls:
                try:
                    logging.info("Revisando producto: %s", url)
                    previous = get_latest_snapshot(settings.database_path, url)
                    snapshot = await check_product(browser, url)
                    alerts = build_alerts(previous, snapshot)
                    save_to_db(settings.database_path, snapshot)

                    logging.info(
                        "Resultado: %s | precio=%s %s | stock=%s | fuente=%s",
                        snapshot.name,
                        snapshot.price,
                        snapshot.currency,
                        "SI" if snapshot.in_stock else "NO",
                        snapshot.source,
                    )

                    if alerts:
                        message = format_alert_message(snapshot, alerts)
                        send_telegram_alert(settings, message)
                        send_email_alert(settings, "Alerta Stradivarius", message)

                    await asyncio.sleep(random.uniform(4.0, 10.0))
                except Exception:
                    logging.exception("Fallo al revisar producto: %s", url)
        finally:
            await browser.close()


def scheduled_job(settings: Settings, urls: list[str]) -> None:
    try:
        asyncio.run(run_cycle(settings, urls))
    except Exception:
        logging.exception("Fallo general del ciclo programado.")


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Monitor de precios y stock de Stradivarius")
    parser.add_argument("--url", action="append", help="URL de producto. Puede repetirse varias veces.")
    parser.add_argument("--once", action="store_true", help="Ejecuta una sola revision y termina.")
    return parser.parse_args()


def main() -> None:
    setup_logging()
    args = parse_args()
    settings = get_settings()
    urls = args.url or settings.product_urls

    if not urls:
        raise SystemExit("Configura PRODUCT_URLS en .env o pasa una URL con --url.")

    if args.once:
        scheduled_job(settings, urls)
        return

    scheduler = BlockingScheduler(timezone="Europe/Madrid")
    scheduler.add_job(
        scheduled_job,
        "interval",
        minutes=settings.check_interval_minutes,
        args=[settings, urls],
        next_run_time=datetime.now(),
        max_instances=1,
        coalesce=True,
    )
    logging.info("Monitor iniciado. Intervalo: %s minutos.", settings.check_interval_minutes)
    scheduler.start()


if __name__ == "__main__":
    main()
