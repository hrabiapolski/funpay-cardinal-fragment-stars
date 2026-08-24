from __future__ import annotations

import copy
import importlib.util
import sys
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

PLUGIN_PATH = Path(__file__).resolve().parents[1] / "fragment_stars.py"
SPEC = importlib.util.spec_from_file_location("fragment_stars_under_test", PLUGIN_PATH)
if SPEC is None or SPEC.loader is None:
    raise RuntimeError(f"Не удалось загрузить {PLUGIN_PATH}")
plugin = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(plugin)


class FakeOrder:
    def __init__(
        self, *, amount=1, title="100 Telegram Stars", player=None, fields=None
    ):
        self.id = "TEST-1"
        self.amount = amount
        self.title = title
        self.short_description = title
        self.full_description = title + " #fragment_stars"
        self.payment_msg = ""
        self.player = player
        self.fields = fields or {}
        self.locale = "ru"
        self.subcategory = SimpleNamespace(id=777)
        self.chat_id = "users-1-2"
        self.buyer_username = "buyer"

    def get_field_value_any(self, field_id):
        field = self.fields.get(field_id)
        return getattr(field, "value", None) if field else None


class FragmentStarsTests(unittest.TestCase):
    def setUp(self):
        self.config = copy.deepcopy(plugin.DEFAULT_CONFIG)
        self.config["safety"]["purchase_delay_seconds"] = 0
        self.config["fragment"]["seed"] = " ".join(f"word{i}" for i in range(12))
        self.config["fragment"]["api_key"] = "test-api-key"
        self.config["fragment"]["cookies"] = {
            "stel_ssid": "ssid",
            "stel_dt": "dt",
            "stel_token": "token",
            "stel_ton_token": "ton-token",
        }
        self.config["runtime"]["python_executable"] = sys.executable

    def test_normalize_username(self):
        self.assertEqual(plugin.normalize_username("@valid_user"), "valid_user")
        self.assertEqual(
            plugin.normalize_username("https://t.me/valid_user"), "valid_user"
        )
        self.assertIsNone(plugin.normalize_username("+79991234567"))
        self.assertIsNone(plugin.normalize_username("https://t.me/+invite"))
        self.assertIsNone(plugin.normalize_username("bad user"))

    def test_extract_username_from_player(self):
        order = FakeOrder(player="@recipient_1")
        self.assertEqual(plugin.extract_username(order, self.config), "recipient_1")

    def test_extract_username_from_named_field(self):
        field = SimpleNamespace(name="Telegram username", value="recipient_2")
        order = FakeOrder(fields={"custom-field": field})
        self.assertEqual(plugin.extract_username(order, self.config), "recipient_2")

    def test_extract_username_field_id_is_case_insensitive(self):
        field = SimpleNamespace(name="Something", value="recipient_case")
        order = FakeOrder(fields={"Telegram_Username": field})
        self.assertEqual(plugin.extract_username(order, self.config), "recipient_case")

    def test_auto_amount_uses_currency_quantity(self):
        order = FakeOrder(amount=500, title="Telegram Stars")
        self.assertEqual(plugin.determine_stars(order, self.config), 500)

    def test_auto_amount_uses_package_title_times_units(self):
        order = FakeOrder(amount=2, title="100 Telegram Stars")
        self.assertEqual(plugin.determine_stars(order, self.config), 200)

    def test_fixed_amount(self):
        self.config["amount"]["mode"] = "fixed"
        self.config["amount"]["fixed_stars_per_unit"] = 250
        order = FakeOrder(amount=3)
        self.assertEqual(plugin.determine_stars(order, self.config), 750)

    def test_allowed_amount_rejects_unknown_package(self):
        self.config["amount"]["allowed"] = [100, 500]
        with self.assertRaises(ValueError):
            plugin.determine_stars(
                FakeOrder(amount=200, title="Telegram Stars"), self.config
            )

    def test_invalid_amount_mode_is_rejected(self):
        self.config["amount"]["mode"] = "guess"
        with self.assertRaises(ValueError):
            plugin.determine_stars(FakeOrder(), self.config)

    def test_invalid_payment_method_is_reported(self):
        self.config["fragment"]["payment_method"] = "btc"
        errors = plugin.validate_config(self.config)
        self.assertTrue(any("payment_method" in error for error in errors))

    def test_secrets_are_redacted_from_helper_errors(self):
        credentials = plugin._credentials(self.config)
        error = (
            f"seed={credentials['seed']} api={credentials['api_key']} "
            f"cookie={credentials['cookies']['stel_token']}"
        )
        redacted = plugin._redact_secrets(error, credentials)
        self.assertNotIn(credentials["seed"], redacted)
        self.assertNotIn(credentials["api_key"], redacted)
        self.assertNotIn(credentials["cookies"]["stel_token"], redacted)

    def test_startup_recovers_paid_but_not_closed_orders(self):
        cardinal = object()
        paid = SimpleNamespace(
            order=SimpleNamespace(status=SimpleNamespace(name="PAID"))
        )
        closed = SimpleNamespace(
            order=SimpleNamespace(status=SimpleNamespace(name="CLOSED"))
        )
        self.config["safety"]["recover_unknown_paid_on_startup"] = True
        with (
            patch.object(plugin, "load_config", return_value=self.config),
            patch.object(plugin, "on_new_order") as new_order,
        ):
            plugin.on_initial_order(cardinal, paid)
            plugin.on_initial_order(cardinal, closed)
        new_order.assert_called_once_with(cardinal, paid)

    def test_startup_recovery_is_disabled_by_default(self):
        cardinal = object()
        paid = SimpleNamespace(
            order=SimpleNamespace(status=SimpleNamespace(name="PAID"))
        )
        with (
            patch.object(plugin, "load_config", return_value=self.config),
            patch.object(plugin, "on_new_order") as new_order,
        ):
            plugin.on_initial_order(cardinal, paid)
        new_order.assert_not_called()

    def test_marker_filter(self):
        order = FakeOrder()
        self.assertTrue(
            plugin.is_target_order(order, SimpleNamespace(description=""), self.config)
        )
        order.full_description = "обычный лот"
        self.assertFalse(
            plugin.is_target_order(order, SimpleNamespace(description=""), self.config)
        )

    def test_order_id_is_idempotent(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            old_base, old_config, old_db = (
                plugin.BASE_DIR,
                plugin.CONFIG_PATH,
                plugin.DB_PATH,
            )
            try:
                plugin.BASE_DIR = Path(temp_dir)
                plugin.CONFIG_PATH = plugin.BASE_DIR / "config.json"
                plugin.DB_PATH = plugin.BASE_DIR / "orders.sqlite3"
                plugin.init_db()
                order = FakeOrder(player="recipient_3")
                self.assertTrue(
                    plugin._insert_order(order, "recipient_3", 100, "queued")
                )
                self.assertFalse(
                    plugin._insert_order(order, "recipient_3", 100, "queued")
                )
            finally:
                plugin.BASE_DIR, plugin.CONFIG_PATH, plugin.DB_PATH = (
                    old_base,
                    old_config,
                    old_db,
                )

    def test_successful_processing_is_persisted(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            old_base, old_config, old_db = (
                plugin.BASE_DIR,
                plugin.CONFIG_PATH,
                plugin.DB_PATH,
            )
            try:
                plugin.BASE_DIR = Path(temp_dir)
                plugin.CONFIG_PATH = plugin.BASE_DIR / "config.json"
                plugin.DB_PATH = plugin.BASE_DIR / "orders.sqlite3"
                plugin.init_db()
                plugin._atomic_json_write(plugin.CONFIG_PATH, self.config)
                order = FakeOrder(player="recipient_4")
                order.status = SimpleNamespace(name="PAID")
                plugin._insert_order(order, "recipient_4", 100, "queued")
                cardinal = SimpleNamespace(
                    account=SimpleNamespace(get_order=lambda _: order)
                )
                with (
                    patch.object(
                        plugin,
                        "_purchase",
                        AsyncMock(return_value=("tx-test-123", True)),
                    ),
                    patch.object(plugin, "_safe_send"),
                    patch.object(plugin, "_notify_admin"),
                ):
                    plugin._process_order(cardinal, order.id)
                row = plugin._db_one(
                    "SELECT * FROM orders WHERE order_id=?", (order.id,)
                )
                self.assertEqual(row["status"], "completed")
                self.assertEqual(row["transaction_id"], "tx-test-123")
                self.assertEqual(row["attempts"], 1)
            finally:
                plugin.BASE_DIR, plugin.CONFIG_PATH, plugin.DB_PATH = (
                    old_base,
                    old_config,
                    old_db,
                )

    def test_uncertain_failure_requires_manual_review(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            old_base, old_config, old_db = (
                plugin.BASE_DIR,
                plugin.CONFIG_PATH,
                plugin.DB_PATH,
            )
            try:
                plugin.BASE_DIR = Path(temp_dir)
                plugin.CONFIG_PATH = plugin.BASE_DIR / "config.json"
                plugin.DB_PATH = plugin.BASE_DIR / "orders.sqlite3"
                plugin.init_db()
                plugin._atomic_json_write(plugin.CONFIG_PATH, self.config)
                order = FakeOrder(player="recipient_5")
                order.status = SimpleNamespace(name="PAID")
                plugin._insert_order(order, "recipient_5", 100, "queued")
                cardinal = SimpleNamespace(
                    account=SimpleNamespace(get_order=lambda _: order)
                )
                with (
                    patch.object(
                        plugin,
                        "_purchase",
                        AsyncMock(side_effect=RuntimeError("network outcome unknown")),
                    ),
                    patch.object(plugin, "_safe_send"),
                    patch.object(plugin, "_notify_admin"),
                    patch.object(plugin.LOGGER, "exception"),
                ):
                    plugin._process_order(cardinal, order.id)
                row = plugin._db_one(
                    "SELECT * FROM orders WHERE order_id=?", (order.id,)
                )
                self.assertEqual(row["status"], "manual_review")
                self.assertNotEqual(row["status"], "queued")
            finally:
                plugin.BASE_DIR, plugin.CONFIG_PATH, plugin.DB_PATH = (
                    old_base,
                    old_config,
                    old_db,
                )

    def test_dry_run_never_calls_purchase(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            old_base, old_config, old_db = (
                plugin.BASE_DIR,
                plugin.CONFIG_PATH,
                plugin.DB_PATH,
            )
            try:
                plugin.BASE_DIR = Path(temp_dir)
                plugin.CONFIG_PATH = plugin.BASE_DIR / "config.json"
                plugin.DB_PATH = plugin.BASE_DIR / "orders.sqlite3"
                self.config["fragment"]["dry_run"] = True
                self.config["fragment"]["seed"] = ""
                self.config["fragment"]["api_key"] = ""
                plugin.init_db()
                plugin._atomic_json_write(plugin.CONFIG_PATH, self.config)
                order = FakeOrder(player="recipient_dry")
                order.status = SimpleNamespace(name="PAID")
                plugin._insert_order(order, "recipient_dry", 100, "queued")
                cardinal = SimpleNamespace(
                    account=SimpleNamespace(get_order=lambda _: order)
                )
                purchase_mock = AsyncMock(
                    side_effect=AssertionError("purchase must not run")
                )
                with (
                    patch.object(plugin, "_purchase", purchase_mock),
                    patch.object(plugin, "_safe_send"),
                    patch.object(plugin, "_notify_admin"),
                ):
                    plugin._process_order(cardinal, order.id)
                row = plugin._db_one(
                    "SELECT * FROM orders WHERE order_id=?", (order.id,)
                )
                self.assertEqual(row["status"], "dry_run")
                purchase_mock.assert_not_awaited()
            finally:
                plugin.BASE_DIR, plugin.CONFIG_PATH, plugin.DB_PATH = (
                    old_base,
                    old_config,
                    old_db,
                )


if __name__ == "__main__":
    unittest.main()
