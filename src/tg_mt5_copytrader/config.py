from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path

from dotenv import load_dotenv

from .symbol_map import FALLBACK_LOT_KEYS


def _bool(name: str, default: bool = False) -> bool:
    raw = os.getenv(name)
    if raw is None or not raw.strip():
        return default
    return raw.strip().lower() in {"1", "true", "yes", "on"}


def _int(name: str, default: int) -> int:
    raw = os.getenv(name, "").strip()
    return int(raw) if raw else default


def _float(name: str, default: float) -> float:
    raw = os.getenv(name, "").strip()
    return float(raw) if raw else default


@dataclass(frozen=True, slots=True)
class Settings:
    telegram_web_url: str
    browser_channel: str
    browser_profile_dir: Path
    browser_chat_title: str
    browser_poll_ms: int
    browser_include_outgoing: bool
    max_signal_age_sec: float

    mt5_terminal_path: str
    mt5_login: int | None
    mt5_password: str
    mt5_server: str
    magic_number: int
    trading_mode: str
    deviation_points: int
    entry_tolerance_points: int

    risk_per_trade_pct: float
    fallback_lot_default: float
    max_volume_lots: float
    max_positions_per_symbol: int
    daily_loss_lock_pct: float

    ai_enabled: bool
    ai_fail_open: bool
    opencode_api_key: str
    opencode_base_url: str
    opencode_model: str
    opencode_reasoning_effort: str
    ai_timeout_sec: float

    database_path: Path
    audit_log_path: Path
    tp_policy: str

    @classmethod
    def load(cls, dotenv_path: str | None = None, *, strict: bool = True) -> "Settings":
        load_dotenv(dotenv_path=dotenv_path, override=False)
        login_raw = os.getenv("MT5_LOGIN", "").strip()
        settings = cls(
            telegram_web_url=os.getenv("TELEGRAM_WEB_URL", "").strip() or "https://web.telegram.org/k/",
            browser_channel=os.getenv("BROWSER_CHANNEL", "chrome").strip().lower(),
            browser_profile_dir=Path(os.getenv("BROWSER_PROFILE_DIR", "").strip() or "runtime/browser_profile"),
            browser_chat_title=os.getenv("BROWSER_CHAT_TITLE", "").strip(),
            browser_poll_ms=_int("BROWSER_POLL_MS", 500),
            browser_include_outgoing=_bool("BROWSER_INCLUDE_OUTGOING", False),
            max_signal_age_sec=_float("MAX_SIGNAL_AGE_SEC", 60.0),
            mt5_terminal_path=os.getenv("MT5_TERMINAL_PATH", "").strip(),
            mt5_login=int(login_raw) if login_raw else None,
            mt5_password=os.getenv("MT5_PASSWORD", "").strip(),
            mt5_server=os.getenv("MT5_SERVER", "").strip(),
            magic_number=_int("MAGIC_NUMBER", 810026),
            trading_mode=os.getenv("TRADING_MODE", "DRY_RUN").strip().upper(),
            deviation_points=_int("DEVIATION_POINTS", 30),
            entry_tolerance_points=_int("ENTRY_TOLERANCE_POINTS", 20),
            risk_per_trade_pct=_float("RISK_PER_TRADE_PCT", 1.0),
            fallback_lot_default=_float("FALLBACK_LOT_DEFAULT", 0.1),
            max_volume_lots=_float("MAX_VOLUME_LOTS", 100.0),
            max_positions_per_symbol=_int("MAX_POSITIONS_PER_SYMBOL", 1),
            daily_loss_lock_pct=_float("DAILY_LOSS_LOCK_PCT", 3.0),
            ai_enabled=_bool("AI_CONFIRMATION_ENABLED", True),
            ai_fail_open=_bool("AI_FAIL_OPEN", False),
            opencode_api_key=os.getenv("OPENCODE_GO_API_KEY", "").strip(),
            opencode_base_url=(os.getenv("OPENCODE_GO_BASE_URL", "").strip() or "https://opencode.ai/zen/go/v1").rstrip("/"),
            opencode_model=os.getenv("OPENCODE_MODEL", "").strip() or "glm-5.3-flash",
            opencode_reasoning_effort=os.getenv("OPENCODE_REASONING_EFFORT", "low").strip().lower(),
            ai_timeout_sec=_float("AI_TIMEOUT_SEC", 6.0),
            database_path=Path(os.getenv("DATABASE_PATH", "").strip() or "runtime/copytrader.sqlite3"),
            audit_log_path=Path(os.getenv("AUDIT_LOG_PATH", "").strip() or "runtime/audit.jsonl"),
            tp_policy=os.getenv("TP_POLICY", "FIRST").strip().upper(),
        )
        if strict:
            settings.validate()
        return settings

    def validate(self) -> None:
        errors: list[str] = []
        if self.browser_poll_ms < 100:
            errors.append("BROWSER_POLL_MS must be >=100")
        if self.trading_mode not in {"DRY_RUN", "LIVE"}:
            errors.append("TRADING_MODE must be DRY_RUN or LIVE")
        if not (0.0 < self.risk_per_trade_pct <= 10.0):
            errors.append("RISK_PER_TRADE_PCT must be >0 and <=10")
        if self.fallback_lot_default <= 0:
            errors.append("FALLBACK_LOT_DEFAULT must be >0")
        if self.ai_enabled and not self.opencode_api_key:
            errors.append("OPENCODE_GO_API_KEY missing while AI_CONFIRMATION_ENABLED=true")
        if self.tp_policy not in {"FIRST", "LAST", "NONE"}:
            errors.append("TP_POLICY must be FIRST, LAST or NONE")
        if errors:
            raise ValueError("; ".join(errors))

    def fallback_lot_for(self, symbol_key: str | None) -> float:
        """Per-symbol fallback lot for a signal without SL.

        Only the indices in FALLBACK_LOT_KEYS (NAS100, US30, GER40, UK100) may be
        traded without an SL; every other instrument returns 0.0 so the caller
        rejects the signal instead of guessing a size.
        """
        key = (symbol_key or "").upper()
        if key not in FALLBACK_LOT_KEYS:
            return 0.0
        raw = os.getenv(f"FALLBACK_LOT_{key}", "").strip()
        lot = float(raw) if raw else self.fallback_lot_default
        return min(lot, self.max_volume_lots) if lot > 0 else 0.0
