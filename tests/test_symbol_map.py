from tg_mt5_copytrader.symbol_map import SymbolMap


def env():
    return {
        "MT5_SYMBOL_NAS100": "USTEC.cash",
        "MT5_SYMBOL_DOW": "US30.cash",
        "MT5_SYMBOL_GERMAN30": "GER40.cash",
        "MT5_SYMBOL_UK100": "UK100.cash",
        "MT5_SYMBOL_EURUSD": "EURUSD.a",
    }


def test_index_aliases():
    sm = SymbolMap(env())
    assert sm.resolve_from_text("NAS 100 short").mt5_symbol == "USTEC.cash"
    assert sm.resolve_from_text("DOW long").key == "DOW"
    assert sm.resolve_from_text("DAX short").key == "GERMAN30"
    assert sm.resolve_from_text("FTSE100 buy").key == "UK100"


def test_long_form_index_names():
    sm = SymbolMap(env())
    assert sm.resolve_from_text("Buy German DAX INDEX now").key == "GERMAN30"
    assert sm.resolve_from_text("SELL FTSE 100 INDEX").key == "UK100"
    assert sm.resolve_from_text("closing FTSE 100").key == "UK100"
    assert sm.resolve_from_text("close trade alert FTSE 100").mt5_symbol == "UK100.cash"


def test_forex_alias_slash():
    sm = SymbolMap(env())
    hit = sm.resolve_from_text("EUR/USD sell")
    assert hit and hit.key == "EURUSD" and hit.mt5_symbol == "EURUSD.a"


def test_unmapped_index_is_disabled():
    sm = SymbolMap({})
    assert sm.resolve_from_text("NAS100 short") is None
