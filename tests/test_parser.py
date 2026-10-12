from tg_mt5_copytrader.contracts import Action, Direction
from tg_mt5_copytrader.parser import DeterministicSignalParser
from tg_mt5_copytrader.symbol_map import SymbolMap


ENV = {
    "MT5_SYMBOL_NAS100": "USTEC.cash",
    "MT5_SYMBOL_DOW": "US30.cash",
    "MT5_SYMBOL_GERMAN30": "GER40.cash",
    "MT5_SYMBOL_UK100": "UK100.cash",
    "MT5_SYMBOL_EURUSD": "EURUSD.a",
}


def parser():
    return DeterministicSignalParser(SymbolMap(ENV))


def test_ignores_chat_without_direction():
    assert parser().parse("NASDAQ looks crazy today") is None


def test_open_nasdaq_full_fields():
    s = parser().parse("NAS 100 SHORT entry 24550-24560 SL 24620 TP1 24450 TP2 24300")
    assert s is not None
    assert s.action == Action.OPEN
    assert s.direction == Direction.SHORT
    assert s.symbol_key == "NAS100"
    assert s.entry_low == 24550
    assert s.entry_high == 24560
    assert s.stop_loss == 24620
    assert s.take_profits == [24450, 24300]


def test_open_without_sl_tp_is_valid():
    s = parser().parse("DOW LONG")
    assert s is not None and s.stop_loss is None and s.take_profits == []


def test_abstract_buy_nasdaq():
    s = parser().parse("Buy nasdaq")
    assert s is not None and s.action == Action.OPEN and s.direction == Direction.LONG and s.symbol_key == "NAS100"
    assert s.entry_low is None and s.stop_loss is None


def test_open_with_sl_is_not_a_stop_loss_update():
    s = parser().parse("NAS100 buy SL 24500")
    assert s is not None and s.action == Action.OPEN and s.stop_loss == 24500


def test_forex_signal():
    s = parser().parse("EUR/USD sell @ 1.1780 sl 1.1810 tp 1.1720")
    assert s is not None
    assert s.symbol_key == "EURUSD"
    assert s.direction == Direction.SHORT


def test_close_trade_alert_with_flag():
    s = parser().parse("Close trade alert 🇺🇸 Nasdaq", context_symbol_key="DOW")
    assert s is not None and s.action == Action.CLOSE
    assert s.symbol_key == "NAS100" and s.close_fraction == 1.0 and not s.symbol_from_context


def test_close_trade_alert_variant_casing():
    s = parser().parse("CLOSE TRADE ALERT!! 🇬🇧 FTSE")
    assert s is not None and s.action == Action.CLOSE and s.symbol_key == "UK100"


def test_close_trade_alert_is_close_even_with_direction_word():
    s = parser().parse("Close trade alert 🇺🇸 Nasdaq buy")
    assert s is not None and s.action == Action.CLOSE


def test_closed_half_uses_recent_instrument():
    s = parser().parse("closed half", context_symbol_key="NAS100")
    assert s is not None and s.action == Action.CLOSE
    assert s.symbol_key == "NAS100" and s.close_fraction == 0.5 and s.symbol_from_context


def test_closed_half_without_recent_instrument_is_ignored():
    assert parser().parse("closed half") is None


def test_close_naming_unmapped_instrument_never_falls_back():
    assert parser().parse("Close trade alert 🇯🇵 Nikkei", context_symbol_key="NAS100") is None


def test_full_close():
    s = parser().parse("close NASDAQ")
    assert s is not None and s.action == Action.CLOSE and s.close_fraction == 1.0


def test_partial_close():
    s = parser().parse("close 50% NAS 100")
    assert s is not None and s.close_fraction == 0.5


def test_context_close_without_symbol():
    s = parser().parse("close 25%", context_symbol_key="UK100")
    assert s is not None and s.symbol_key == "UK100" and s.close_fraction == 0.25


def test_stop_loss_alert_with_price():
    s = parser().parse("Stop loss alert 🇺🇸 Nasdaq 24,500")
    assert s is not None and s.action == Action.MODIFY_SL
    assert s.symbol_key == "NAS100" and s.stop_loss == 24500 and not s.breakeven


def test_stop_loss_alert_does_not_read_symbol_digits_as_price():
    s = parser().parse("Stop loss alert US30 SL to 44950")
    assert s is not None and s.symbol_key == "DOW" and s.stop_loss == 44950


def test_stop_loss_alert_breakeven():
    s = parser().parse("Stop loss alert 🇩🇪 DAX move to breakeven")
    assert s is not None and s.action == Action.MODIFY_SL
    assert s.symbol_key == "GERMAN30" and s.breakeven and s.stop_loss is None


def test_breakeven_without_instrument_uses_recent():
    s = parser().parse("Move SL to break even", context_symbol_key="NAS100")
    assert s is not None and s.action == Action.MODIFY_SL and s.breakeven and s.symbol_from_context


def test_ambiguous_buy_sell_rejected():
    assert parser().parse("NAS100 buy then maybe sell") is None


def test_channel_open_format_reads_stop():
    s = parser().parse("🇩🇪  GERMAN DAX INDEX\nSOLD SHORT 🔻 = 100%\n\nENTRY = 25311.2\n\nSTOP = 25361.2")
    assert s is not None and s.action == Action.OPEN and s.direction == Direction.SHORT
    assert s.symbol_key == "GERMAN30" and s.entry_low == 25311.2 and s.stop_loss == 25361.2


def test_channel_open_format_blank_fields_is_market_without_sl():
    s = parser().parse("🇺🇸 NASDAQ INDEX\nSOLD SHORT 🔻 = 100%\n\nENTRY = \n\nSTOP =")
    assert s is not None and s.action == Action.OPEN and s.symbol_key == "NAS100"
    assert s.entry_low is None and s.stop_loss is None


def test_compact_entry_after_direction():
    s = parser().parse("NAS100 short 24,550 SL 24,620 TP 24,400")
    assert s is not None
    assert s.entry_low == 24550
    assert s.stop_loss == 24620
    assert s.take_profits == [24400]
