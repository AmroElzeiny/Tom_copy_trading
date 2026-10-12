from __future__ import annotations

from dataclasses import dataclass, replace
from typing import Protocol

from .contracts import AIDecision, Action, Direction, ParsedSignal
from .parser import DeterministicSignalParser, normalize_numbers, numbers_in
from .symbol_map import SymbolMap


class Classifier(Protocol):
    async def classify(
        self,
        raw_text: str,
        *,
        deterministic: ParsedSignal | None,
        recent_instrument: str | None,
        instruments: dict[str, list[str]],
    ) -> AIDecision: ...


@dataclass(slots=True)
class Interpretation:
    signal: ParsedSignal | None
    status: str  # OK / IGNORED / AI_REJECT / AI_ERROR_REJECT
    reason: str
    deterministic: ParsedSignal | None = None
    decision: AIDecision | None = None
    ai_error: str | None = None


_AI_ACTIONS = {"OPEN": Action.OPEN, "CLOSE": Action.CLOSE, "STOP_LOSS": Action.MODIFY_SL}


class SignalInterpreter:
    """Deterministic parse + AI classification, reconciled fail-closed.

    The AI decides what kind of message it is (so "CLOSE TRADE ALERT!!" variants and
    wording the regexes miss are understood), but every AI field is checked against
    the raw text: instruments must be mapped, prices must literally appear, and an
    OPEN direction must be backed by a buy/sell word. Any disagreement rejects.
    """

    def __init__(
        self,
        parser: DeterministicSignalParser,
        symbol_map: SymbolMap,
        ai: Classifier | None = None,
        *,
        ai_fail_open: bool = False,
    ) -> None:
        self.parser = parser
        self.symbol_map = symbol_map
        self.ai = ai
        self.ai_fail_open = ai_fail_open

    async def interpret(self, text: str, *, context_symbol_key: str | None) -> Interpretation:
        det = self.parser.parse(text, context_symbol_key=context_symbol_key)
        if det is None and not self.parser.looks_actionable(text):
            return Interpretation(None, "IGNORED", "no trade wording")
        if self.ai is None:
            return Interpretation(det, "OK" if det else "IGNORED", "deterministic only", det)
        try:
            decision = await self.ai.classify(
                text,
                deterministic=det,
                recent_instrument=context_symbol_key,
                instruments={key: list(self.symbol_map.aliases(key)[:6]) for key in self.symbol_map.keys()},
            )
        except Exception as exc:
            if self.ai_fail_open and det is not None:
                return Interpretation(det, "OK", "AI failed; fail-open used deterministic parse", det, ai_error=repr(exc))
            return Interpretation(None, "AI_ERROR_REJECT", f"AI classification failed: {exc!r}", det, ai_error=repr(exc))
        signal, status, reason = self._reconcile(text, det, decision, context_symbol_key)
        return Interpretation(signal, status, reason, det, decision)

    def _reconcile(
        self, text: str, det: ParsedSignal | None, ai: AIDecision, context_symbol_key: str | None
    ) -> tuple[ParsedSignal | None, str, str]:
        if ai.message_type == "IGNORE":
            if det is None:
                return None, "IGNORED", f"AI: {ai.reason}"
            return None, "AI_REJECT", f"AI says not a trade instruction: {ai.reason}"

        action = _AI_ACTIONS[ai.message_type]
        if det is not None and det.action != action:
            return None, "AI_REJECT", f"action mismatch parser={det.action.value} ai={ai.message_type}"

        scan = normalize_numbers(text)

        # ---- instrument ----
        named = self.symbol_map.resolve_from_text(scan, include_unmapped=True)
        from_context = False
        if named is not None:
            if not named.mt5_symbol:
                return None, "IGNORED", f"instrument {named.key} is not mapped in .env"
            key = named.key
            if ai.instrument and ai.instrument != key:
                return None, "AI_REJECT", f"instrument mismatch text={key} ai={ai.instrument}"
        elif ai.instrument:
            if not self.symbol_map.mt5_symbol(ai.instrument):
                return None, "IGNORED", f"AI instrument {ai.instrument} is not mapped in .env"
            key = ai.instrument
        elif action != Action.OPEN and context_symbol_key:
            key, from_context = context_symbol_key, True
        else:
            return None, "IGNORED", "no instrument named and no recent opened instrument"
        mt5_symbol = self.symbol_map.mt5_symbol(key)
        if not mt5_symbol:
            return None, "IGNORED", f"instrument {key} is not mapped in .env"

        # ---- action-specific fields ----
        if action == Action.OPEN:
            text_direction = self.parser.direction(scan)
            if text_direction is None:
                return None, "AI_REJECT", "no unambiguous buy/sell word in text"
            if ai.direction != text_direction.value:
                return None, "AI_REJECT", f"direction mismatch text={text_direction.value} ai={ai.direction}"
            if det is not None and det.symbol_key == key:
                return det, "OK", ai.reason
            signal = self.parser.open_signal(scan, text, key, mt5_symbol, Direction(ai.direction))
            signal.warnings.append("instrument_mapped_by_ai")
            return signal, "OK", ai.reason

        if action == Action.CLOSE:
            written = self.parser.close_fraction(scan)
            if written is not None:
                if ai.close_fraction is not None and abs(ai.close_fraction - written) > 1e-6:
                    return None, "AI_REJECT", f"close fraction mismatch text={written} ai={ai.close_fraction}"
                fraction = written
            elif ai.close_fraction is not None:
                if not (0.0 < ai.close_fraction <= 1.0):
                    return None, "AI_REJECT", f"invalid AI close fraction {ai.close_fraction}"
                fraction = ai.close_fraction
            else:
                fraction = 1.0
            signal = ParsedSignal(
                action=Action.CLOSE,
                symbol_key=key,
                mt5_symbol=mt5_symbol,
                close_fraction=fraction,
                symbol_from_context=from_context,
                raw_text=text,
            )
            return signal, "OK", ai.reason

        # MODIFY_SL
        text_breakeven = self.parser.is_breakeven(scan)
        if text_breakeven and not ai.breakeven:
            return None, "AI_REJECT", "text says breakeven but AI did not"
        breakeven = text_breakeven or ai.breakeven
        price: float | None = None
        if not breakeven:
            resolution = named if named is not None and named.mt5_symbol else None
            written_price = self.parser.sl_price(scan, resolution)
            if written_price is not None and ai.stop_loss is not None and abs(written_price - ai.stop_loss) > 1e-9:
                return None, "AI_REJECT", f"SL price mismatch text={written_price} ai={ai.stop_loss}"
            price = written_price if written_price is not None else ai.stop_loss
            if price is None:
                return None, "AI_REJECT", "stop loss alert without breakeven or a price"
            if not any(abs(n - price) < 1e-9 for n in numbers_in(scan)):
                return None, "AI_REJECT", f"SL price {price} does not appear in the message"
        signal = ParsedSignal(
            action=Action.MODIFY_SL,
            symbol_key=key,
            mt5_symbol=mt5_symbol,
            stop_loss=price,
            breakeven=breakeven,
            symbol_from_context=from_context,
            raw_text=text,
        )
        if det is not None and det.symbol_key != key:
            signal = replace(signal, warnings=[f"parser_symbol_{det.symbol_key}_overridden"])
        return signal, "OK", ai.reason
