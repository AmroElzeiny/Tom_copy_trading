from __future__ import annotations

import os
from datetime import datetime, timezone
from typing import Any

from .config import Settings
from .contracts import Direction, ExecutionResult, ParsedSignal
from .risk import floor_volume_to_step, partial_close_volume, risk_sized_volume

try:
    import MetaTrader5 as mt5
except ImportError:  # allows parser/tests to run off-Windows
    mt5 = None  # type: ignore[assignment]

# symbol_info().filling_mode bit flags (MQL5 SYMBOL_FILLING_*). The MetaTrader5
# Python package does not export these, only the ORDER_FILLING_* enums.
SYMBOL_FILLING_FOK = 1
SYMBOL_FILLING_IOC = 2


class MT5Gateway:
    def __init__(self, settings: Settings) -> None:
        self.settings = settings
        self._day_key: str | None = None
        self._day_equity_anchor: float | None = None

    def connect(self) -> None:
        if mt5 is None:
            raise RuntimeError("MetaTrader5 Python package is not installed")
        kwargs: dict[str, Any] = {}
        if self.settings.mt5_login is not None:
            kwargs["login"] = self.settings.mt5_login
        if self.settings.mt5_password:
            kwargs["password"] = self.settings.mt5_password
        if self.settings.mt5_server:
            kwargs["server"] = self.settings.mt5_server
        if self.settings.mt5_terminal_path:
            ok = mt5.initialize(self.settings.mt5_terminal_path, **kwargs)
        else:
            ok = mt5.initialize(**kwargs)
        if not ok:
            raise RuntimeError(f"mt5.initialize failed: {mt5.last_error()}")
        info = mt5.terminal_info()
        if info is None or not bool(getattr(info, "connected", False)):
            raise RuntimeError("MT5 terminal is not connected")

    def shutdown(self) -> None:
        if mt5 is not None:
            mt5.shutdown()

    def _account(self):
        account = mt5.account_info()
        if account is None:
            raise RuntimeError(f"account_info failed: {mt5.last_error()}")
        return account

    def _daily_loss_blocked(self) -> tuple[bool, str]:
        account = self._account()
        key = datetime.now(timezone.utc).date().isoformat()
        if self._day_key != key or not self._day_equity_anchor:
            self._day_key = key
            self._day_equity_anchor = float(account.equity)
        cap = self.settings.daily_loss_lock_pct
        if cap <= 0:
            return False, ""
        anchor = float(self._day_equity_anchor)
        current = float(account.equity)
        loss_pct = max(0.0, ((anchor - current) / anchor) * 100.0) if anchor > 0 else 0.0
        return (loss_pct >= cap, f"daily_loss_lock {loss_pct:.3f}% >= {cap:.3f}%")

    def _symbol_info(self, symbol: str):
        if not mt5.symbol_select(symbol, True):
            raise RuntimeError(f"symbol_select failed for {symbol}: {mt5.last_error()}")
        info = mt5.symbol_info(symbol)
        if info is None:
            raise RuntimeError(f"symbol_info failed for {symbol}")
        return info

    def _managed_positions(self, symbol: str):
        positions = mt5.positions_get(symbol=symbol) or ()
        return [p for p in positions if int(getattr(p, "magic", -1)) == self.settings.magic_number]

    def _managed_orders(self, symbol: str):
        orders = mt5.orders_get(symbol=symbol) or ()
        return [o for o in orders if int(getattr(o, "magic", -1)) == self.settings.magic_number]

    def _filling_mode(self, info, *, pending: bool) -> int:
        if pending:
            return mt5.ORDER_FILLING_RETURN
        execution = int(getattr(info, "trade_exemode", -1))
        filling = int(getattr(info, "filling_mode", 0))
        if execution == mt5.SYMBOL_TRADE_EXECUTION_MARKET:
            if filling & SYMBOL_FILLING_IOC:
                return mt5.ORDER_FILLING_IOC
            if filling & SYMBOL_FILLING_FOK:
                return mt5.ORDER_FILLING_FOK
            # FOK is enum zero and is the least-surprising final attempt for
            # brokers that expose incomplete filling flags. order_check still guards it.
            return mt5.ORDER_FILLING_FOK
        return mt5.ORDER_FILLING_RETURN

    def _validate_geometry(self, signal: ParsedSignal, entry: float) -> str | None:
        if signal.stop_loss is not None:
            if signal.direction == Direction.LONG and signal.stop_loss >= entry:
                return "invalid_long_sl_not_below_entry"
            if signal.direction == Direction.SHORT and signal.stop_loss <= entry:
                return "invalid_short_sl_not_above_entry"
        tp = self._tp_value(signal)
        if tp > 0:
            if signal.direction == Direction.LONG and tp <= entry:
                return "invalid_long_tp_not_above_entry"
            if signal.direction == Direction.SHORT and tp >= entry:
                return "invalid_short_tp_not_below_entry"
        return None

    def _fallback_volume(self, signal: ParsedSignal, info) -> float:
        requested = self.settings.fallback_lot_for(signal.symbol_key)
        if requested <= 0:
            # Not one of the fallback-eligible indices: no SL means no trade.
            return 0.0
        return floor_volume_to_step(
            requested,
            float(info.volume_min),
            min(float(info.volume_max), self.settings.max_volume_lots),
            float(info.volume_step),
        )

    def _entry_reference(self, signal: ParsedSignal, tick) -> float:
        if signal.entry_low is not None and signal.entry_high is not None:
            return (signal.entry_low + signal.entry_high) / 2.0
        return float(tick.ask if signal.direction == Direction.LONG else tick.bid)

    def _volume(self, signal: ParsedSignal, info, entry: float) -> tuple[float, str]:
        if signal.stop_loss is None:
            volume = self._fallback_volume(signal, info)
            if volume <= 0:
                return 0.0, "no_sl_and_symbol_not_fallback_eligible"
            return volume, "fallback_lot_no_sl"
        account = self._account()
        order_type = mt5.ORDER_TYPE_BUY if signal.direction == Direction.LONG else mt5.ORDER_TYPE_SELL
        pnl = mt5.order_calc_profit(order_type, signal.mt5_symbol, 1.0, entry, signal.stop_loss)
        if pnl is None or abs(float(pnl)) <= 0:
            return 0.0, "risk_calc_failed_with_sl"
        volume = risk_sized_volume(
            equity=float(account.equity),
            risk_pct=self.settings.risk_per_trade_pct,
            loss_per_one_lot=abs(float(pnl)),
            min_volume=float(info.volume_min),
            max_volume=float(info.volume_max),
            step=float(info.volume_step),
            hard_max_volume=self.settings.max_volume_lots,
        )
        return volume, "risk_pct_with_sl"

    def _tp_value(self, signal: ParsedSignal) -> float:
        if not signal.take_profits or self.settings.tp_policy == "NONE":
            return 0.0
        return float(signal.take_profits[-1] if self.settings.tp_policy == "LAST" else signal.take_profits[0])

    def _choose_order(self, signal: ParsedSignal, info, tick) -> tuple[int, float, str]:
        market_price = float(tick.ask if signal.direction == Direction.LONG else tick.bid)
        if signal.entry_low is None or signal.entry_high is None:
            return (
                mt5.ORDER_TYPE_BUY if signal.direction == Direction.LONG else mt5.ORDER_TYPE_SELL,
                market_price,
                "market_no_entry",
            )
        if signal.entry_low <= market_price <= signal.entry_high:
            return (
                mt5.ORDER_TYPE_BUY if signal.direction == Direction.LONG else mt5.ORDER_TYPE_SELL,
                market_price,
                "market_inside_entry_range",
            )
        point = float(info.point)
        nearest = signal.entry_low if abs(market_price - signal.entry_low) <= abs(market_price - signal.entry_high) else signal.entry_high
        if abs(market_price - nearest) <= self.settings.entry_tolerance_points * point:
            return (
                mt5.ORDER_TYPE_BUY if signal.direction == Direction.LONG else mt5.ORDER_TYPE_SELL,
                market_price,
                "market_within_entry_tolerance",
            )
        if signal.direction == Direction.LONG:
            return (
                mt5.ORDER_TYPE_BUY_LIMIT if nearest < market_price else mt5.ORDER_TYPE_BUY_STOP,
                float(nearest),
                "pending_entry",
            )
        return (
            mt5.ORDER_TYPE_SELL_LIMIT if nearest > market_price else mt5.ORDER_TYPE_SELL_STOP,
            float(nearest),
            "pending_entry",
        )

    def open(self, signal: ParsedSignal) -> ExecutionResult:
        assert signal.mt5_symbol and signal.direction
        blocked, reason = self._daily_loss_blocked()
        if blocked:
            return ExecutionResult(False, "OPEN", signal.mt5_symbol, reason)
        info = self._symbol_info(signal.mt5_symbol)
        positions = self._managed_positions(signal.mt5_symbol)
        pending = self._managed_orders(signal.mt5_symbol)
        if len(positions) + len(pending) >= self.settings.max_positions_per_symbol:
            return ExecutionResult(False, "OPEN", signal.mt5_symbol, "max_positions_per_symbol reached")
        tick = mt5.symbol_info_tick(signal.mt5_symbol)
        if tick is None:
            return ExecutionResult(False, "OPEN", signal.mt5_symbol, "symbol_info_tick failed")
        order_type, price, entry_reason = self._choose_order(signal, info, tick)
        geometry_error = self._validate_geometry(signal, price)
        if geometry_error:
            return ExecutionResult(False, "OPEN", signal.mt5_symbol, geometry_error)
        volume, volume_reason = self._volume(signal, info, price)
        if volume <= 0:
            return ExecutionResult(False, "OPEN", signal.mt5_symbol, f"no tradable volume: {volume_reason}")
        pending_types = {mt5.ORDER_TYPE_BUY_LIMIT, mt5.ORDER_TYPE_BUY_STOP, mt5.ORDER_TYPE_SELL_LIMIT, mt5.ORDER_TYPE_SELL_STOP}
        is_pending = order_type in pending_types
        action = mt5.TRADE_ACTION_PENDING if is_pending else mt5.TRADE_ACTION_DEAL
        request = {
            "action": action,
            "symbol": signal.mt5_symbol,
            "volume": volume,
            "type": order_type,
            "price": price,
            "sl": float(signal.stop_loss or 0.0),
            "tp": self._tp_value(signal),
            "deviation": self.settings.deviation_points,
            "magic": self.settings.magic_number,
            "comment": f"TGCopy {signal.source_chat_id}:{signal.source_message_id}"[:31],
            "type_time": mt5.ORDER_TIME_GTC,
            "type_filling": self._filling_mode(info, pending=is_pending),
        }
        check = mt5.order_check(request)
        if check is None:
            return ExecutionResult(False, "OPEN", signal.mt5_symbol, f"order_check failed: {mt5.last_error()}")
        if int(getattr(check, "retcode", -1)) not in {0, mt5.TRADE_RETCODE_DONE}:
            return ExecutionResult(False, "OPEN", signal.mt5_symbol, f"order_check rejected retcode={getattr(check, 'retcode', None)} comment={getattr(check, 'comment', '')}")
        if self.settings.trading_mode == "DRY_RUN":
            return ExecutionResult(True, "OPEN_DRY_RUN", signal.mt5_symbol, f"{entry_reason}; {volume_reason}", volume=volume, price=price)
        result = mt5.order_send(request)
        if result is None:
            return ExecutionResult(False, "OPEN", signal.mt5_symbol, f"order_send returned None: {mt5.last_error()}")
        ok = int(result.retcode) in {mt5.TRADE_RETCODE_DONE, mt5.TRADE_RETCODE_PLACED, mt5.TRADE_RETCODE_DONE_PARTIAL}
        return ExecutionResult(
            ok,
            "OPEN",
            signal.mt5_symbol,
            f"{getattr(result, 'comment', '')}; {entry_reason}; {volume_reason}",
            broker_retcode=int(result.retcode),
            ticket=int(getattr(result, "order", 0) or getattr(result, "deal", 0) or 0),
            volume=float(getattr(result, "volume", volume) or volume),
            price=float(getattr(result, "price", price) or price),
        )

    def close(self, signal: ParsedSignal) -> ExecutionResult:
        assert signal.mt5_symbol
        info = self._symbol_info(signal.mt5_symbol)
        positions = self._managed_positions(signal.mt5_symbol)
        pending = self._managed_orders(signal.mt5_symbol)
        fraction = float(signal.close_fraction or 1.0)
        if not positions and (fraction < 0.999999 or not pending):
            return ExecutionResult(False, "CLOSE", signal.mt5_symbol, "no managed positions/orders to close")
        if self.settings.trading_mode == "DRY_RUN":
            return ExecutionResult(True, "CLOSE_DRY_RUN", signal.mt5_symbol, f"would close {fraction:.0%}; positions={len(positions)} pending={len(pending)}")
        acted = 0
        failures: list[str] = []
        if fraction >= 0.999999:
            for order in pending:
                res = mt5.order_send({"action": mt5.TRADE_ACTION_REMOVE, "order": int(order.ticket), "magic": self.settings.magic_number})
                if res is not None and int(res.retcode) == mt5.TRADE_RETCODE_DONE:
                    acted += 1
                else:
                    failures.append(f"cancel_order_{order.ticket}")
        for pos in positions:
            close_vol = partial_close_volume(
                current_volume=float(pos.volume),
                fraction=fraction,
                min_volume=float(info.volume_min),
                step=float(info.volume_step),
            )
            if close_vol <= 0:
                failures.append(f"position_{pos.ticket}_close_volume_zero")
                continue
            tick = mt5.symbol_info_tick(signal.mt5_symbol)
            is_buy_position = int(pos.type) == mt5.POSITION_TYPE_BUY
            req_type = mt5.ORDER_TYPE_SELL if is_buy_position else mt5.ORDER_TYPE_BUY
            price = float(tick.bid if is_buy_position else tick.ask)
            req = {
                "action": mt5.TRADE_ACTION_DEAL,
                "symbol": signal.mt5_symbol,
                "volume": close_vol,
                "type": req_type,
                "position": int(pos.ticket),
                "price": price,
                "deviation": self.settings.deviation_points,
                "magic": self.settings.magic_number,
                "comment": f"TGClose {fraction:.0%}"[:31],
                "type_time": mt5.ORDER_TIME_GTC,
                "type_filling": self._filling_mode(info, pending=False),
            }
            check = mt5.order_check(req)
            if check is None or int(getattr(check, "retcode", -1)) not in {0, mt5.TRADE_RETCODE_DONE}:
                failures.append(f"position_{pos.ticket}_order_check_{getattr(check, 'retcode', None)}")
                continue
            res = mt5.order_send(req)
            if res is not None and int(res.retcode) in {mt5.TRADE_RETCODE_DONE, mt5.TRADE_RETCODE_DONE_PARTIAL}:
                acted += 1
            else:
                failures.append(f"position_{pos.ticket}_retcode_{getattr(res, 'retcode', None)}")
        return ExecutionResult(
            ok=(acted > 0 and not failures),
            action="CLOSE",
            symbol=signal.mt5_symbol,
            message=f"acted={acted}; failures={','.join(failures) if failures else 'none'}",
        )

    def modify_sl(self, signal: ParsedSignal) -> ExecutionResult:
        """Set or move the SL of bot-managed positions (and pending orders for explicit prices).

        breakeven=True puts the SL at each position's own entry price.
        """
        assert signal.mt5_symbol
        symbol = signal.mt5_symbol
        if not signal.breakeven and signal.stop_loss is None:
            return ExecutionResult(False, "MODIFY_SL", symbol, "no stop loss price and not breakeven")
        info = self._symbol_info(symbol)
        positions = self._managed_positions(symbol)
        # Breakeven on a not-yet-filled order is meaningless (SL would equal the entry).
        pending = [] if signal.breakeven else self._managed_orders(symbol)
        if not positions and not pending:
            return ExecutionResult(False, "MODIFY_SL", symbol, "no managed positions/orders to modify")
        tick = mt5.symbol_info_tick(symbol)
        if tick is None:
            return ExecutionResult(False, "MODIFY_SL", symbol, "symbol_info_tick failed")
        point = float(info.point)
        digits = int(info.digits)
        min_distance = int(getattr(info, "trade_stops_level", 0) or 0) * point
        dry_run = self.settings.trading_mode == "DRY_RUN"
        acted = 0
        notes: list[str] = []
        failures: list[str] = []

        for pos in positions:
            is_buy = int(pos.type) == mt5.POSITION_TYPE_BUY
            new_sl = round(float(pos.price_open) if signal.breakeven else float(signal.stop_loss), digits)
            if abs(new_sl - float(pos.sl)) < point / 2:
                acted += 1
                notes.append(f"position_{pos.ticket}_sl_already_{new_sl}")
                continue
            market = float(tick.bid if is_buy else tick.ask)
            if is_buy and new_sl > market - min_distance:
                failures.append(f"position_{pos.ticket}_sl_{new_sl}_not_below_bid_{market}")
                continue
            if not is_buy and new_sl < market + min_distance:
                failures.append(f"position_{pos.ticket}_sl_{new_sl}_not_above_ask_{market}")
                continue
            if dry_run:
                acted += 1
                notes.append(f"would_set_position_{pos.ticket}_sl_{new_sl}")
                continue
            res = mt5.order_send({
                "action": mt5.TRADE_ACTION_SLTP,
                "symbol": symbol,
                "position": int(pos.ticket),
                "sl": new_sl,
                "tp": float(pos.tp),
                "magic": self.settings.magic_number,
            })
            if res is not None and int(res.retcode) == mt5.TRADE_RETCODE_DONE:
                acted += 1
                notes.append(f"position_{pos.ticket}_sl_{new_sl}")
            else:
                failures.append(f"position_{pos.ticket}_retcode_{getattr(res, 'retcode', None)}_{getattr(res, 'comment', mt5.last_error())}")

        buy_order_types = {mt5.ORDER_TYPE_BUY_LIMIT, mt5.ORDER_TYPE_BUY_STOP, mt5.ORDER_TYPE_BUY_STOP_LIMIT}
        for order in pending:
            is_buy = int(order.type) in buy_order_types
            entry = float(order.price_open)
            new_sl = round(float(signal.stop_loss), digits)
            if is_buy and new_sl > entry - min_distance:
                failures.append(f"order_{order.ticket}_sl_{new_sl}_not_below_entry_{entry}")
                continue
            if not is_buy and new_sl < entry + min_distance:
                failures.append(f"order_{order.ticket}_sl_{new_sl}_not_above_entry_{entry}")
                continue
            if dry_run:
                acted += 1
                notes.append(f"would_set_order_{order.ticket}_sl_{new_sl}")
                continue
            res = mt5.order_send({
                "action": mt5.TRADE_ACTION_MODIFY,
                "order": int(order.ticket),
                "symbol": symbol,
                "price": entry,
                "sl": new_sl,
                "tp": float(order.tp),
                "type_time": int(order.type_time),
                "magic": self.settings.magic_number,
            })
            if res is not None and int(res.retcode) == mt5.TRADE_RETCODE_DONE:
                acted += 1
                notes.append(f"order_{order.ticket}_sl_{new_sl}")
            else:
                failures.append(f"order_{order.ticket}_retcode_{getattr(res, 'retcode', None)}_{getattr(res, 'comment', mt5.last_error())}")

        return ExecutionResult(
            ok=(acted > 0 and not failures),
            action="MODIFY_SL_DRY_RUN" if dry_run else "MODIFY_SL",
            symbol=symbol,
            message=f"breakeven={signal.breakeven}; acted={acted}; {','.join(notes) or 'none'}; failures={','.join(failures) if failures else 'none'}",
        )
