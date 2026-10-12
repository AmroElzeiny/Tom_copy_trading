from __future__ import annotations

import asyncio
import logging
from datetime import datetime, timezone

from .config import Settings
from .contracts import Action, ExecutionResult
from .interpreter import SignalInterpreter
from .mt5_gateway import MT5Gateway
from .state import StateStore

log = logging.getLogger(__name__)


class CopyTradingEngine:
    def __init__(
        self,
        *,
        settings: Settings,
        interpreter: SignalInterpreter,
        state: StateStore,
        mt5_gateway: MT5Gateway,
    ) -> None:
        self.settings = settings
        self.interpreter = interpreter
        self.state = state
        self.mt5 = mt5_gateway
        self._execution_lock = asyncio.Lock()

    async def process_message(self, *, chat_id: int, message_id: int, text: str, message_dt: datetime) -> ExecutionResult | None:
        now = datetime.now(timezone.utc)
        dt = message_dt if message_dt.tzinfo else message_dt.replace(tzinfo=timezone.utc)
        age = (now - dt.astimezone(timezone.utc)).total_seconds()
        if age > self.settings.max_signal_age_sec:
            self.state.audit({"event": "ignored_stale", "chat_id": chat_id, "message_id": message_id, "age_sec": age, "text": text})
            log.info("ignored stale message %s (%.0fs old)", message_id, age)
            return None
        if not self.state.claim(chat_id, message_id):
            return None

        interpretation = await self.interpreter.interpret(text, context_symbol_key=self.state.context_symbol())
        if interpretation.decision is not None or interpretation.ai_error:
            self.state.audit({
                "event": "ai_classification",
                "chat_id": chat_id,
                "message_id": message_id,
                "decision": interpretation.decision.to_dict() if interpretation.decision else None,
                "error": interpretation.ai_error,
            })
        signal = interpretation.signal
        if signal is None:
            self.state.set_status(chat_id, message_id, interpretation.status)
            if interpretation.status == "IGNORED":
                log.info("message %s ignored: %s", message_id, interpretation.reason)
                return None
            self.state.audit({"event": "rejected", "chat_id": chat_id, "message_id": message_id, "status": interpretation.status, "reason": interpretation.reason, "text": text})
            log.warning("message %s rejected (%s): %s", message_id, interpretation.status, interpretation.reason)
            return ExecutionResult(False, "REJECT", None, interpretation.reason)

        signal = self.interpreter.parser.attach_source(signal, chat_id=chat_id, message_id=message_id, timestamp_utc=dt.astimezone(timezone.utc).isoformat())
        self.state.audit({"event": "parsed", "signal": signal.to_dict(), "reason": interpretation.reason})
        log.info(
            "message %s -> %s %s dir=%s close=%s sl=%s breakeven=%s%s",
            message_id, signal.action.value, signal.mt5_symbol, signal.direction.value if signal.direction else None,
            signal.close_fraction, signal.stop_loss, signal.breakeven, " (recent instrument)" if signal.symbol_from_context else "",
        )

        handler = {Action.OPEN: self.mt5.open, Action.CLOSE: self.mt5.close, Action.MODIFY_SL: self.mt5.modify_sl}[signal.action]
        async with self._execution_lock:
            try:
                result = await asyncio.to_thread(handler, signal)
            except Exception as exc:
                result = ExecutionResult(False, signal.action.value, signal.mt5_symbol, f"execution error: {exc!r}")
        self.state.audit({"event": "execution", "signal": signal.to_dict(), "result": result.to_dict()})
        self.state.set_status(chat_id, message_id, "EXECUTED" if result.ok else "EXECUTION_FAILED", signal.symbol_key)
        (log.info if result.ok else log.error)("execution %s: %s", "OK" if result.ok else "FAILED", result.to_dict())
        # Only opened trades define the "most recent instrument" used by
        # messages like "closed half" that do not name one.
        if result.ok and signal.action == Action.OPEN and signal.symbol_key:
            self.state.set_context_symbol(chat_id, signal.symbol_key)
        return result
