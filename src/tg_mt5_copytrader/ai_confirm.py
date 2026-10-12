from __future__ import annotations

import hashlib
import json
import time
from typing import Any

import httpx

from .contracts import AIDecision, ParsedSignal


_SCHEMA: dict[str, Any] = {
    "type": "object",
    "additionalProperties": False,
    "required": ["message_type", "instrument", "direction", "close_fraction", "stop_loss", "breakeven", "reason"],
    "properties": {
        "message_type": {"type": "string", "enum": ["OPEN", "CLOSE", "STOP_LOSS", "IGNORE"]},
        "instrument": {"type": ["string", "null"]},
        "direction": {"type": ["string", "null"], "enum": ["LONG", "SHORT", None]},
        "close_fraction": {"type": ["number", "null"]},
        "stop_loss": {"type": ["number", "null"]},
        "breakeven": {"type": "boolean"},
        "reason": {"type": "string", "maxLength": 160},
    },
}

_SYSTEM_PROMPT = """You classify messages from a trading-signal Telegram channel for a copy trader. Return JSON only.

message_type:
- CLOSE: a close trade alert. The channel's format starts with "Close trade alert", then a country flag emoji, then the instrument (e.g. "Close trade alert 🇺🇸 Nasdaq"). Accept obvious variants (casing, punctuation, emojis, typos). Also CLOSE for any instruction or report that a trade is closed fully or partially, e.g. "closed half", "closing 50%", "take profits now".
- STOP_LOSS: a "Stop loss alert" or any instruction to place, move or adjust a stop loss. If it says breakeven (or SL to entry), set breakeven=true and stop_loss=null. Otherwise stop_loss is the new stop price written in the message.
- OPEN: an instruction to enter a new trade now, including short messages like "Buy Nasdaq" or "Sell gold". direction LONG for buy, SHORT for sell. The channel's entry format is a flag + instrument, then "SOLD SHORT 🔻 = 100%" or "BOUGHT LONG = 100%", then "ENTRY = <price>" and "STOP = <price>". That is always OPEN, even when ENTRY or STOP are left blank; "= 100%" there is the position size, never a close.
- IGNORE: commentary, analysis, recaps of past results, questions, promotions, or anything that is not an actionable instruction.

instrument: the key from `instruments` that the message names (match by the listed aliases or an obvious name such as "US Tech" -> NAS100). Use null when the message does not name an instrument; never guess from a flag alone and never use recent_instrument. If the message names an instrument that is not in the list, return its name as written.
direction: LONG/SHORT for OPEN, otherwise null.
close_fraction: for CLOSE only: 1.0 full close, 0.5 half, 0.25 quarter, 0.75 three quarters, or the written percent; otherwise null.
Copy prices exactly as written; never invent a number.
deterministic_parse is a rule-based reading that may be wrong or null. Judge the raw text yourself."""


class OpenCodeConfirmer:
    """AI classifies the raw message; interpreter.py validates every AI field against the text.

    It is not allowed to invent a symbol, change volume, or introduce prices that
    are not written in the message.
    """

    def __init__(
        self,
        *,
        api_key: str,
        base_url: str,
        model: str,
        reasoning_effort: str = "low",
        timeout_sec: float = 6.0,
    ) -> None:
        self.api_key = api_key
        self.base_url = base_url.rstrip("/")
        self.model = model
        self.reasoning_effort = reasoning_effort
        self.timeout_sec = timeout_sec
        self._client = httpx.AsyncClient(
            timeout=httpx.Timeout(timeout_sec),
            headers={"Authorization": f"Bearer {api_key}", "User-Agent": "tg-mt5-copytrader/1.1"},
        )

    async def aclose(self) -> None:
        await self._client.aclose()

    async def classify(
        self,
        raw_text: str,
        *,
        deterministic: ParsedSignal | None,
        recent_instrument: str | None,
        instruments: dict[str, list[str]],
    ) -> AIDecision:
        started = time.perf_counter()
        sid_seed = f"{self.model}|copytrader_classify_v2"
        session_id = "ct-" + hashlib.sha256(sid_seed.encode()).hexdigest()[:24]
        payload = {
            "model": self.model,
            "messages": [
                {"role": "system", "content": _SYSTEM_PROMPT},
                {
                    "role": "user",
                    "content": json.dumps(
                        {
                            "raw_text": raw_text,
                            "instruments": instruments,
                            "recent_instrument": recent_instrument,
                            "deterministic_parse": deterministic.to_dict() if deterministic else None,
                        },
                        ensure_ascii=False,
                        separators=(",", ":"),
                    ),
                },
            ],
            "response_format": {
                "type": "json_schema",
                "json_schema": {"name": "copytrader_classification", "strict": True, "schema": _SCHEMA},
            },
            "max_tokens": 800,
            "temperature": 0,
        }
        if self.reasoning_effort not in {"", "none", "off"}:
            payload["reasoning_effort"] = self.reasoning_effort
        response = await self._client.post(
            f"{self.base_url}/chat/completions",
            json=payload,
            headers={"x-opencode-session": session_id},
        )
        response.raise_for_status()
        body = response.json()
        data = json.loads(body["choices"][0]["message"]["content"])

        message_type = str(data["message_type"]).upper()
        if message_type not in {"OPEN", "CLOSE", "STOP_LOSS", "IGNORE"}:
            raise ValueError(f"invalid message_type {message_type!r}")
        direction = data.get("direction")
        direction = str(direction).upper() if direction else None
        if direction not in {None, "LONG", "SHORT"}:
            raise ValueError(f"invalid direction {direction!r}")
        instrument = data.get("instrument")
        return AIDecision(
            message_type=message_type,
            instrument=str(instrument).strip().upper() if instrument else None,
            direction=direction,
            close_fraction=float(data["close_fraction"]) if data.get("close_fraction") is not None else None,
            stop_loss=float(data["stop_loss"]) if data.get("stop_loss") is not None else None,
            breakeven=bool(data.get("breakeven")),
            reason=str(data.get("reason") or "")[:160],
            model=self.model,
            latency_ms=int((time.perf_counter() - started) * 1000),
        )
