from __future__ import annotations

import os
import re
from dataclasses import dataclass


DEFAULT_ALIASES: dict[str, tuple[str, ...]] = {
    "NAS100": ("NAS100", "NAS 100", "NASDAQ", "NASDAQ 100", "US100", "USTEC", "US TECH", "US TECH 100", "NASDQ", "NAS"),
    "DOW": ("DOW", "DOW30", "DOW 30", "DOW JONES", "US30", "US 30", "DJ30", "DJIA", "WALL STREET", "WALLSTREET"),
    # The first six aliases of each key are what the AI classifier is shown
    # (interpreter.py), so the names that actually appear in the channel lead.
    "GERMAN30": (
        "GERMAN DAX INDEX", "GERMAN DAX", "DAX", "GER40", "GER 40", "DAX INDEX",
        "GERMAN30", "GERMAN 30", "GERMAN 40", "GERMANY 40", "GER30", "DE30", "DAX30", "DAX 40", "DE40",
        "GERMAN 40 INDEX", "GER40 INDEX",
    ),
    "UK100": (
        "FTSE 100 INDEX", "FTSE 100", "FTSE100", "FTSE", "UK100", "UK 100",
        "FTSE INDEX", "UK 100 INDEX", "FTSE UK 100",
    ),
    "SPX500": ("SPX500", "SPX 500", "SPX", "S&P 500", "S&P500", "S&P", "US500", "US 500", "SP500"),
    "AUS200": ("AUS200", "AUS 200", "ASX200", "ASX 200", "AUSTRALIA 200"),
    "JPN225": ("JPN225", "JP225", "NIKKEI", "NIKKEI 225", "JAPAN 225"),
    "XAUUSD": ("XAUUSD", "XAU/USD", "XAU", "GOLD"),
    "XAGUSD": ("XAGUSD", "XAG/USD", "XAG", "SILVER"),
    "USOIL": ("USOIL", "WTI", "US OIL", "CRUDE OIL", "OIL"),
    "UKOIL": ("UKOIL", "BRENT", "UK OIL", "BRENT OIL"),
    "BTCUSD": ("BTCUSD", "BTC/USD", "BTC", "BITCOIN"),
    "ETHUSD": ("ETHUSD", "ETH/USD", "ETH", "ETHEREUM"),
    "LTCUSD": ("LTCUSD", "LTC/USD", "LITECOIN"),
    "BCHUSD": ("BCHUSD", "BCH/USD", "BITCOIN CASH"),
}

FOREX_PAIRS = (
    "AUDCAD", "AUDCHF", "AUDJPY", "AUDNZD", "AUDUSD",
    "CADCHF", "CADJPY", "CHFJPY",
    "EURAUD", "EURCAD", "EURCHF", "EURGBP", "EURJPY", "EURNZD", "EURUSD",
    "GBPAUD", "GBPCAD", "GBPCHF", "GBPJPY", "GBPNZD", "GBPUSD",
    "NZDCAD", "NZDCHF", "NZDJPY", "NZDUSD",
    "USDCAD", "USDCHF", "USDJPY",
)
for _pair in FOREX_PAIRS:
    DEFAULT_ALIASES[_pair] = (_pair, f"{_pair[:3]}/{_pair[3:]}")

# Only these indices may trade without an SL, on a per-symbol fallback lot.
# Broker names: NAS100, US30 (DOW), GER40 (GERMAN30), UK100.
FALLBACK_LOT_KEYS: frozenset[str] = frozenset({"NAS100", "DOW", "GERMAN30", "UK100"})


@dataclass(frozen=True, slots=True)
class SymbolResolution:
    key: str
    mt5_symbol: str  # empty when the instrument is known but not mapped in .env
    alias: str
    start: int = 0
    end: int = 0


class SymbolMap:
    """Alias -> canonical key -> broker MT5 symbol.

    Broker symbol names are deliberately operator-owned through .env.
    Aliases have safe defaults and can be extended/overridden through ALIASES_<KEY>.
    """

    def __init__(self, env: dict[str, str] | None = None) -> None:
        env = dict(os.environ if env is None else env)
        self._mt5: dict[str, str] = {}
        self._aliases: dict[str, tuple[str, ...]] = {}

        keys = set(DEFAULT_ALIASES)
        for name in env:
            if name.startswith("MT5_SYMBOL_"):
                keys.add(name.removeprefix("MT5_SYMBOL_").upper())
            if name.startswith("ALIASES_"):
                keys.add(name.removeprefix("ALIASES_").upper())

        for key in sorted(keys):
            broker_symbol = env.get(f"MT5_SYMBOL_{key}", "").strip()
            # No instrument is live unless the operator explicitly maps it in .env.
            # This prevents an unreviewed default broker symbol from becoming tradable.
            if broker_symbol:
                self._mt5[key] = broker_symbol

            base = list(DEFAULT_ALIASES.get(key, (key,)))
            extra = [x.strip() for x in env.get(f"ALIASES_{key}", "").split(",") if x.strip()]
            seen: set[str] = set()
            aliases: list[str] = []
            for item in base + extra:
                norm = self._normalize(item)
                if norm and norm not in seen:
                    seen.add(norm)
                    aliases.append(item)
            self._aliases[key] = tuple(aliases)

    @staticmethod
    def _normalize(text: str) -> str:
        text = text.upper().replace("_", " ")
        text = re.sub(r"[^A-Z0-9/& ]+", " ", text)
        return re.sub(r"\s+", " ", text).strip()

    @staticmethod
    def _alias_regex(alias: str) -> re.Pattern[str]:
        # Spaces and slashes in signal names are optional separators.
        tokens = [re.escape(x) for x in re.split(r"[\s/]+", alias.upper()) if x]
        body = r"[\s/_-]*".join(tokens)
        return re.compile(rf"(?<![A-Z0-9]){body}(?![A-Z0-9])", re.IGNORECASE)

    def resolve_from_text(self, text: str, *, include_unmapped: bool = False) -> SymbolResolution | None:
        best: tuple[int, SymbolResolution] | None = None
        for key, aliases in self._aliases.items():
            mt5_symbol = self._mt5.get(key, "")
            if not mt5_symbol and not include_unmapped:
                continue
            for alias in aliases:
                m = self._alias_regex(alias).search(text)
                if not m:
                    continue
                score = len(m.group(0).replace(" ", ""))
                item = SymbolResolution(key=key, mt5_symbol=mt5_symbol, alias=alias, start=m.start(), end=m.end())
                if best is None or score > best[0]:
                    best = (score, item)
        return best[1] if best else None

    def mt5_symbol(self, key: str) -> str | None:
        return self._mt5.get(key.upper())

    def keys(self) -> tuple[str, ...]:
        return tuple(sorted(self._mt5))

    def aliases(self, key: str) -> tuple[str, ...]:
        return self._aliases.get(key.upper(), ())
