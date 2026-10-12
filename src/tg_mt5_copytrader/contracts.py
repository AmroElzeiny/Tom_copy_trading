from __future__ import annotations

from dataclasses import asdict, dataclass, field
from enum import Enum
from typing import Any


class Action(str, Enum):
    OPEN = "OPEN"
    CLOSE = "CLOSE"
    MODIFY_SL = "MODIFY_SL"


class Direction(str, Enum):
    LONG = "LONG"
    SHORT = "SHORT"


@dataclass(slots=True)
class ParsedSignal:
    action: Action
    symbol_key: str | None
    mt5_symbol: str | None
    direction: Direction | None = None
    entry_low: float | None = None
    entry_high: float | None = None
    stop_loss: float | None = None
    take_profits: list[float] = field(default_factory=list)
    close_fraction: float | None = None
    breakeven: bool = False
    symbol_from_context: bool = False
    source_chat_id: int | None = None
    source_message_id: int | None = None
    source_timestamp_utc: str | None = None
    raw_text: str = ""
    warnings: list[str] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        out = asdict(self)
        out["action"] = self.action.value
        out["direction"] = self.direction.value if self.direction else None
        return out


@dataclass(slots=True)
class AIDecision:
    """AI reading of a raw message. Validated against the text before use."""

    message_type: str  # OPEN / CLOSE / STOP_LOSS / IGNORE
    instrument: str | None
    direction: str | None
    close_fraction: float | None
    stop_loss: float | None
    breakeven: bool
    reason: str
    model: str
    latency_ms: int

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass(slots=True)
class ExecutionResult:
    ok: bool
    action: str
    symbol: str | None
    message: str
    broker_retcode: int | None = None
    ticket: int | None = None
    volume: float | None = None
    price: float | None = None

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)
