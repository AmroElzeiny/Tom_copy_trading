from __future__ import annotations

import argparse
import asyncio
import json
import logging

from .ai_confirm import OpenCodeConfirmer
from .browser_listener import BrowserSignalListener
from .config import Settings
from .engine import CopyTradingEngine
from .interpreter import SignalInterpreter
from .mt5_gateway import MT5Gateway
from .parser import DeterministicSignalParser
from .state import StateStore
from .symbol_map import SymbolMap


def _build_ai(settings: Settings) -> OpenCodeConfirmer | None:
    if not settings.ai_enabled or not settings.opencode_api_key:
        return None
    return OpenCodeConfirmer(
        api_key=settings.opencode_api_key,
        base_url=settings.opencode_base_url,
        model=settings.opencode_model,
        reasoning_effort=settings.opencode_reasoning_effort,
        timeout_sec=settings.ai_timeout_sec,
    )


async def _test_message(text: str, recent: str | None) -> None:
    """Show how a message would be interpreted. Never touches MT5."""
    settings = Settings.load(strict=False)
    symbol_map = SymbolMap()
    ai = _build_ai(settings)
    interpreter = SignalInterpreter(DeterministicSignalParser(symbol_map), symbol_map, ai, ai_fail_open=settings.ai_fail_open)
    try:
        result = await interpreter.interpret(text, context_symbol_key=recent)
    finally:
        if ai:
            await ai.aclose()
    print(json.dumps({
        "ai_used": ai is not None,
        "status": result.status,
        "reason": result.reason,
        "signal": result.signal.to_dict() if result.signal else None,
        "deterministic": result.deterministic.to_dict() if result.deterministic else None,
        "ai_decision": result.decision.to_dict() if result.decision else None,
        "ai_error": result.ai_error,
    }, ensure_ascii=False, indent=2))


async def amain(args: argparse.Namespace) -> None:
    if args.test_message:
        await _test_message(args.test_message, args.recent)
        return
    if args.probe:
        settings = Settings.load(strict=False)
        await BrowserSignalListener(settings, None, probe=True).run()
        return

    settings = Settings.load()
    symbol_map = SymbolMap()
    state = StateStore(settings.database_path, settings.audit_log_path)
    gateway = MT5Gateway(settings)
    gateway.connect()
    logging.getLogger(__name__).info("MT5 connected; TRADING_MODE=%s; mapped instruments=%s", settings.trading_mode, ",".join(symbol_map.keys()))
    ai = _build_ai(settings)
    interpreter = SignalInterpreter(DeterministicSignalParser(symbol_map), symbol_map, ai, ai_fail_open=settings.ai_fail_open)
    engine = CopyTradingEngine(settings=settings, interpreter=interpreter, state=state, mt5_gateway=gateway)
    listener = BrowserSignalListener(settings, engine.process_message)
    try:
        await listener.run()
    finally:
        if ai:
            await ai.aclose()
        gateway.shutdown()


def main() -> None:
    parser = argparse.ArgumentParser(description="Telegram Web -> MT5 copy trader")
    parser.add_argument("--probe", action="store_true", help="open Telegram Web and only print detected messages (no trading)")
    parser.add_argument("--test-message", metavar="TEXT", help="print how TEXT would be interpreted (no trading)")
    parser.add_argument("--recent", metavar="KEY", help="recent opened instrument for --test-message, e.g. NAS100")
    args = parser.parse_args()
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s %(message)s")
    logging.getLogger("httpx").setLevel(logging.WARNING)
    asyncio.run(amain(args))


if __name__ == "__main__":
    main()
