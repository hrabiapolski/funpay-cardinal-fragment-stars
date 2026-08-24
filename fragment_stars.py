"""FunPayCardinal plugin: automatic Telegram Stars delivery via Fragment.

The plugin intentionally keeps every purchase serial and records a durable state
before touching the wallet. A transaction with an uncertain outcome is never
retried automatically; this prevents double delivery after network failures.
"""

from __future__ import annotations

import asyncio
import html
import json
import logging
import os
import queue
import re
import sqlite3
import subprocess
import sys
import threading
import time
from pathlib import Path
from typing import Any

NAME = "Fragment Stars"
VERSION = "1.1.0"
DESCRIPTION = "Автоматическая выдача Telegram Stars покупателям FunPay через Fragment"
CREDITS = "Граф"
UUID = "be55a292-61dc-4fc0-a696-2088a5026335"
SETTINGS_PAGE = False


LOGGER = logging.getLogger("FPC.FragmentStars")
BASE_DIR = Path("storage/plugins/fragment_stars")
CONFIG_PATH = BASE_DIR / "config.json"
DB_PATH = BASE_DIR / "orders.sqlite3"

DEFAULT_CONFIG: dict[str, Any] = {
    "enabled": True,
    "paused": False,
    "fragment": {
        "seed": "",
        "api_key": "",
        "cookies": {
            "stel_ssid": "",
            "stel_dt": "",
            "stel_token": "",
            "stel_ton_token": "",
        },
        "wallet_version": "V5R1",
        "api_provider": "tonapi",
        "payment_method": "ton",
        "show_sender": False,
        "timeout_seconds": 45,
        "dry_run": False,
    },
    "runtime": {
        "python_executable": "",
        "helper_timeout_seconds": 180,
    },
    "lot_filter": {
        "require_marker": True,
        "markers": ["#fragment_stars"],
        "subcategory_ids": [],
    },
    "username": {
        "field_ids": ["player", "username", "telegram", "telegram_username"],
        "ask_in_chat_if_missing": True,
    },
    "amount": {
        "mode": "auto",
        "fixed_stars_per_unit": 100,
        "title_regex": r"(?i)(\d[\d\s]{0,10})\s*(?:telegram\s*)?(?:stars?|зв[её]зд)",
        "minimum": 50,
        "maximum": 1000000,
        "allowed": [],
    },
    "safety": {
        "purchase_delay_seconds": 2,
        "recover_unknown_paid_on_startup": False,
    },
    "messages": {
        "ask_username": (
            "Заказ #{order_id} принят. Пришлите Telegram username получателя одним сообщением "
            "в формате @username. Не отправляйте номер телефона или ссылку-приглашение."
        ),
        "processing": "Username @{username} принят. Отправляю {stars} Telegram Stars через Fragment…",
        "success": (
            "Готово: {stars} Telegram Stars отправлены пользователю @{username}. "
            "Заказ #{order_id}. Транзакция: {txid}"
        ),
        "submitted": (
            "Транзакция на {stars} Telegram Stars для @{username} отправлена в сеть. "
            "Заказ #{order_id}. Транзакция: {txid}"
        ),
        "invalid_username": (
            "Не удалось найти получателя @{username}. Проверьте username и пришлите его ещё раз "
            "одним сообщением в формате @username."
        ),
        "manual_review": (
            "Автовыдача заказа #{order_id} временно остановлена. Продавец уже получил уведомление; "
            "повторная покупка автоматически не выполняется, чтобы исключить двойную выдачу."
        ),
        "dry_run": (
            "Тестовый режим: покупка {stars} Stars для @{username} по заказу #{order_id} "
            "успешно смоделирована. Реальная транзакция не отправлялась."
        ),
    },
}

USERNAME_RE = re.compile(
    r"^(?:https?://t\.me/)?@?([A-Za-z][A-Za-z0-9_]{4,31})/?$", re.IGNORECASE
)
FIELD_NAME_RE = re.compile(
    r"telegram|username|user\s*name|логин|ник(?:нейм)?|получател", re.IGNORECASE
)

_QUEUE: queue.Queue[str | None] = queue.Queue()
_STOP_EVENT = threading.Event()
_WORKER: threading.Thread | None = None
_CARDINAL: Any = None


def _deep_merge(defaults: dict[str, Any], override: dict[str, Any]) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for key, value in defaults.items():
        if isinstance(value, dict):
            candidate = override.get(key, {})
            result[key] = _deep_merge(
                value, candidate if isinstance(candidate, dict) else {}
            )
        else:
            result[key] = override.get(key, value)
    for key, value in override.items():
        if key not in result:
            result[key] = value
    return result


def _atomic_json_write(path: Path, data: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(path.suffix + ".tmp")
    temporary.write_text(
        json.dumps(data, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
    )
    if os.name == "posix":
        temporary.chmod(0o600)
    temporary.replace(path)


def load_config() -> dict[str, Any]:
    BASE_DIR.mkdir(parents=True, exist_ok=True)
    if not CONFIG_PATH.exists():
        _atomic_json_write(CONFIG_PATH, DEFAULT_CONFIG)
        LOGGER.warning(
            "Создан конфиг %s. Заполните его и перезапустите Cardinal.", CONFIG_PATH
        )
        return _deep_merge(DEFAULT_CONFIG, {})
    try:
        raw = json.loads(CONFIG_PATH.read_text(encoding="utf-8"))
        if not isinstance(raw, dict):
            raise TypeError("корень конфига должен быть JSON-объектом")
    except Exception as exc:
        raise RuntimeError(f"Не удалось прочитать {CONFIG_PATH}: {exc}") from exc
    return _deep_merge(DEFAULT_CONFIG, raw)


def _connect_db() -> sqlite3.Connection:
    BASE_DIR.mkdir(parents=True, exist_ok=True)
    connection = sqlite3.connect(DB_PATH, timeout=30)
    connection.row_factory = sqlite3.Row
    connection.execute("PRAGMA journal_mode=WAL")
    connection.execute("PRAGMA busy_timeout=30000")
    return connection


def init_db() -> None:
    db = _connect_db()
    try:
        db.execute(
            """
            CREATE TABLE IF NOT EXISTS orders (
                order_id TEXT PRIMARY KEY,
                chat_id TEXT NOT NULL,
                buyer_username TEXT,
                recipient_username TEXT,
                stars INTEGER NOT NULL,
                status TEXT NOT NULL,
                transaction_id TEXT,
                confirmed INTEGER,
                attempts INTEGER NOT NULL DEFAULT 0,
                error TEXT,
                created_at INTEGER NOT NULL,
                updated_at INTEGER NOT NULL
            )
            """
        )
        db.execute(
            "CREATE INDEX IF NOT EXISTS idx_fragment_stars_status ON orders(status)"
        )
        db.execute(
            "CREATE INDEX IF NOT EXISTS idx_fragment_stars_chat ON orders(chat_id, status)"
        )
        db.commit()
    finally:
        db.close()


def _db_one(sql: str, parameters: tuple[Any, ...] = ()) -> sqlite3.Row | None:
    db = _connect_db()
    try:
        return db.execute(sql, parameters).fetchone()
    finally:
        db.close()


def _db_all(sql: str, parameters: tuple[Any, ...] = ()) -> list[sqlite3.Row]:
    db = _connect_db()
    try:
        return db.execute(sql, parameters).fetchall()
    finally:
        db.close()


def _db_execute(sql: str, parameters: tuple[Any, ...] = ()) -> int:
    db = _connect_db()
    try:
        cursor = db.execute(sql, parameters)
        db.commit()
        return cursor.rowcount
    finally:
        db.close()


def normalize_username(value: Any) -> str | None:
    if value is None:
        return None
    match = USERNAME_RE.fullmatch(str(value).strip())
    return match.group(1) if match else None


def _field_value(order: Any, field_id: str) -> Any:
    getter = getattr(order, "get_field_value_any", None)
    if callable(getter):
        value = getter(field_id)
        if value:
            return value
    field = getattr(order, "fields", {}).get(field_id)
    value = getattr(field, "value", None)
    if isinstance(value, dict):
        return value.get(getattr(order, "locale", "ru")) or next(
            iter(value.values()), None
        )
    return value


def extract_username(order: Any, config: dict[str, Any]) -> str | None:
    username = normalize_username(getattr(order, "player", None))
    if username:
        return username

    configured_ids = {
        str(item).casefold() for item in config["username"].get("field_ids", [])
    }
    fields = getattr(order, "fields", {}) or {}
    actual_ids = {str(field_id).casefold(): str(field_id) for field_id in fields}
    for configured_id in configured_ids:
        field_id = actual_ids.get(configured_id, configured_id)
        username = normalize_username(_field_value(order, field_id))
        if username:
            return username

    for field_id, field in fields.items():
        field_name = str(getattr(field, "name", "") or "")
        if str(field_id).casefold() not in configured_ids and not FIELD_NAME_RE.search(
            field_name
        ):
            continue
        username = normalize_username(_field_value(order, str(field_id)))
        if username:
            return username
    return None


def _positive_int(value: Any) -> int | None:
    try:
        number = float(value)
    except (TypeError, ValueError):
        return None
    if number <= 0 or not number.is_integer():
        return None
    return int(number)


def determine_stars(order: Any, config: dict[str, Any], shortcut: Any = None) -> int:
    settings = config["amount"]
    mode = str(settings.get("mode", "auto")).lower()
    units = _positive_int(getattr(order, "amount", None)) or 1

    if mode not in {"auto", "order_amount", "fixed", "title"}:
        raise ValueError(f"неизвестный amount.mode: {mode}")
    if mode == "order_amount":
        stars = _positive_int(getattr(order, "amount", None))
    elif mode == "fixed":
        per_unit = _positive_int(settings.get("fixed_stars_per_unit"))
        stars = per_unit * units if per_unit else None
    else:
        stars = None
        minimum = _positive_int(settings.get("minimum")) or 50
        if mode == "auto" and units >= minimum:
            stars = units
        if stars is None:
            texts = [
                getattr(order, "title", None),
                getattr(order, "short_description", None),
                getattr(order, "full_description", None),
                getattr(shortcut, "description", None),
            ]
            pattern = re.compile(
                str(
                    settings.get("title_regex")
                    or DEFAULT_CONFIG["amount"]["title_regex"]
                )
            )
            for text in texts:
                if not text:
                    continue
                match = pattern.search(str(text))
                if match:
                    package = _positive_int(match.group(1).replace(" ", ""))
                    if package:
                        stars = package * units
                        break

    if stars is None:
        raise ValueError("не удалось определить количество Stars")
    minimum = _positive_int(settings.get("minimum")) or 50
    maximum = _positive_int(settings.get("maximum")) or 1_000_000
    allowed = {_positive_int(item) for item in settings.get("allowed", [])}
    allowed.discard(None)
    if not minimum <= stars <= maximum:
        raise ValueError(
            f"количество Stars {stars} вне разрешённого диапазона {minimum}..{maximum}"
        )
    if allowed and stars not in allowed:
        raise ValueError(f"количество Stars {stars} отсутствует в allowlist")
    return stars


def is_target_order(order: Any, shortcut: Any, config: dict[str, Any]) -> bool:
    filters = config["lot_filter"]
    allowed_subcategories = {int(item) for item in filters.get("subcategory_ids", [])}
    subcategory = getattr(order, "subcategory", None)
    subcategory_id = getattr(subcategory, "id", None)
    if allowed_subcategories and subcategory_id not in allowed_subcategories:
        return False

    if not filters.get("require_marker", True):
        return True
    markers = [
        str(item).lower() for item in filters.get("markers", []) if str(item).strip()
    ]
    if not markers:
        return False
    haystack = "\n".join(
        str(item)
        for item in (
            getattr(order, "title", None),
            getattr(order, "full_description", None),
            getattr(order, "payment_msg", None),
            getattr(shortcut, "description", None),
        )
        if item
    ).lower()
    return any(marker in haystack for marker in markers)


def _safe_send(
    cardinal: Any, chat_id: Any, text: str, buyer_username: str | None = None
) -> None:
    try:
        cardinal.send_message(chat_id, text, buyer_username, watermark=False)
    except Exception:
        LOGGER.exception("Не удалось отправить сообщение в FunPay-чат %s", chat_id)


def _notify_admin(cardinal: Any, text: str) -> None:
    telegram = getattr(cardinal, "telegram", None)
    if telegram is None:
        return
    try:
        telegram.send_notification(text)
    except Exception:
        LOGGER.exception("Не удалось отправить уведомление администратору")


def _format_message(config: dict[str, Any], key: str, **values: Any) -> str:
    fallback = str(DEFAULT_CONFIG["messages"][key])
    template = str(config.get("messages", {}).get(key, fallback))
    try:
        return template.format(**values)
    except (KeyError, ValueError, IndexError):
        LOGGER.exception(
            "Ошибка шаблона messages.%s; используется стандартный текст", key
        )
        return fallback.format(**values)


def _credentials(config: dict[str, Any]) -> dict[str, Any]:
    fragment = config["fragment"]
    seed = os.getenv("FPC_FRAGMENT_SEED") or str(fragment.get("seed") or "")
    api_key = os.getenv("FPC_FRAGMENT_API_KEY") or str(fragment.get("api_key") or "")
    cookies_raw: Any = os.getenv("FPC_FRAGMENT_COOKIES") or fragment.get("cookies", {})
    if isinstance(cookies_raw, str):
        cookies = json.loads(cookies_raw)
    else:
        cookies = cookies_raw
    return {
        "seed": seed.strip(),
        "api_key": api_key.strip(),
        "cookies": cookies,
        "wallet_version": str(fragment.get("wallet_version", "V5R1")),
        "api_provider": str(fragment.get("api_provider", "tonapi")),
        "timeout": float(fragment.get("timeout_seconds", 45)),
    }


def _redact_secrets(value: Any, credentials: dict[str, Any]) -> str:
    """Keep wallet/session secrets out of Cardinal logs and Telegram alerts."""
    text = str(value)
    secrets = [credentials.get("seed"), credentials.get("api_key")]
    cookies = credentials.get("cookies")
    if isinstance(cookies, dict):
        secrets.extend(cookies.values())
    for secret in sorted(
        {str(item) for item in secrets if item and len(str(item)) >= 4},
        key=len,
        reverse=True,
    ):
        text = text.replace(secret, "***")
    return text


def _helper_python(config: dict[str, Any]) -> Path:
    configured = str(config.get("runtime", {}).get("python_executable") or "").strip()
    if configured:
        return Path(configured).expanduser().resolve()
    relative = Path("Scripts/python.exe") if os.name == "nt" else Path("bin/python")
    return (BASE_DIR / "venv" / relative).resolve()


def validate_config(
    config: dict[str, Any], require_credentials: bool = True
) -> list[str]:
    errors: list[str] = []
    fragment = config.get("fragment", {})
    for key in ("enabled", "paused"):
        if not isinstance(config.get(key), bool):
            errors.append(f"{key} должен быть true или false")
    for key in ("show_sender", "dry_run"):
        if not isinstance(fragment.get(key), bool):
            errors.append(f"fragment.{key} должен быть true или false")
    if str(fragment.get("payment_method", "ton")).lower() not in {"ton", "usdt_ton"}:
        errors.append("fragment.payment_method должен быть ton или usdt_ton")
    if str(fragment.get("wallet_version", "V5R1")).upper() not in {"V4R2", "V5R1"}:
        errors.append("fragment.wallet_version должен быть V4R2 или V5R1")
    if str(fragment.get("api_provider", "tonapi")).lower() not in {
        "tonapi",
        "toncenter",
    }:
        errors.append("fragment.api_provider должен быть tonapi или toncenter")
    try:
        timeout = float(fragment.get("timeout_seconds", 45))
        if not 5 <= timeout <= 300:
            errors.append("fragment.timeout_seconds должен быть от 5 до 300")
    except (TypeError, ValueError):
        errors.append("fragment.timeout_seconds должен быть числом")

    amount = config.get("amount", {})
    if str(amount.get("mode", "auto")).lower() not in {
        "auto",
        "order_amount",
        "fixed",
        "title",
    }:
        errors.append("amount.mode должен быть auto, order_amount, fixed или title")
    minimum = _positive_int(amount.get("minimum"))
    maximum = _positive_int(amount.get("maximum"))
    if minimum is None or maximum is None or minimum > maximum:
        errors.append("некорректный диапазон amount.minimum/maximum")
    try:
        re.compile(
            str(amount.get("title_regex") or DEFAULT_CONFIG["amount"]["title_regex"])
        )
    except re.error as exc:
        errors.append(f"ошибка amount.title_regex: {exc}")
    try:
        delay = float(config.get("safety", {}).get("purchase_delay_seconds", 2))
        if not 0 <= delay <= 60:
            errors.append("safety.purchase_delay_seconds должен быть от 0 до 60")
    except (TypeError, ValueError):
        errors.append("safety.purchase_delay_seconds должен быть числом")
    if not isinstance(
        config.get("safety", {}).get("recover_unknown_paid_on_startup", False), bool
    ):
        errors.append(
            "safety.recover_unknown_paid_on_startup должен быть true или false"
        )
    try:
        helper_timeout = float(
            config.get("runtime", {}).get("helper_timeout_seconds", 180)
        )
        if not 30 <= helper_timeout <= 600:
            errors.append("runtime.helper_timeout_seconds должен быть от 30 до 600")
    except (TypeError, ValueError):
        errors.append("runtime.helper_timeout_seconds должен быть числом")
    lot_filter = config.get("lot_filter", {})
    if lot_filter.get("require_marker", True) and not any(
        str(marker).strip() for marker in lot_filter.get("markers", [])
    ):
        errors.append("lot_filter.markers не может быть пустым при require_marker=true")
    try:
        [int(item) for item in lot_filter.get("subcategory_ids", [])]
    except (TypeError, ValueError):
        errors.append("lot_filter.subcategory_ids должен содержать только числа")

    if require_credentials:
        try:
            credentials = _credentials(config)
            missing = [key for key in ("seed", "api_key") if not credentials.get(key)]
            cookies = credentials.get("cookies")
            if not isinstance(cookies, dict):
                errors.append("Fragment cookies должны быть JSON-объектом")
            else:
                missing.extend(
                    key
                    for key in ("stel_ssid", "stel_dt", "stel_token", "stel_ton_token")
                    if not cookies.get(key)
                )
            if missing:
                errors.append("не заполнены: " + ", ".join(missing))
            elif len(str(credentials.get("seed", "")).split()) not in {12, 24}:
                errors.append("seed-фраза должна содержать 12 или 24 слова")
        except (TypeError, ValueError) as exc:
            errors.append(f"ошибка секретов Fragment: {exc}")
        helper_python = _helper_python(config)
        if not helper_python.is_file():
            errors.append(f"не найден отдельный Python для Fragment: {helper_python}")
    return errors


class FragmentHelperError(RuntimeError):
    pass


async def _run_helper(
    config: dict[str, Any], operation: str, **parameters: Any
) -> dict[str, Any]:
    python_executable = _helper_python(config)
    if not python_executable.is_file():
        raise FragmentHelperError(
            f"Не найден отдельный Python для Fragment: {python_executable}"
        )
    credentials = _credentials(config)
    payload = {
        "operation": operation,
        "credentials": credentials,
        "fragment": {
            "payment_method": str(config["fragment"].get("payment_method", "ton")),
            "show_sender": bool(config["fragment"].get("show_sender", False)),
        },
        "parameters": parameters,
    }
    timeout = float(config.get("runtime", {}).get("helper_timeout_seconds", 180))
    subprocess_options: dict[str, Any] = {}
    if os.name == "nt":
        subprocess_options["creationflags"] = subprocess.CREATE_NO_WINDOW
    process = await asyncio.create_subprocess_exec(
        str(python_executable),
        str(Path(__file__).resolve()),
        "--fragment-helper",
        stdin=asyncio.subprocess.PIPE,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
        **subprocess_options,
    )
    try:
        stdout, stderr = await asyncio.wait_for(
            process.communicate(
                json.dumps(payload, ensure_ascii=False).encode("utf-8")
            ),
            timeout=timeout,
        )
    except TimeoutError as exc:
        process.kill()
        await process.wait()
        raise FragmentHelperError(
            "Fragment helper превысил таймаут; результат транзакции неизвестен"
        ) from exc

    stdout_text = stdout.decode("utf-8", errors="replace").strip()
    stderr_text = stderr.decode("utf-8", errors="replace").strip()
    lines = [line for line in stdout_text.splitlines() if line.strip()]
    if not lines:
        detail = (
            stderr_text[-700:] if stderr_text else f"exit code {process.returncode}"
        )
        detail = _redact_secrets(detail, credentials)
        raise FragmentHelperError(f"Fragment helper не вернул результат: {detail}")
    try:
        response = json.loads(lines[-1])
    except (TypeError, ValueError) as exc:
        raise FragmentHelperError("Fragment helper вернул повреждённый JSON") from exc
    if not isinstance(response, dict):
        raise FragmentHelperError("Fragment helper вернул неожиданный ответ")
    if not response.get("ok"):
        code = str(response.get("code") or "FragmentHelperError")
        message = _redact_secrets(
            response.get("error") or stderr_text[-700:] or "неизвестная ошибка",
            credentials,
        )
        raise FragmentHelperError(f"{code}: {message}")
    return response


async def _purchase(
    config: dict[str, Any], username: str, stars: int
) -> tuple[str, bool]:
    response = await _run_helper(config, "purchase", username=username, stars=stars)
    if response.get("code") == "user_not_found":
        return "", False
    return str(response["transaction_id"]), bool(response.get("confirmed", False))


async def _get_wallet_info(config: dict[str, Any]) -> dict[str, Any]:
    return await _run_helper(config, "wallet")


def _insert_order(order: Any, username: str | None, stars: int, status: str) -> bool:
    now = int(time.time())
    return bool(
        _db_execute(
            """
            INSERT OR IGNORE INTO orders
                (order_id, chat_id, buyer_username, recipient_username, stars, status, created_at, updated_at)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                str(order.id),
                str(order.chat_id),
                getattr(order, "buyer_username", None),
                username,
                stars,
                status,
                now,
                now,
            ),
        )
    )


def _prepare_candidate(cardinal: Any, shortcut: Any) -> None:
    try:
        config = load_config()
        if not config.get("enabled", True):
            return
        config_errors = validate_config(config, require_credentials=False)
        if config_errors:
            raise RuntimeError("ошибка config.json: " + "; ".join(config_errors))
        order = cardinal.account.get_order(str(shortcut.id))
        if not is_target_order(order, shortcut, config):
            return
        status_name = str(getattr(getattr(order, "status", None), "name", "")).upper()
        if status_name not in {"PAID", "CLOSED"}:
            return
        try:
            stars = determine_stars(order, config, shortcut)
        except ValueError as exc:
            LOGGER.error("Заказ #%s: %s", order.id, exc)
            _notify_admin(
                cardinal,
                f"⚠️ Fragment Stars: заказ <code>#{html.escape(str(order.id))}</code>: {html.escape(str(exc))}",
            )
            return

        username = extract_username(order, config)
        new_status = "queued" if username else "awaiting_username"
        if not _insert_order(order, username, stars, new_status):
            LOGGER.info(
                "Заказ #%s уже известен плагину; повторная постановка пропущена.",
                order.id,
            )
            return

        if username and not config.get("paused", False):
            _QUEUE.put(str(order.id))
        elif config["username"].get("ask_in_chat_if_missing", True):
            _safe_send(
                cardinal,
                order.chat_id,
                _format_message(config, "ask_username", order_id=order.id),
                order.buyer_username,
            )
        else:
            _db_execute(
                "UPDATE orders SET status='manual_review', error=?, updated_at=? WHERE order_id=?",
                ("username missing", int(time.time()), str(order.id)),
            )
            _notify_admin(
                cardinal,
                f"⚠️ Fragment Stars: в заказе <code>#{html.escape(str(order.id))}</code> нет username.",
            )
    except Exception as exc:
        LOGGER.exception("Ошибка подготовки заказа #%s", getattr(shortcut, "id", "?"))
        _notify_admin(
            cardinal,
            f"⚠️ Fragment Stars: ошибка подготовки заказа: {html.escape(str(exc))}",
        )


def _process_order(cardinal: Any, order_id: str) -> None:
    row = _db_one("SELECT * FROM orders WHERE order_id=?", (order_id,))
    if row is None or row["status"] != "queued":
        return
    username = str(row["recipient_username"])
    stars = int(row["stars"])
    config = _deep_merge(DEFAULT_CONFIG, {})

    try:
        config = load_config()
        if not config.get("enabled", True) or config.get("paused", False):
            return
        config_errors = validate_config(
            config,
            require_credentials=not bool(
                config.get("fragment", {}).get("dry_run", False)
            ),
        )
        if config_errors:
            _db_execute(
                "UPDATE orders SET status='failed', error=?, updated_at=? WHERE order_id=? AND status='queued'",
                ("; ".join(config_errors)[:1000], int(time.time()), order_id),
            )
            _notify_admin(
                cardinal,
                f"⚠️ Fragment Stars: заказ <code>#{html.escape(order_id)}</code> не запущен из-за конфига: "
                f"<code>{html.escape('; '.join(config_errors)[:700])}</code>",
            )
            return

        claimed = _db_execute(
            """
            UPDATE orders SET status='processing', attempts=attempts+1, updated_at=?
            WHERE order_id=? AND status='queued'
            """,
            (int(time.time()), order_id),
        )
        if not claimed:
            return

        _safe_send(
            cardinal,
            row["chat_id"],
            _format_message(
                config, "processing", username=username, stars=stars, order_id=order_id
            ),
            row["buyer_username"],
        )
        order = cardinal.account.get_order(order_id)
        status_name = str(getattr(getattr(order, "status", None), "name", "")).upper()
        if status_name not in {"PAID", "CLOSED"}:
            raise RuntimeError(
                f"статус FunPay-заказа изменился на {status_name or 'UNKNOWN'}"
            )

        delay = max(
            0.0,
            min(float(config.get("safety", {}).get("purchase_delay_seconds", 2)), 60.0),
        )
        if delay:
            time.sleep(delay)
        # Re-check after the delay so a refund/cancellation cannot race the purchase.
        order = cardinal.account.get_order(order_id)
        status_name = str(getattr(getattr(order, "status", None), "name", "")).upper()
        if status_name not in {"PAID", "CLOSED"}:
            raise RuntimeError(
                f"статус FunPay-заказа изменился на {status_name or 'UNKNOWN'}"
            )

        if bool(config["fragment"].get("dry_run", False)):
            transaction_id = f"dry-run-{order_id}"
            _db_execute(
                """
                UPDATE orders SET status='dry_run', transaction_id=?, confirmed=0, error=NULL, updated_at=?
                WHERE order_id=?
                """,
                (transaction_id, int(time.time()), order_id),
            )
            _safe_send(
                cardinal,
                row["chat_id"],
                _format_message(
                    config, "dry_run", username=username, stars=stars, order_id=order_id
                ),
                row["buyer_username"],
            )
            _notify_admin(
                cardinal,
                f"🧪 Fragment Stars dry-run: <code>{stars}</code> ⭐ → <code>@{html.escape(username)}</code>, "
                f"заказ <code>#{html.escape(order_id)}</code>. Транзакция не отправлялась.",
            )
            return

        transaction_id, confirmed = asyncio.run(_purchase(config, username, stars))
        if not transaction_id:
            _db_execute(
                """
                UPDATE orders SET status='awaiting_username', recipient_username=NULL, error=?, updated_at=?
                WHERE order_id=?
                """,
                ("recipient not found", int(time.time()), order_id),
            )
            _safe_send(
                cardinal,
                row["chat_id"],
                _format_message(
                    config,
                    "invalid_username",
                    username=username,
                    order_id=order_id,
                    stars=stars,
                ),
                row["buyer_username"],
            )
            return

        final_status = "completed" if confirmed else "submitted"
        _db_execute(
            """
            UPDATE orders SET status=?, transaction_id=?, confirmed=?, error=NULL, updated_at=? WHERE order_id=?
            """,
            (final_status, transaction_id, int(confirmed), int(time.time()), order_id),
        )
        message_key = "success" if confirmed else "submitted"
        _safe_send(
            cardinal,
            row["chat_id"],
            _format_message(
                config,
                message_key,
                username=username,
                stars=stars,
                order_id=order_id,
                txid=transaction_id,
            ),
            row["buyer_username"],
        )
        _notify_admin(
            cardinal,
            f"✅ Fragment Stars: <code>{stars}</code> ⭐ → <code>@{html.escape(username)}</code>, "
            f"заказ <code>#{html.escape(order_id)}</code>, tx <code>{html.escape(transaction_id)}</code>.",
        )
        LOGGER.info(
            "Заказ #%s выполнен: %s Stars -> @%s, tx=%s",
            order_id,
            stars,
            username,
            transaction_id,
        )
    except Exception as exc:
        # The exception may happen after a blockchain broadcast. Never retry automatically.
        current = _db_one("SELECT status FROM orders WHERE order_id=?", (order_id,))
        current_status = current["status"] if current else None
        if current_status == "processing":
            _db_execute(
                "UPDATE orders SET status='manual_review', error=?, updated_at=? WHERE order_id=?",
                (str(exc)[:1000], int(time.time()), order_id),
            )
        elif current_status == "queued":
            _db_execute(
                "UPDATE orders SET status='failed', error=?, updated_at=? WHERE order_id=?",
                (str(exc)[:1000], int(time.time()), order_id),
            )
        LOGGER.exception("Заказ #%s переведён на ручную проверку", order_id)
        _safe_send(
            cardinal,
            row["chat_id"],
            _format_message(
                config,
                "manual_review",
                order_id=order_id,
                username=username,
                stars=stars,
            ),
            row["buyer_username"],
        )
        _notify_admin(
            cardinal,
            f"🚨 Fragment Stars: заказ <code>#{html.escape(order_id)}</code> требует ручной проверки. "
            f"Ошибка: <code>{html.escape(str(exc)[:700])}</code>",
        )


def _worker_loop() -> None:
    while not _STOP_EVENT.is_set():
        try:
            order_id = _QUEUE.get(timeout=1)
        except queue.Empty:
            continue
        if order_id is None:
            _QUEUE.task_done()
            break
        try:
            if _CARDINAL is not None:
                _process_order(_CARDINAL, order_id)
        except Exception as exc:
            # One malformed order must never kill the single wallet worker.
            LOGGER.exception("Необработанная ошибка worker для заказа #%s", order_id)
            _db_execute(
                """
                UPDATE orders SET status='manual_review', error=?, updated_at=?
                WHERE order_id=? AND status='processing'
                """,
                (f"worker error: {exc}"[:1000], int(time.time()), order_id),
            )
        finally:
            _QUEUE.task_done()


def _ensure_worker(cardinal: Any) -> None:
    global _CARDINAL, _WORKER
    _CARDINAL = cardinal
    init_db()
    if _WORKER is None or not _WORKER.is_alive():
        # A crash/restart during processing has an unknown on-chain outcome.
        _db_execute(
            """
            UPDATE orders SET status='manual_review', error=?, updated_at=? WHERE status='processing'
            """,
            ("Cardinal restarted while purchase outcome was unknown", int(time.time())),
        )
        _STOP_EVENT.clear()
        _WORKER = threading.Thread(
            target=_worker_loop, name="FragmentStarsWorker", daemon=True
        )
        _WORKER.start()
        try:
            config = load_config()
        except (OSError, RuntimeError):
            config = _deep_merge(DEFAULT_CONFIG, {})
        if config.get("enabled", True) and not config.get("paused", False):
            for row in _db_all(
                "SELECT order_id FROM orders WHERE status='queued' ORDER BY created_at"
            ):
                _QUEUE.put(str(row["order_id"]))


def on_post_init(cardinal: Any, *args: Any) -> None:
    _ensure_worker(cardinal)
    try:
        config = load_config()
        errors = validate_config(
            config,
            require_credentials=not bool(
                config.get("fragment", {}).get("dry_run", False)
            ),
        )
        if errors:
            LOGGER.warning("Fragment Stars не готов к покупкам: %s", "; ".join(errors))
        if config.get("paused", False):
            LOGGER.warning("Fragment Stars поставлен на паузу в config.json")
    except Exception:
        LOGGER.exception("Ошибка инициализации Fragment Stars")


def on_new_order(cardinal: Any, event: Any, *args: Any) -> None:
    # Fetching full order data is network I/O; keep Cardinal's event loop responsive.
    threading.Thread(
        target=_prepare_candidate,
        args=(cardinal, event.order),
        name=f"FragmentStarsPrepare-{getattr(event.order, 'id', 'unknown')}",
        daemon=True,
    ).start()


def on_initial_order(cardinal: Any, event: Any, *args: Any) -> None:
    """Optionally recover still-paid orders; never replay historical closed sales."""
    try:
        recover_unknown = bool(
            load_config()
            .get("safety", {})
            .get("recover_unknown_paid_on_startup", False)
        )
    except (OSError, RuntimeError):
        recover_unknown = False
    if not recover_unknown:
        return
    status_name = str(getattr(getattr(event.order, "status", None), "name", "")).upper()
    if status_name == "PAID":
        on_new_order(cardinal, event, *args)


def on_new_message(cardinal: Any, event: Any, *args: Any) -> None:
    message = event.message
    if getattr(message, "author_id", 0) in (0, getattr(cardinal.account, "id", None)):
        return
    username = normalize_username(getattr(message, "text", None))
    if not username:
        return
    row = _db_one(
        """
        SELECT * FROM orders WHERE chat_id=? AND status='awaiting_username'
        ORDER BY created_at ASC LIMIT 1
        """,
        (str(message.chat_id),),
    )
    if row is None:
        return
    updated = _db_execute(
        """
        UPDATE orders SET recipient_username=?, status='queued', error=NULL, updated_at=?
        WHERE order_id=? AND status='awaiting_username'
        """,
        (username, int(time.time()), row["order_id"]),
    )
    if updated:
        try:
            paused = bool(load_config().get("paused", False))
        except (OSError, RuntimeError):
            paused = True
        if not paused:
            _QUEUE.put(str(row["order_id"]))


def _telegram_status(message: Any) -> None:
    if _CARDINAL is None or getattr(_CARDINAL, "telegram", None) is None:
        return
    parts = (getattr(message, "text", "") or "").split(maxsplit=1)
    if len(parts) == 2:
        order_id = parts[1].lstrip("#").strip()
        rows = (
            [row]
            if (row := _db_one("SELECT * FROM orders WHERE order_id=?", (order_id,)))
            else []
        )
    else:
        rows = _db_all("SELECT * FROM orders ORDER BY updated_at DESC LIMIT 10")
    if not rows:
        text = "Заказы Fragment Stars не найдены."
    else:
        try:
            config = load_config()
            mode = "DRY-RUN" if config["fragment"].get("dry_run", False) else "БОЕВОЙ"
            service = "ПАУЗА" if config.get("paused", False) else "РАБОТАЕТ"
        except (OSError, RuntimeError, KeyError, TypeError):
            mode, service = "НЕИЗВЕСТНО", "ОШИБКА КОНФИГА"
        lines = [f"Fragment Stars: {service}, режим {mode}.", "Последние заказы:"]
        for row in rows:
            recipient = (
                f"@{row['recipient_username']}" if row["recipient_username"] else "—"
            )
            tx = row["transaction_id"] or "—"
            lines.append(
                f"#{row['order_id']} | {row['status']} | {row['stars']} ⭐ | {recipient} | tx: {tx}"
            )
        text = "\n".join(lines)
    _CARDINAL.telegram.bot.send_message(message.chat.id, html.escape(text))


def _telegram_check_worker(chat_id: Any) -> None:
    if _CARDINAL is None or getattr(_CARDINAL, "telegram", None) is None:
        return
    try:
        config = load_config()
        errors = validate_config(config, require_credentials=True)
        if errors:
            raise RuntimeError("; ".join(errors))
        wallet = asyncio.run(_get_wallet_info(config))
        payment = str(config["fragment"].get("payment_method", "ton"))
        text = (
            "✅ Подключение к кошельку работает.\n"
            f"Адрес: {wallet['address']}\n"
            f"Состояние: {wallet['state']}\n"
            f"TON: {float(wallet['gram_balance']):.4f}\n"
            f"USDT TON: {float(wallet['usdt_balance']):.4f}\n"
            f"Способ оплаты: {payment}\n"
            f"pyfragment: {wallet.get('pyfragment_version', 'unknown')}"
        )
    except Exception as exc:
        LOGGER.exception("Проверка Fragment/кошелька завершилась ошибкой")
        text = f"❌ Проверка Fragment/кошелька не пройдена: {exc}"
    _CARDINAL.telegram.bot.send_message(chat_id, html.escape(text))


def _telegram_check(message: Any) -> None:
    if _CARDINAL is None or getattr(_CARDINAL, "telegram", None) is None:
        return
    _CARDINAL.telegram.bot.send_message(
        message.chat.id, "Проверяю конфиг и подключение к TON-кошельку…"
    )
    threading.Thread(
        target=_telegram_check_worker,
        args=(message.chat.id,),
        name="FragmentStarsCheck",
        daemon=True,
    ).start()


def _set_paused(paused: bool) -> None:
    config = load_config()
    config["paused"] = paused
    _atomic_json_write(CONFIG_PATH, config)


def _telegram_pause(message: Any) -> None:
    if _CARDINAL is None or getattr(_CARDINAL, "telegram", None) is None:
        return
    _set_paused(True)
    _CARDINAL.telegram.bot.send_message(
        message.chat.id,
        "⏸ Автовыдача поставлена на паузу. Уже начатая транзакция может завершиться; новые покупки не запускаются.",
    )


def _telegram_resume(message: Any) -> None:
    if _CARDINAL is None or getattr(_CARDINAL, "telegram", None) is None:
        return
    _set_paused(False)
    queued = _db_all(
        "SELECT order_id FROM orders WHERE status='queued' ORDER BY created_at"
    )
    for row in queued:
        _QUEUE.put(str(row["order_id"]))
    _CARDINAL.telegram.bot.send_message(
        message.chat.id,
        f"▶️ Автовыдача продолжена. В очередь возвращено заказов: {len(queued)}.",
    )


def _telegram_set_username(message: Any) -> None:
    if _CARDINAL is None or getattr(_CARDINAL, "telegram", None) is None:
        return
    parts = (getattr(message, "text", "") or "").split()
    if len(parts) != 3:
        _CARDINAL.telegram.bot.send_message(
            message.chat.id,
            "Использование: /fragment_set_username ORDER_ID @username",
        )
        return
    order_id = parts[1].lstrip("#").strip()
    username = normalize_username(parts[2])
    if not username:
        _CARDINAL.telegram.bot.send_message(
            message.chat.id, "Некорректный Telegram username."
        )
        return
    row = _db_one("SELECT * FROM orders WHERE order_id=?", (order_id,))
    if row is None:
        _CARDINAL.telegram.bot.send_message(
            message.chat.id, "Заказ не найден в базе плагина."
        )
        return
    if row["status"] == "awaiting_username":
        _db_execute(
            "UPDATE orders SET recipient_username=?, status='queued', error=NULL, updated_at=? WHERE order_id=?",
            (username, int(time.time()), order_id),
        )
        config = load_config()
        if not config.get("paused", False):
            _QUEUE.put(order_id)
        reply = f"Username заказа #{order_id} изменён на @{username}; заказ поставлен в очередь."
    elif row["status"] in {"manual_review", "failed"}:
        _db_execute(
            "UPDATE orders SET recipient_username=?, updated_at=? WHERE order_id=?",
            (username, int(time.time()), order_id),
        )
        reply = (
            f"Username заказа #{order_id} изменён на @{username}. Статус не изменён; "
            f"после проверки транзакций используйте /fragment_retry {order_id}."
        )
    else:
        reply = f"Username нельзя изменить при статусе {row['status']}."
    _CARDINAL.telegram.bot.send_message(message.chat.id, html.escape(reply))


def _telegram_retry(message: Any) -> None:
    if _CARDINAL is None or getattr(_CARDINAL, "telegram", None) is None:
        return
    parts = (getattr(message, "text", "") or "").split(maxsplit=1)
    if len(parts) != 2:
        _CARDINAL.telegram.bot.send_message(
            message.chat.id, "Использование: /fragment_retry ORDER_ID"
        )
        return
    order_id = parts[1].lstrip("#").strip()
    row = _db_one("SELECT * FROM orders WHERE order_id=?", (order_id,))
    if row is None:
        _CARDINAL.telegram.bot.send_message(
            message.chat.id, "Заказ не найден в базе плагина."
        )
        return
    if row["status"] not in {"manual_review", "failed"}:
        _CARDINAL.telegram.bot.send_message(
            message.chat.id,
            f"Повтор недоступен для статуса {html.escape(row['status'])}.",
        )
        return
    if not row["recipient_username"]:
        _CARDINAL.telegram.bot.send_message(
            message.chat.id, "В заказе отсутствует username получателя."
        )
        return
    _db_execute(
        "UPDATE orders SET status='queued', error=NULL, updated_at=? WHERE order_id=?",
        (int(time.time()), order_id),
    )
    _QUEUE.put(order_id)
    _CARDINAL.telegram.bot.send_message(
        message.chat.id,
        "Заказ поставлен в очередь повторно. Делайте это только после проверки кошелька и Fragment.",
    )


def on_pre_init(cardinal: Any, *args: Any) -> None:
    if getattr(cardinal, "telegram", None) is None:
        return
    cardinal.telegram.msg_handler(_telegram_status, commands=["fragment_status"])
    cardinal.telegram.msg_handler(_telegram_check, commands=["fragment_check"])
    cardinal.telegram.msg_handler(_telegram_pause, commands=["fragment_pause"])
    cardinal.telegram.msg_handler(_telegram_resume, commands=["fragment_resume"])
    cardinal.telegram.msg_handler(
        _telegram_set_username, commands=["fragment_set_username"]
    )
    cardinal.telegram.msg_handler(_telegram_retry, commands=["fragment_retry"])
    cardinal.add_telegram_commands(
        UUID,
        [
            ("fragment_status", "Статус автовыдачи Fragment Stars", True),
            ("fragment_check", "Проверить Fragment и TON-кошелёк", True),
            ("fragment_pause", "Аварийная пауза Fragment Stars", False),
            ("fragment_resume", "Продолжить автовыдачу Fragment Stars", False),
            ("fragment_set_username", "Исправить username получателя", False),
            ("fragment_retry", "Ручной повтор заказа Fragment Stars", False),
        ],
    )


def on_pre_stop(cardinal: Any, *args: Any) -> None:
    _STOP_EVENT.set()
    _QUEUE.put(None)


def on_delete(cardinal: Any, call: Any) -> None:
    on_pre_stop(cardinal)
    # Config and database are deliberately preserved for audit/recovery.


BIND_TO_PRE_INIT = [on_pre_init]
BIND_TO_POST_INIT = [on_post_init]
BIND_TO_POST_START = [_ensure_worker]
BIND_TO_INIT_ORDER = [on_initial_order]
BIND_TO_NEW_ORDER = [on_new_order]
BIND_TO_NEW_MESSAGE = [on_new_message]
BIND_TO_PRE_STOP = [on_pre_stop]
BIND_TO_DELETE = on_delete


async def _fragment_helper_operation(request: dict[str, Any]) -> dict[str, Any]:
    from importlib.metadata import version

    from pyfragment import FragmentClient
    from pyfragment.enums import PaymentMethod
    from pyfragment.exceptions import UserNotFoundError

    operation = str(request.get("operation") or "")
    if operation == "self_test":
        return {"ok": True, "pyfragment_version": version("pyfragment")}
    credentials = request.get("credentials")
    if not isinstance(credentials, dict):
        raise TypeError("credentials must be an object")
    async with FragmentClient(**credentials) as client:
        if operation == "wallet":
            wallet = await client.get_wallet()
            return {
                "ok": True,
                "address": str(wallet.address),
                "state": str(wallet.state),
                "gram_balance": float(wallet.gram_balance),
                "usdt_balance": float(wallet.usdt_balance),
                "pyfragment_version": version("pyfragment"),
            }
        if operation != "purchase":
            raise ValueError(f"unsupported helper operation: {operation}")

        fragment = request.get("fragment") or {}
        parameters = request.get("parameters") or {}
        payment_name = str(fragment.get("payment_method", "ton")).lower()
        payment_methods = {
            "ton": PaymentMethod.GRAM,
            "usdt_ton": PaymentMethod.USDT_GRAM,
        }
        if payment_name not in payment_methods:
            raise ValueError(f"unsupported payment_method: {payment_name}")
        username = normalize_username(parameters.get("username"))
        stars = _positive_int(parameters.get("stars"))
        if not username or stars is None:
            raise ValueError("invalid purchase parameters")
        try:
            result = await client.purchase_stars(
                "@" + username,
                amount=stars,
                show_sender=bool(fragment.get("show_sender", False)),
                payment_method=payment_methods[payment_name],
            )
        except UserNotFoundError:
            return {"ok": True, "code": "user_not_found"}
        return {
            "ok": True,
            "transaction_id": str(result.transaction_id),
            "confirmed": bool(result.confirmed),
        }


def _fragment_helper_main() -> int:
    try:
        request = json.loads(sys.stdin.buffer.read().decode("utf-8"))
        if not isinstance(request, dict):
            raise TypeError("request must be an object")
        response = asyncio.run(_fragment_helper_operation(request))
    except Exception as exc:  # noqa: BLE001 - JSON boundary must always return a response
        response = {"ok": False, "code": type(exc).__name__, "error": str(exc)[:1000]}
    sys.stdout.write(json.dumps(response, ensure_ascii=False) + "\n")
    sys.stdout.flush()
    return 0 if response.get("ok") else 1


if __name__ == "__main__" and "--fragment-helper" in sys.argv:
    raise SystemExit(_fragment_helper_main())
