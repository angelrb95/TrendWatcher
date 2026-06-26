from __future__ import annotations

import asyncio
import json
import logging
import os
import secrets
import sqlite3
import threading
from datetime import datetime, timezone
from functools import wraps
from pathlib import Path
from typing import Any, Callable
from zoneinfo import ZoneInfo

from apscheduler.schedulers.background import BackgroundScheduler
from flask import Flask, flash, redirect, render_template, request, session, url_for
from playwright.async_api import async_playwright
from playwright_stealth import Stealth
from werkzeug.security import check_password_hash, generate_password_hash

import stradivarius_monitor as monitor


APP_DIR = Path(__file__).resolve().parent
monitor.load_dotenv(APP_DIR / ".env")
DATA_DIR = Path(os.getenv("DATA_DIR", APP_DIR / "data"))
DATA_DIR.mkdir(parents=True, exist_ok=True)
DB_PATH = Path(os.getenv("DATABASE_PATH", DATA_DIR / "stradivarius_monitor.db"))
LOG_PATH = Path(os.getenv("LOG_PATH", DATA_DIR / "app.log"))

DEFAULT_ADMIN_USER = os.getenv("ADMIN_USERNAME", "admin")
DEFAULT_ADMIN_PASSWORD = os.getenv("ADMIN_PASSWORD", "cambia-esta-contrasena")
SECRET_KEY = os.getenv("SECRET_KEY", secrets.token_hex(32))

logging.basicConfig(
    filename=LOG_PATH,
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    encoding="utf-8",
)

app = Flask(__name__)
app.secret_key = SECRET_KEY
scheduler = BackgroundScheduler(timezone="Europe/Madrid")
job_lock = threading.Lock()


def db() -> sqlite3.Connection:
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    return conn


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()


def parse_datetime(value: Any) -> datetime | None:
    if not value:
        return None
    try:
        text = str(value).replace("Z", "+00:00")
        parsed = datetime.fromisoformat(text)
        if parsed.tzinfo is None:
            parsed = parsed.replace(tzinfo=timezone.utc)
        return parsed.astimezone(ZoneInfo("Europe/Madrid"))
    except ValueError:
        return None


def row_get(row: Any, key: str, default: Any = None) -> Any:
    try:
        return row[key]
    except (KeyError, IndexError, TypeError):
        return getattr(row, key, default)


def column_exists(conn: sqlite3.Connection, table: str, column: str) -> bool:
    return any(row["name"] == column for row in conn.execute(f"PRAGMA table_info({table})"))


def init_app_db() -> None:
    monitor.init_db(DB_PATH)
    now = utc_now()
    with db() as conn:
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS app_config (
                key TEXT PRIMARY KEY,
                value TEXT NOT NULL
            )
            """
        )
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS app_users (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                username TEXT NOT NULL UNIQUE,
                password TEXT NOT NULL,
                role TEXT NOT NULL CHECK(role IN ('admin', 'user')),
                email TEXT,
                notifications_enabled INTEGER NOT NULL DEFAULT 1,
                created_at TEXT NOT NULL
            )
            """
        )
        if not column_exists(conn, "app_users", "email"):
            conn.execute("ALTER TABLE app_users ADD COLUMN email TEXT")
        if not column_exists(conn, "app_users", "notifications_enabled"):
            conn.execute("ALTER TABLE app_users ADD COLUMN notifications_enabled INTEGER NOT NULL DEFAULT 1")
        if not column_exists(conn, "products", "scrape_status"):
            conn.execute("ALTER TABLE products ADD COLUMN scrape_status TEXT NOT NULL DEFAULT 'pending'")
        if not column_exists(conn, "products", "last_error"):
            conn.execute("ALTER TABLE products ADD COLUMN last_error TEXT")

        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS user_products (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                user_id INTEGER NOT NULL,
                product_id INTEGER NOT NULL,
                notify_price_drop INTEGER NOT NULL DEFAULT 1,
                notify_restock INTEGER NOT NULL DEFAULT 1,
                created_at TEXT NOT NULL,
                UNIQUE(user_id, product_id),
                FOREIGN KEY(user_id) REFERENCES app_users(id),
                FOREIGN KEY(product_id) REFERENCES products(id)
            )
            """
        )
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS app_events (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                level TEXT NOT NULL,
                message TEXT NOT NULL,
                created_at TEXT NOT NULL
            )
            """
        )
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS password_reset_tokens (
                token TEXT PRIMARY KEY,
                user_id INTEGER NOT NULL,
                expires_at TEXT NOT NULL,
                used_at TEXT,
                created_at TEXT NOT NULL,
                FOREIGN KEY(user_id) REFERENCES app_users(id)
            )
            """
        )

        admin = conn.execute("SELECT id FROM app_users WHERE username = ?", (DEFAULT_ADMIN_USER,)).fetchone()
        admin_hash = generate_password_hash(DEFAULT_ADMIN_PASSWORD)
        if admin:
            conn.execute(
                "UPDATE app_users SET password = ?, role = 'admin' WHERE id = ?",
                (admin_hash, admin["id"]),
            )
            admin_id = admin["id"]
        else:
            cursor = conn.execute(
                "INSERT INTO app_users (username, password, role, email, created_at) VALUES (?, ?, 'admin', '', ?)",
                (DEFAULT_ADMIN_USER, admin_hash, now),
            )
            admin_id = cursor.lastrowid

        defaults = {
            "check_interval_minutes": os.getenv("CHECK_INTERVAL_MINUTES", "5"),
            "telegram_bot_token": os.getenv("TELEGRAM_BOT_TOKEN", ""),
            "telegram_chat_id": os.getenv("TELEGRAM_CHAT_ID", ""),
            "email_enabled": os.getenv("EMAIL_ENABLED", "true"),
            "smtp_host": os.getenv("SMTP_HOST", "smtp.gmail.com"),
            "smtp_port": os.getenv("SMTP_PORT", "587"),
            "smtp_username": os.getenv("SMTP_USERNAME", ""),
            "smtp_password": os.getenv("SMTP_PASSWORD", ""),
            "email_from": os.getenv("EMAIL_FROM", os.getenv("SMTP_USERNAME", "")),
            "email_to": os.getenv("EMAIL_TO", ""),
        }
        for key, value in defaults.items():
            current = conn.execute("SELECT value FROM app_config WHERE key = ?", (key,)).fetchone()
            if not current:
                conn.execute("INSERT INTO app_config (key, value) VALUES (?, ?)", (key, value))
            elif value and (not current["value"] or key == "email_enabled"):
                conn.execute("UPDATE app_config SET value = ? WHERE key = ?", (value, key))
            elif key == "check_interval_minutes" and current["value"] in {"", "30"}:
                conn.execute("UPDATE app_config SET value = '5' WHERE key = 'check_interval_minutes'")
        product_count = conn.execute("SELECT COUNT(*) AS total FROM products").fetchone()["total"]
        seed_marker = conn.execute("SELECT value FROM app_config WHERE key = 'product_urls_seeded'").fetchone()
        seed_env_products = seed_marker is None and product_count == 0
        if seed_marker is None and product_count > 0:
            conn.execute("INSERT INTO app_config (key, value) VALUES ('product_urls_seeded', 'true')")
        conn.commit()

    repair_product_urls()
    repair_weak_product_data()
    cleanup_orphan_products()

    if seed_env_products:
        for url in [item.strip() for item in os.getenv("PRODUCT_URLS", "").split(",") if item.strip()]:
            product_id = ensure_product(url)
            subscribe_user(admin_id, product_id)
        set_config({"product_urls_seeded": "true"})


def repair_product_urls() -> None:
    with db() as conn:
        rows = conn.execute("SELECT * FROM products").fetchall()
        for row in rows:
            normalized_url = monitor.normalize_product_url(row["url"])
            if not normalized_url or normalized_url == row["url"]:
                continue
            existing = conn.execute(
                "SELECT id FROM products WHERE url = ? AND id != ?",
                (normalized_url, row["id"]),
            ).fetchone()
            if existing:
                existing_id = int(existing["id"])
                conn.execute(
                    """
                    INSERT OR IGNORE INTO user_products (
                        user_id, product_id, notify_price_drop, notify_restock, created_at
                    )
                    SELECT user_id, ?, notify_price_drop, notify_restock, created_at
                    FROM user_products
                    WHERE product_id = ?
                    """,
                    (existing_id, row["id"]),
                )
                conn.execute(
                    "UPDATE price_stock_history SET product_id = ? WHERE product_id = ?",
                    (existing_id, row["id"]),
                )
                conn.execute("DELETE FROM user_products WHERE product_id = ?", (row["id"],))
                conn.execute("DELETE FROM products WHERE id = ?", (row["id"],))
            else:
                conn.execute(
                    "UPDATE products SET url = ? WHERE id = ?",
                    (normalized_url, row["id"]),
                )
        conn.commit()


def repair_weak_product_data() -> None:
    with db() as conn:
        rows = conn.execute("SELECT id, url, name FROM products").fetchall()
        for row in rows:
            if monitor.is_weak_product_name(row["name"]):
                conn.execute(
                    "UPDATE products SET name = ? WHERE id = ?",
                    (monitor.product_name_from_url(row["url"]), row["id"]),
                )
        conn.commit()


def cleanup_orphan_products() -> int:
    with db() as conn:
        rows = conn.execute(
            """
            SELECT p.id
            FROM products p
            LEFT JOIN user_products up ON up.product_id = p.id
            WHERE up.id IS NULL
            """
        ).fetchall()
        for row in rows:
            conn.execute("DELETE FROM price_stock_history WHERE product_id = ?", (row["id"],))
            conn.execute("DELETE FROM products WHERE id = ?", (row["id"],))
        conn.commit()
    return len(rows)


def add_event(level: str, message: str) -> None:
    logging.info("%s: %s", level.upper(), message)
    with db() as conn:
        conn.execute(
            "INSERT INTO app_events (level, message, created_at) VALUES (?, ?, ?)",
            (level, message, utc_now()),
        )
        conn.commit()


def get_config() -> dict[str, str]:
    with db() as conn:
        rows = conn.execute("SELECT key, value FROM app_config").fetchall()
    return {row["key"]: row["value"] for row in rows}


def set_config(values: dict[str, str]) -> None:
    with db() as conn:
        for key, value in values.items():
            conn.execute(
                "INSERT INTO app_config (key, value) VALUES (?, ?) ON CONFLICT(key) DO UPDATE SET value = excluded.value",
                (key, value),
            )
        conn.commit()


def build_settings() -> monitor.Settings:
    config = get_config()
    urls = list_tracked_product_urls()
    smtp_username = config.get("smtp_username", "")
    return monitor.Settings(
        product_urls=urls,
        check_interval_minutes=int(config.get("check_interval_minutes", "5") or "5"),
        database_path=DB_PATH,
        telegram_bot_token=config.get("telegram_bot_token", ""),
        telegram_chat_id=config.get("telegram_chat_id", ""),
        email_enabled=config.get("email_enabled", "false").lower() == "true",
        smtp_host=config.get("smtp_host", "smtp.gmail.com"),
        smtp_port=int(config.get("smtp_port", "587") or "587"),
        smtp_username=smtp_username,
        smtp_password=config.get("smtp_password", ""),
        email_from=config.get("email_from", "") or smtp_username,
        email_to=config.get("email_to", ""),
    )


def list_tracked_product_urls() -> list[str]:
    with db() as conn:
        rows = conn.execute(
            """
            SELECT DISTINCT p.url
            FROM products p
            JOIN user_products up ON up.product_id = p.id
            ORDER BY p.created_at DESC
            """
        ).fetchall()
    return [row["url"] for row in rows]


def is_url_tracked(url: str) -> bool:
    normalized_url = monitor.normalize_product_url(url)
    with db() as conn:
        row = conn.execute(
            """
            SELECT 1
            FROM products p
            JOIN user_products up ON up.product_id = p.id
            WHERE p.url = ?
            LIMIT 1
            """,
            (normalized_url,),
        ).fetchone()
    return row is not None


def list_all_products() -> list[sqlite3.Row]:
    with db() as conn:
        return conn.execute(
            """
            SELECT p.*,
                   (SELECT COUNT(*) FROM price_stock_history h WHERE h.product_id = p.id) AS checks,
                   (SELECT COUNT(*) FROM user_products up WHERE up.product_id = p.id) AS followers
            FROM products p
            ORDER BY p.created_at DESC
            """
        ).fetchall()


def list_user_products(user_id: int) -> list[sqlite3.Row]:
    with db() as conn:
        return conn.execute(
            """
            SELECT p.*, up.notify_price_drop, up.notify_restock,
                   (SELECT COUNT(*) FROM price_stock_history h WHERE h.product_id = p.id) AS checks
            FROM user_products up
            JOIN products p ON p.id = up.product_id
            WHERE up.user_id = ?
            ORDER BY up.created_at DESC
            """,
            (user_id,),
        ).fetchall()


def list_users() -> list[sqlite3.Row]:
    with db() as conn:
        return conn.execute(
            """
            SELECT u.id, u.username, u.role, u.email, u.notifications_enabled, u.created_at,
                   COUNT(up.id) AS products_count
            FROM app_users u
            LEFT JOIN user_products up ON up.user_id = u.id
            GROUP BY u.id
            ORDER BY u.created_at DESC
            """
        ).fetchall()


def get_user(user_id: int) -> sqlite3.Row | None:
    with db() as conn:
        return conn.execute("SELECT * FROM app_users WHERE id = ?", (user_id,)).fetchone()


def get_product(product_id: int, user_id: int | None = None, admin: bool = False) -> sqlite3.Row | None:
    with db() as conn:
        if admin:
            return conn.execute("SELECT * FROM products WHERE id = ?", (product_id,)).fetchone()
        return conn.execute(
            """
            SELECT p.* FROM products p
            JOIN user_products up ON up.product_id = p.id
            WHERE p.id = ? AND up.user_id = ?
            """,
            (product_id, user_id),
        ).fetchone()


def product_history(product_id: int, limit: int = 80) -> list[sqlite3.Row]:
    with db() as conn:
        return conn.execute(
            """
            SELECT * FROM price_stock_history
            WHERE product_id = ?
            ORDER BY checked_at DESC, id DESC
            LIMIT ?
            """,
            (product_id, limit),
        ).fetchall()


def recent_events(limit: int = 50) -> list[sqlite3.Row]:
    with db() as conn:
        return conn.execute(
            "SELECT * FROM app_events ORDER BY created_at DESC, id DESC LIMIT ?",
            (limit,),
        ).fetchall()


def ensure_product(url: str, name: str = "Producto vigilado") -> int:
    now = utc_now()
    clean_url = monitor.normalize_product_url(url)
    with db() as conn:
        conn.execute(
            """
            INSERT INTO products (
                url, name, current_price, currency, in_stock, sizes_json,
                last_checked_at, created_at, scrape_status, last_error
            )
            VALUES (?, ?, NULL, 'EUR', 0, '{}', NULL, ?, 'pending', NULL)
            ON CONFLICT(url) DO NOTHING
            """,
            (clean_url, name, now),
        )
        row = conn.execute("SELECT id FROM products WHERE url = ?", (clean_url,)).fetchone()
        conn.commit()
    return int(row["id"])


def subscribe_user(user_id: int, product_id: int) -> None:
    with db() as conn:
        conn.execute(
            """
            INSERT INTO user_products (user_id, product_id, created_at)
            VALUES (?, ?, ?)
            ON CONFLICT(user_id, product_id) DO NOTHING
            """,
            (user_id, product_id, utc_now()),
        )
        conn.commit()


def unsubscribe_user_product(user_id: int, product_id: int) -> None:
    with db() as conn:
        conn.execute("DELETE FROM user_products WHERE user_id = ? AND product_id = ?", (user_id, product_id))
        followers = conn.execute(
            "SELECT COUNT(*) AS total FROM user_products WHERE product_id = ?",
            (product_id,),
        ).fetchone()["total"]
        if followers == 0:
            conn.execute("DELETE FROM price_stock_history WHERE product_id = ?", (product_id,))
            conn.execute("DELETE FROM products WHERE id = ?", (product_id,))
        conn.commit()


def update_user_product_notifications(
    user_id: int,
    product_id: int,
    notify_price_drop: bool,
    notify_restock: bool,
) -> None:
    with db() as conn:
        conn.execute(
            """
            UPDATE user_products
            SET notify_price_drop = ?, notify_restock = ?
            WHERE user_id = ? AND product_id = ?
            """,
            (int(notify_price_drop), int(notify_restock), user_id, product_id),
        )
        conn.commit()


def delete_product(product_id: int) -> None:
    with db() as conn:
        conn.execute("DELETE FROM user_products WHERE product_id = ?", (product_id,))
        conn.execute("DELETE FROM price_stock_history WHERE product_id = ?", (product_id,))
        conn.execute("DELETE FROM products WHERE id = ?", (product_id,))
        conn.commit()


def add_product_for_user(url: str, user_id: int) -> None:
    product_id = ensure_product(url)
    subscribe_user(user_id, product_id)


def subscribed_users(product_id: int, reasons: list[str]) -> list[sqlite3.Row]:
    needs_price = any("precio" in reason.lower() for reason in reasons)
    needs_stock = any("stock" in reason.lower() or "reposicion" in reason.lower() for reason in reasons)
    with db() as conn:
        return conn.execute(
            """
            SELECT u.*
            FROM app_users u
            JOIN user_products up ON up.user_id = u.id
            WHERE up.product_id = ?
              AND u.notifications_enabled = 1
              AND COALESCE(u.email, '') != ''
              AND ((? = 1 AND up.notify_price_drop = 1) OR (? = 1 AND up.notify_restock = 1))
            """,
            (product_id, int(needs_price), int(needs_stock)),
        ).fetchall()


def login_required(role: str | None = None) -> Callable:
    def decorator(view: Callable) -> Callable:
        @wraps(view)
        def wrapped(*args: Any, **kwargs: Any) -> Any:
            if not session.get("user_id"):
                return redirect(url_for("login", next=request.path))
            if role and session.get("role") != role:
                flash("No tienes permisos para entrar en esa zona.", "error")
                return redirect(url_for("dashboard"))
            return view(*args, **kwargs)

        return wrapped

    return decorator


def run_monitor_cycle(product_urls: list[str] | None = None) -> None:
    if not job_lock.acquire(blocking=False):
        add_event("warning", "Ya hay una revisión en curso; se omite esta ejecución.")
        return
    try:
        settings = build_settings()
        urls = product_urls or settings.product_urls
        if not urls:
            add_event("warning", "No hay productos configurados.")
            return
        asyncio.run(check_urls(settings, urls))
        add_event("success", f"Revisión completada para {len(urls)} producto(s).")
    except Exception as exc:
        logging.exception("Fallo general al ejecutar revisión")
        add_event("error", f"Fallo general al ejecutar revisión: {exc}")
    finally:
        job_lock.release()


async def check_urls(settings: monitor.Settings, urls: list[str]) -> None:
    async with Stealth().use_async(async_playwright()) as playwright:
        browser = await playwright.chromium.launch(
            headless=True,
            args=["--disable-blink-features=AutomationControlled", "--disable-dev-shm-usage", "--no-sandbox"],
        )
        try:
            for url in urls:
                try:
                    if not is_url_tracked(url):
                        add_event("info", f"Producto omitido porque ya no tiene usuarios: {url}")
                        continue
                    previous = monitor.get_latest_snapshot(settings.database_path, url)
                    snapshot = await monitor.check_product(browser, url)
                    alerts = monitor.build_alerts(previous, snapshot)
                    monitor.save_to_db(settings.database_path, snapshot)
                    product_row = get_product_by_url(snapshot.url)
                    if snapshot.status == "blocked":
                        add_event("warning", f"{snapshot.name}: lectura bloqueada por la tienda. {snapshot.error}")
                    else:
                        add_event(
                            "info",
                            f"{snapshot.name}: precio={snapshot.price} {snapshot.currency}, stock={'Sí' if snapshot.in_stock else 'No'}",
                        )
                    if alerts and product_row:
                        message = monitor.format_alert_message(snapshot, alerts)
                        monitor.send_telegram_alert(settings, message)
                        notify_product_users(settings, int(product_row["id"]), snapshot, alerts, message)
                        add_event("success", f"Alerta enviada: {' | '.join(alerts)}")
                except Exception as exc:
                    logging.exception("Fallo al revisar %s", url)
                    add_event("error", f"Fallo al revisar {url}: {exc}")
        finally:
            await browser.close()


def get_product_by_url(url: str) -> sqlite3.Row | None:
    with db() as conn:
        return conn.execute("SELECT * FROM products WHERE url = ?", (url,)).fetchone()


def notify_product_users(
    settings: monitor.Settings,
    product_id: int,
    snapshot: monitor.ProductSnapshot,
    alerts: list[str],
    fallback_message: str,
) -> None:
    if not settings.email_enabled:
        return
    for user in subscribed_users(product_id, alerts):
        target = user["email"]
        try:
            send_email_to(settings, target, "Alerta TrendWatcher", fallback_message)
        except Exception as exc:
            add_event("error", f"No se pudo enviar email a {target}: {exc}")


def send_email_to(settings: monitor.Settings, recipient: str, subject: str, message: str) -> None:
    if not settings.email_enabled:
        raise RuntimeError("Email desactivado en la configuracion.")
    original_to = settings.email_to
    settings.email_to = recipient
    try:
        monitor.send_email_alert(settings, subject, message)
    finally:
        settings.email_to = original_to


def send_password_reset_email(user: sqlite3.Row, token: str) -> None:
    settings = build_settings()
    reset_url = url_for("reset_password", token=token, _external=True)
    message = (
        "Recuperación de contraseña\n\n"
        f"Hola {user['username']},\n\n"
        "Usa este enlace para crear una contraseña nueva. Caduca en 1 hora:\n"
        f"{reset_url}\n\n"
        "Si no lo has pedido, ignora este mensaje."
    )
    send_email_to(settings, user["email"], "Recuperar contraseña", message)


def create_password_reset_token(user_id: int) -> str:
    token = secrets.token_urlsafe(32)
    expires_at = datetime.now(timezone.utc).timestamp() + 3600
    with db() as conn:
        conn.execute("DELETE FROM password_reset_tokens WHERE user_id = ? OR expires_at < ?", (user_id, utc_now()))
        conn.execute(
            """
            INSERT INTO password_reset_tokens (token, user_id, expires_at, created_at)
            VALUES (?, ?, ?, ?)
            """,
            (token, user_id, datetime.fromtimestamp(expires_at, timezone.utc).isoformat(), utc_now()),
        )
        conn.commit()
    return token


def get_valid_reset_token(token: str) -> sqlite3.Row | None:
    with db() as conn:
        return conn.execute(
            """
            SELECT t.*, u.username, u.email
            FROM password_reset_tokens t
            JOIN app_users u ON u.id = t.user_id
            WHERE t.token = ? AND t.used_at IS NULL AND t.expires_at > ?
            """,
            (token, utc_now()),
        ).fetchone()


def reschedule() -> None:
    settings = build_settings()
    if scheduler.get_job("monitor"):
        scheduler.remove_job("monitor")
    scheduler.add_job(
        run_monitor_cycle,
        "interval",
        minutes=max(1, settings.check_interval_minutes),
        id="monitor",
        max_instances=1,
        coalesce=True,
    )


@app.template_filter("money")
def money(value: Any) -> str:
    if value is None:
        return "No detectado"
    return f"{float(value):.2f} EUR"


@app.template_filter("date_es")
def date_es(value: Any) -> str:
    parsed = parse_datetime(value)
    if not parsed:
        return "Pendiente"
    return parsed.strftime("%d/%m/%Y %H:%M")


@app.template_filter("stock")
def stock(value: Any) -> str:
    return "Disponible" if bool(value) else "Agotado"


@app.template_filter("availability_label")
def availability_label(product: Any) -> str:
    size_map_values = list(sizes_map(row_get(product, "sizes_json", "")).values())
    if row_get(product, "scrape_status") == "blocked":
        return "Bloqueado"
    if bool(row_get(product, "in_stock")) or any(size_map_values):
        return "Disponible"
    if size_map_values and not any(size_map_values):
        return "Agotado"
    if row_get(product, "current_price") is None:
        return "Por confirmar"
    return "Agotado"


@app.template_filter("availability_class")
def availability_class(product: Any) -> str:
    label = availability_label(product)
    if label == "Disponible":
        return "ok"
    if label == "Agotado":
        return "bad"
    return "warn"


@app.template_filter("scrape_status")
def scrape_status(value: Any) -> str:
    mapping = {
        "ok": "Datos actualizados",
        "blocked": "Bloqueado por la tienda",
        "pending": "Pendiente de revisar",
    }
    return mapping.get(str(value or "pending"), str(value))


@app.template_filter("sizes")
def sizes(value: str | None) -> str:
    if not value:
        return "No detectadas"
    try:
        parsed = json.loads(value)
    except json.JSONDecodeError:
        return "No detectadas"
    if not parsed:
        return "No detectadas"
    return ", ".join(f"{name}: {'Sí' if ok else 'No'}" for name, ok in parsed.items())


@app.template_filter("sizes_map")
def sizes_map(value: str | None) -> dict[str, bool]:
    if not value:
        return {}
    try:
        parsed = json.loads(value)
    except json.JSONDecodeError:
        return {}
    if not isinstance(parsed, dict):
        return {}
    return {str(name): bool(available) for name, available in parsed.items()}


@app.template_filter("measurements")
def measurements(value: str | None) -> str:
    if not value:
        return "No detectadas"
    try:
        parsed = json.loads(value)
    except json.JSONDecodeError:
        return "No detectadas"
    if not isinstance(parsed, dict) or not parsed:
        return "No detectadas"
    return ", ".join(f"{name}: {amount}" for name, amount in parsed.items())


@app.template_filter("image_urls")
def image_urls(value: str | None) -> list[str]:
    if not value:
        return []
    try:
        parsed = json.loads(value)
    except json.JSONDecodeError:
        return []
    if not isinstance(parsed, list):
        return []
    return [str(url) for url in parsed if str(url).startswith("http")]


@app.template_filter("product_code")
def product_code(value: str | None) -> str:
    if not value:
        return "No detectado"
    asin = monitor.asin_from_url(value)
    if asin:
        return asin
    match = monitor.product_code_from_url(value)
    return match.upper() if match else "No detectado"


@app.template_filter("store_name")
def store_name(value: str | None) -> str:
    if not value:
        return "Tienda"
    if monitor.is_amazon_url(value):
        return "Amazon"
    if monitor.is_stradivarius_url(value):
        return "Stradivarius"
    return "Tienda"


@app.route("/login", methods=["GET", "POST"])
def login() -> Any:
    if request.method == "POST":
        username = request.form.get("username", "").strip()
        password = request.form.get("password", "")
        with db() as conn:
            user = conn.execute("SELECT * FROM app_users WHERE username = ?", (username,)).fetchone()
        if user and check_password_hash(user["password"], password):
            session["user_id"] = user["id"]
            session["username"] = user["username"]
            session["role"] = user["role"]
            return redirect(request.args.get("next") or url_for("dashboard"))
        flash("Usuario o contraseña incorrectos.", "error")
    return render_template("login.html")


@app.route("/forgot-password", methods=["GET", "POST"])
def forgot_password() -> Any:
    if request.method == "POST":
        identifier = request.form.get("identifier", "").strip()
        with db() as conn:
            user = conn.execute(
                "SELECT * FROM app_users WHERE username = ? OR email = ?",
                (identifier, identifier),
            ).fetchone()
        if user and user["email"]:
            token = create_password_reset_token(int(user["id"]))
            try:
                send_password_reset_email(user, token)
                add_event("info", f"Recuperación de contraseña solicitada para {user['username']}.")
            except Exception as exc:
                add_event("error", f"No se pudo enviar recuperacion a {user['email']}: {exc}")
        flash("Si el usuario o email existe, te enviaremos un enlace para recuperar la contraseña.", "success")
        return redirect(url_for("login"))
    return render_template("forgot_password.html")


@app.route("/reset-password/<token>", methods=["GET", "POST"])
def reset_password(token: str) -> Any:
    reset = get_valid_reset_token(token)
    if not reset:
        flash("El enlace de recuperacion no es valido o ha caducado.", "error")
        return redirect(url_for("forgot_password"))
    if request.method == "POST":
        password = request.form.get("password", "")
        confirm = request.form.get("confirm", "")
        if len(password) < 8:
            flash("La contraseña debe tener al menos 8 caracteres.", "error")
        elif password != confirm:
            flash("Las contraseñas no coinciden.", "error")
        else:
            with db() as conn:
                conn.execute(
                    "UPDATE app_users SET password = ? WHERE id = ?",
                    (generate_password_hash(password), reset["user_id"]),
                )
                conn.execute(
                    "UPDATE password_reset_tokens SET used_at = ? WHERE token = ?",
                    (utc_now(), token),
                )
                conn.commit()
            flash("Contrasena actualizada. Ya puedes entrar.", "success")
            return redirect(url_for("login"))
    return render_template("reset_password.html", token=token)


@app.route("/register", methods=["GET", "POST"])
def register() -> Any:
    if request.method == "POST":
        username = request.form.get("username", "").strip()
        email = request.form.get("email", "").strip()
        password = request.form.get("password", "")
        confirm = request.form.get("confirm", "")
        if len(username) < 3:
            flash("El usuario debe tener al menos 3 caracteres.", "error")
        elif "@" not in email:
            flash("Introduce un email válido.", "error")
        elif len(password) < 8:
            flash("La contraseña debe tener al menos 8 caracteres.", "error")
        elif password != confirm:
            flash("Las contraseñas no coinciden.", "error")
        else:
            try:
                with db() as conn:
                    cursor = conn.execute(
                        """
                        INSERT INTO app_users (username, password, role, email, notifications_enabled, created_at)
                        VALUES (?, ?, 'user', ?, 1, ?)
                        """,
                        (username, generate_password_hash(password), email, utc_now()),
                    )
                    conn.commit()
                session["user_id"] = cursor.lastrowid
                session["username"] = username
                session["role"] = "user"
                flash("Cuenta creada. Ya puedes añadir tus productos.", "success")
                return redirect(url_for("dashboard"))
            except sqlite3.IntegrityError:
                flash("Ese usuario ya existe.", "error")
    return render_template("register.html")


@app.route("/logout")
def logout() -> Any:
    session.clear()
    return redirect(url_for("login"))


@app.route("/", methods=["GET", "POST"])
@login_required()
def dashboard() -> Any:
    user_id = int(session["user_id"])
    if request.method == "POST":
        action = request.form.get("action")
        if action == "add_product":
            url = request.form.get("url", "").strip()
            if url:
                add_product_for_user(url, user_id)
                threading.Thread(target=run_monitor_cycle, args=[[url]], daemon=True).start()
                flash("Producto añadido a tu lista. La primera revisión se lanzará automáticamente.", "success")
        elif action == "remove_product":
            unsubscribe_user_product(user_id, int(request.form["product_id"]))
            flash("Producto eliminado de tu lista.", "success")
        elif action == "update_notifications":
            product_id = int(request.form["product_id"])
            update_user_product_notifications(
                user_id,
                product_id,
                request.form.get("notify_price_drop") == "on",
                request.form.get("notify_restock") == "on",
            )
            flash("Alertas del producto actualizadas.", "success")
        return redirect(url_for("dashboard"))
    return render_template(
        "dashboard.html",
        products=list_user_products(user_id),
        user=get_user(user_id),
    )

@app.route("/profile", methods=["GET", "POST"])
@login_required()
def profile() -> Any:
    user_id = int(session["user_id"])
    if request.method == "POST":
        email = request.form.get("email", "").strip()
        notifications_enabled = 1 if request.form.get("notifications_enabled") == "on" else 0
        new_password = request.form.get("new_password", "")
        with db() as conn:
            conn.execute(
                "UPDATE app_users SET email = ?, notifications_enabled = ? WHERE id = ?",
                (email, notifications_enabled, user_id),
            )
            if new_password:
                if len(new_password) < 8:
                    flash("La nueva contraseña debe tener al menos 8 caracteres.", "error")
                    return redirect(url_for("profile"))
                conn.execute(
                    "UPDATE app_users SET password = ? WHERE id = ?",
                    (generate_password_hash(new_password), user_id),
                )
            conn.commit()
        flash("Perfil actualizado.", "success")
        return redirect(url_for("profile"))
    return render_template("profile.html", user=get_user(user_id))


@app.route("/product/<int:product_id>")
@login_required()
def product_detail(product_id: int) -> Any:
    is_admin = session.get("role") == "admin"
    product = get_product(product_id, int(session["user_id"]), admin=is_admin)
    if not product:
        flash("Producto no encontrado.", "error")
        return redirect(url_for("dashboard"))
    return render_template("product.html", product=product, history=product_history(product_id))


@app.route("/admin", methods=["GET", "POST"])
@login_required("admin")
def admin() -> Any:
    if request.method == "POST":
        action = request.form.get("action")
        if action == "add_product":
            url = request.form.get("url", "").strip()
            if url:
                product_id = ensure_product(url)
                subscribe_user(int(session["user_id"]), product_id)
                flash("Producto añadido al catálogo.", "success")
        elif action == "delete_product":
            delete_product(int(request.form["product_id"]))
            flash("Producto eliminado para todos los usuarios.", "success")
        elif action == "cleanup_orphans":
            removed = cleanup_orphan_products()
            flash(f"Productos sin usuarios eliminados: {removed}.", "success")
        elif action == "config":
            current_config = get_config()
            smtp_password = request.form.get("smtp_password", "")
            set_config(
                {
                    "check_interval_minutes": request.form.get("check_interval_minutes", "5"),
                    "telegram_bot_token": request.form.get("telegram_bot_token", ""),
                    "telegram_chat_id": request.form.get("telegram_chat_id", ""),
                    "email_enabled": "true" if request.form.get("email_enabled") == "on" else "false",
                    "smtp_host": request.form.get("smtp_host", "smtp.gmail.com"),
                    "smtp_port": request.form.get("smtp_port", "587"),
                    "smtp_username": request.form.get("smtp_username", ""),
                    "smtp_password": smtp_password or current_config.get("smtp_password", ""),
                    "email_from": request.form.get("email_from", "") or request.form.get("smtp_username", ""),
                    "email_to": request.form.get("email_to", ""),
                }
            )
            reschedule()
            flash("Configuración guardada.", "success")
        elif action == "test_email":
            settings = build_settings()
            target = request.form.get("test_email_to", "").strip() or settings.email_to or settings.smtp_username
            try:
                send_email_to(
                    settings,
                    target,
                    "Prueba de alertas TrendWatcher",
                    "El envío de email está funcionando correctamente.",
                )
                add_event("success", f"Email de prueba enviado a {target}.")
                flash("Email de prueba enviado.", "success")
            except Exception as exc:
                add_event("error", f"Fallo en email de prueba: {exc}")
                flash(f"No se pudo enviar el email de prueba: {exc}", "error")
        elif action == "run_now":
            threading.Thread(target=run_monitor_cycle, daemon=True).start()
            flash("Revisión global lanzada en segundo plano.", "success")
        return redirect(url_for("admin"))
    return render_template(
        "admin.html",
        products=list_all_products(),
        users=list_users(),
        config=get_config(),
        events=recent_events(),
    )


@app.route("/admin/users", methods=["POST"])
@login_required("admin")
def create_user() -> Any:
    username = request.form.get("username", "").strip()
    email = request.form.get("email", "").strip()
    password = request.form.get("password", "").strip()
    role = request.form.get("role", "user")
    if username and password and role in {"admin", "user"}:
        try:
            with db() as conn:
                conn.execute(
                    """
                    INSERT INTO app_users (username, password, role, email, notifications_enabled, created_at)
                    VALUES (?, ?, ?, ?, 1, ?)
                    """,
                    (username, generate_password_hash(password), role, email, utc_now()),
                )
                conn.commit()
            flash("Usuario creado.", "success")
        except sqlite3.IntegrityError:
            flash("Ese usuario ya existe.", "error")
    return redirect(url_for("admin"))


@app.route("/health")
def health() -> dict[str, str]:
    return {"status": "ok"}


def bootstrap() -> None:
    init_app_db()
    reschedule()
    scheduler.start()


bootstrap()


if __name__ == "__main__":
    app.run(host="0.0.0.0", port=int(os.getenv("PORT", "8080")))



