from __future__ import annotations

import re
from dataclasses import replace

from .contracts import Action, Direction, ParsedSignal
from .symbol_map import SymbolMap, SymbolResolution


_DIRECTION_LONG = re.compile(r"(?<!\w)(LONG|BUY(?:ING)?|BULL(?:ISH)?)(?!\w)", re.I)
_DIRECTION_SHORT = re.compile(r"(?<!\w)(SHORT|SELL(?:ING)?|BEAR(?:ISH)?)(?!\w)", re.I)

# "Close trade alert 🇺🇸 Nasdaq" — the channel's explicit close format.
_CLOSE_ALERT = re.compile(r"CLOSE\W*TRADE\W*ALERT", re.I)
# "Stop loss alert 🇺🇸 Nasdaq 24500" / "SL alert ... breakeven".
_SL_ALERT = re.compile(r"(?:STOP\W*LOSS|(?<![A-Z])SL)\W*ALERT", re.I)
_SL_MOVE = re.compile(
    r"(?<!\w)(?:MOVE[DS]?|MOVING|ADJUST(?:ED|ING)?|SET|UPDATE[DS]?|TRAIL(?:ED|ING)?|SHIFT(?:ED)?|RAISE[DS]?|LOWER(?:ED)?)"
    r"\s+(?:THE\s+|OUR\s+|MY\s+|YOUR\s+)?(?:SL|S/L|STOP\s*LOSS|STOPS?)(?!\w)",
    re.I,
)
_BREAKEVEN = re.compile(r"BREAK\W*EVEN|(?<!\w)B/E(?!\w)|(?:SL|STOP\s*LOSS|STOPS?)\s+(?:TO|AT)\s+ENTRY", re.I)
_CLOSE = re.compile(
    r"(?<!\w)(CLOSE[DS]?|CLOSING|EXIT(?:ED|ING)?|BOOK(?:ED)?\s+PROFITS?|TAKE\s+OFF|TOOK\s+OFF|"
    r"TAK(?:E|ING)\s+(?:HALF|PARTIALS?)|SECURE[DS]?\s+(?:HALF|PARTIALS?|PROFITS?))(?!\w)",
    re.I,
)
_PERCENT = re.compile(r"(?<![\d.])(\d{1,3}(?:\.\d+)?)\s*%")
_THREE_QUARTERS = re.compile(r"THREE\W*QUARTERS?", re.I)
_QUARTER = re.compile(r"(?<!\w)QUARTER(?!\w)", re.I)
_HALF = re.compile(r"(?<!\w)(HALF|PARTIALS?)(?!\w)", re.I)

# Cheap prefilter: only messages containing one of these words are worth an AI call.
_ACTIONABLE_HINT = re.compile(
    r"(?<!\w)(ALERT|CLOSE[DS]?|CLOSING|EXIT\w*|HALF|QUARTER|PARTIALS?|STOP\w*|SL|BREAK\W*EVEN|B/E|"
    r"BUY\w*|SELL\w*|LONG|SHORT|ENTRY|ENTER|TP\d?|TAKE|TOOK|SECURE\w*|BOOK\w*)(?!\w)",
    re.I,
)

_NUMBER = r"(-?\d+(?:\.\d+)?)"
_ANY_NUMBER = re.compile(r"(?<![\w.])(\d+(?:\.\d+)?)(?![\w%]|\.\d)")
_ENTRY_PATTERNS = (
    re.compile(rf"\b(?:ENTRY|ENTER|ENTRY\s*PRICE)\s*[:=@-]?\s*{_NUMBER}(?:\s*(?:-|TO|–|—)\s*{_NUMBER})?", re.I),
    re.compile(rf"@\s*{_NUMBER}(?:\s*(?:-|TO|–|—)\s*{_NUMBER})?", re.I),
    # Common compact style: "NAS100 short 24550". The number must appear
    # immediately after the direction, which avoids mistaking SL/TP values for entry.
    re.compile(rf"\b(?:LONG|BUY|SHORT|SELL)\b\s*(?:AT\s*)?[:=@-]?\s*{_NUMBER}(?:\s*(?:-|TO|–|—)\s*{_NUMBER})?", re.I),
)
_SL_PATTERNS = (
    re.compile(rf"\b(?:SL|S/L|STOP\s*LOSS|STOPLOSS)\s*[:=@-]?\s*{_NUMBER}", re.I),
    # Channel open format: "ENTRY = 25311.2\n\nSTOP = 25361.2". Same line only, so a
    # blank "STOP =" never picks up a number from a later line.
    re.compile(rf"\bSTOP[^\S\n]*[:=@-]?[^\S\n]*{_NUMBER}", re.I),
)
# New SL price in a stop-loss alert: "... SL to 24500", "stop loss alert nasdaq @ 24500".
_SL_UPDATE_PRICE = re.compile(rf"(?:(?<![A-Z])(?:SL|S/L|STOP\s*LOSS|STOPS?|ALERT|TO|AT)|@|:|=)\W{{0,3}}{_NUMBER}(?![\w%]|\.\d)", re.I)
_TP_PATTERN = re.compile(rf"\b(?:TP(?:#?\d{{1,2}})?|T/P(?:#?\d{{1,2}})?|TAKE\s*PROFIT(?:\s*#?\d{{1,2}}(?=\s*[:=@-]))?)\s*[:=@-]?\s*{_NUMBER}", re.I)


def normalize_numbers(text: str) -> str:
    """Remove thousands separators: 24,550 -> 24550."""
    return re.sub(r"(?<=\d),(?=\d{3}(?:\D|$))", "", text or "")


def numbers_in(text: str) -> list[float]:
    return [float(m.group(1)) for m in _ANY_NUMBER.finditer(normalize_numbers(text))]


class DeterministicSignalParser:
    def __init__(self, symbol_map: SymbolMap) -> None:
        self.symbol_map = symbol_map

    @staticmethod
    def direction(text: str) -> Direction | None:
        has_long = bool(_DIRECTION_LONG.search(text))
        has_short = bool(_DIRECTION_SHORT.search(text))
        if has_long == has_short:
            return None
        return Direction.LONG if has_long else Direction.SHORT

    @staticmethod
    def looks_actionable(text: str) -> bool:
        return bool(_ACTIONABLE_HINT.search(text or ""))

    @staticmethod
    def is_breakeven(text: str) -> bool:
        return bool(_BREAKEVEN.search(text or ""))

    @staticmethod
    def close_fraction(text: str) -> float | None:
        """Fraction written in the text, or None when no partial wording exists."""
        percent = _PERCENT.search(text)
        if percent:
            value = float(percent.group(1))
            if 0 < value <= 100:
                return value / 100.0
        if _THREE_QUARTERS.search(text):
            return 0.75
        if _QUARTER.search(text):
            return 0.25
        if _HALF.search(text):
            return 0.5
        return None

    @staticmethod
    def _float(value: str | None) -> float | None:
        if value is None:
            return None
        try:
            return float(value)
        except ValueError:
            return None

    def _instrument(self, scan: str, context_symbol_key: str | None) -> tuple[SymbolResolution | None, str | None, bool]:
        """Returns (resolution, key, from_context).

        A message naming an instrument that is NOT mapped must never fall back to
        the recent instrument, otherwise "close gold" could close Nasdaq.
        """
        resolution = self.symbol_map.resolve_from_text(scan)
        if resolution:
            return resolution, resolution.key, False
        if self.symbol_map.resolve_from_text(scan, include_unmapped=True):
            return None, None, False
        return None, context_symbol_key, bool(context_symbol_key)

    def sl_price(self, scan: str, resolution: SymbolResolution | None) -> float | None:
        # Blank out the instrument name so "NAS100"/"US30" digits are not read as prices.
        text = scan
        if resolution and resolution.end > resolution.start:
            text = text[: resolution.start] + " " * (resolution.end - resolution.start) + text[resolution.end :]
        match = _SL_UPDATE_PRICE.search(text)
        if match:
            return self._float(match.group(1))
        values = {float(m.group(1)) for m in _ANY_NUMBER.finditer(text)}
        return values.pop() if len(values) == 1 else None

    def parse(self, text: str, *, context_symbol_key: str | None = None) -> ParsedSignal | None:
        raw = text or ""
        if not raw.strip():
            return None
        scan = normalize_numbers(raw)
        direction = self.direction(scan)

        if _CLOSE_ALERT.search(scan):
            return self._close(scan, raw, context_symbol_key)
        if _SL_ALERT.search(scan) or (direction is None and _SL_MOVE.search(scan)):
            return self._modify_sl(scan, raw, context_symbol_key)
        if _CLOSE.search(scan):
            return self._close(scan, raw, context_symbol_key)
        if direction is None and _BREAKEVEN.search(scan):
            return self._modify_sl(scan, raw, context_symbol_key)

        # Opening signal requires BOTH a recognized mapped instrument and one
        # unambiguous direction token. Normal conversation is ignored.
        resolution = self.symbol_map.resolve_from_text(scan)
        if not resolution or direction is None:
            return None
        return self.open_signal(scan, raw, resolution.key, resolution.mt5_symbol, direction)

    def _close(self, scan: str, raw: str, context_symbol_key: str | None) -> ParsedSignal | None:
        _, key, from_context = self._instrument(scan, context_symbol_key)
        mt5_symbol = self.symbol_map.mt5_symbol(key) if key else None
        if not key or not mt5_symbol:
            return None
        fraction = self.close_fraction(scan)
        return ParsedSignal(
            action=Action.CLOSE,
            symbol_key=key,
            mt5_symbol=mt5_symbol,
            close_fraction=fraction if fraction is not None else 1.0,
            symbol_from_context=from_context,
            raw_text=raw,
        )

    def _modify_sl(self, scan: str, raw: str, context_symbol_key: str | None) -> ParsedSignal | None:
        resolution, key, from_context = self._instrument(scan, context_symbol_key)
        mt5_symbol = self.symbol_map.mt5_symbol(key) if key else None
        if not key or not mt5_symbol:
            return None
        breakeven = self.is_breakeven(scan)
        price = None if breakeven else self.sl_price(scan, resolution)
        return ParsedSignal(
            action=Action.MODIFY_SL,
            symbol_key=key,
            mt5_symbol=mt5_symbol,
            stop_loss=price,
            breakeven=breakeven,
            symbol_from_context=from_context,
            raw_text=raw,
            warnings=[] if breakeven or price is not None else ["sl_price_missing"],
        )

    def open_signal(self, scan: str, raw: str, key: str, mt5_symbol: str, direction: Direction) -> ParsedSignal:
        entry_low = entry_high = None
        for pattern in _ENTRY_PATTERNS:
            match = pattern.search(scan)
            if match:
                a = self._float(match.group(1))
                b = self._float(match.group(2)) if match.lastindex and match.lastindex >= 2 else None
                if a is not None:
                    entry_low, entry_high = (min(a, b), max(a, b)) if b is not None else (a, a)
                    break

        stop_loss = None
        for pattern in _SL_PATTERNS:
            match = pattern.search(scan)
            if match:
                stop_loss = self._float(match.group(1))
                break

        tps: list[float] = []
        for match in _TP_PATTERN.finditer(scan):
            value = self._float(match.group(1))
            if value is not None and value not in tps:
                tps.append(value)

        warnings: list[str] = []
        if re.search(r"\b(?:SL|S/L|STOP\s*LOSS|STOPLOSS|STOP)\b", scan, re.I) and stop_loss is None:
            warnings.append("sl_keyword_present_but_unparsed")
        if re.search(r"\b(?:TP|T/P|TAKE\s*PROFIT)\b", scan, re.I) and not tps:
            warnings.append("tp_keyword_present_but_unparsed")

        return ParsedSignal(
            action=Action.OPEN,
            symbol_key=key,
            mt5_symbol=mt5_symbol,
            direction=direction,
            entry_low=entry_low,
            entry_high=entry_high,
            stop_loss=stop_loss,
            take_profits=tps,
            raw_text=raw,
            warnings=warnings,
        )

    @staticmethod
    def attach_source(signal: ParsedSignal, *, chat_id: int, message_id: int, timestamp_utc: str) -> ParsedSignal:
        return replace(
            signal,
            source_chat_id=chat_id,
            source_message_id=message_id,
            source_timestamp_utc=timestamp_utc,
        )
