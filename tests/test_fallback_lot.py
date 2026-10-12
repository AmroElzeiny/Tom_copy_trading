import pytest

from tg_mt5_copytrader.config import Settings


def settings(**over) -> Settings:
    base = dict(
        telegram_web_url="", browser_channel="chrome", browser_profile_dir="runtime",
        browser_chat_title="", browser_poll_ms=500, browser_include_outgoing=False,
        max_signal_age_sec=60.0, mt5_terminal_path="", mt5_login=None, mt5_password="",
        mt5_server="", magic_number=1, trading_mode="DRY_RUN", deviation_points=30,
        entry_tolerance_points=20, risk_per_trade_pct=1.0, fallback_lot_default=0.13,
        max_volume_lots=5.0, max_positions_per_symbol=1, daily_loss_lock_pct=2.0,
        ai_enabled=False, ai_fail_open=False, opencode_api_key="", opencode_base_url="",
        opencode_model="", opencode_reasoning_effort="low", ai_timeout_sec=6.0,
        database_path="runtime/db", audit_log_path="runtime/audit.jsonl", tp_policy="FIRST",
    )
    base.update(over)
    return Settings(**base)


@pytest.mark.parametrize("key", ["NAS100", "DOW", "GERMAN30", "UK100"])
def test_indices_get_default_fallback(key):
    assert settings().fallback_lot_for(key) == 0.13


@pytest.mark.parametrize("key", ["EURUSD", "XAUUSD", "BTCUSD", "SPX500", "JPN225", None])
def test_non_fallback_symbols_get_nothing(key):
    assert settings().fallback_lot_for(key) == 0.0


def test_per_symbol_override(monkeypatch):
    monkeypatch.setenv("FALLBACK_LOT_UK100", "0.4")
    s = settings()
    assert s.fallback_lot_for("UK100") == 0.4
    assert s.fallback_lot_for("NAS100") == 0.13


def test_override_capped_by_max_volume(monkeypatch):
    monkeypatch.setenv("FALLBACK_LOT_DOW", "50")
    assert settings().fallback_lot_for("DOW") == 5.0
