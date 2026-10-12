import asyncio

from tg_mt5_copytrader.contracts import AIDecision, Action, Direction
from tg_mt5_copytrader.interpreter import SignalInterpreter
from tg_mt5_copytrader.parser import DeterministicSignalParser
from tg_mt5_copytrader.symbol_map import SymbolMap


ENV = {
    "MT5_SYMBOL_NAS100": "NAS100",
    "MT5_SYMBOL_DOW": "US30",
    "MT5_SYMBOL_XAUUSD": "XAUUSD",
}


class FakeAI:
    def __init__(self, **fields):
        self.fields = fields
        self.calls = 0

    async def classify(self, raw_text, *, deterministic, recent_instrument, instruments):
        self.calls += 1
        base = dict(message_type="IGNORE", instrument=None, direction=None, close_fraction=None,
                    stop_loss=None, breakeven=False, reason="test", model="fake", latency_ms=1)
        base.update(self.fields)
        return AIDecision(**base)


def interpret(text, ai, recent=None):
    sm = SymbolMap(ENV)
    it = SignalInterpreter(DeterministicSignalParser(sm), sm, ai)
    return asyncio.run(it.interpret(text, context_symbol_key=recent))


def test_plain_chat_skips_ai():
    ai = FakeAI()
    r = interpret("good morning everyone", ai)
    assert r.status == "IGNORED" and ai.calls == 0


def test_ai_understands_close_alert_variant_the_regex_misses():
    ai = FakeAI(message_type="CLOSE", instrument="NAS100", close_fraction=1.0)
    r = interpret("🚨 Trade close alert 🇺🇸 US Tech", ai, recent="XAUUSD")
    assert r.status == "OK" and r.signal.action == Action.CLOSE and r.signal.symbol_key == "NAS100"


def test_closed_half_falls_back_to_recent_open():
    ai = FakeAI(message_type="CLOSE", close_fraction=0.5)
    r = interpret("closed half", ai, recent="DOW")
    assert r.status == "OK" and r.signal.symbol_key == "DOW" and r.signal.close_fraction == 0.5


def test_instrument_mismatch_rejects():
    ai = FakeAI(message_type="CLOSE", instrument="DOW", close_fraction=1.0)
    r = interpret("Close trade alert 🇺🇸 Nasdaq", ai)
    assert r.status == "AI_REJECT" and r.signal is None


def test_ai_ignore_vetoes_parser_match():
    ai = FakeAI(message_type="IGNORE")
    r = interpret("Last week we closed Nasdaq for +200 pips", ai)
    assert r.status == "AI_REJECT"


def test_ai_invented_sl_price_rejected():
    ai = FakeAI(message_type="STOP_LOSS", instrument="NAS100", stop_loss=24400)
    r = interpret("Stop loss alert 🇺🇸 Nasdaq adjust it", ai)
    assert r.status == "AI_REJECT"


def test_stop_loss_breakeven():
    ai = FakeAI(message_type="STOP_LOSS", instrument="NAS100", breakeven=True)
    r = interpret("Stop loss alert 🇺🇸 Nasdaq breakeven", ai)
    assert r.status == "OK" and r.signal.action == Action.MODIFY_SL and r.signal.breakeven and r.signal.stop_loss is None


def test_stop_loss_price():
    ai = FakeAI(message_type="STOP_LOSS", instrument="NAS100", stop_loss=24500)
    r = interpret("Stop loss alert 🇺🇸 Nasdaq 24500", ai)
    assert r.status == "OK" and r.signal.stop_loss == 24500


def test_open_direction_must_be_in_text():
    ai = FakeAI(message_type="OPEN", instrument="XAUUSD", direction="LONG")
    assert interpret("Gold alert, going up", ai).status == "AI_REJECT"
    r = interpret("Buy gold", ai)
    assert r.status == "OK" and r.signal.direction == Direction.LONG and r.signal.mt5_symbol == "XAUUSD"


def test_ai_error_fails_closed():
    class Broken:
        async def classify(self, *a, **k):
            raise TimeoutError("slow")

    r = interpret("Buy nasdaq", Broken())
    assert r.status == "AI_ERROR_REJECT" and r.signal is None
