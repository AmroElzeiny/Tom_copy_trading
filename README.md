# Telegram Web → MT5 Copy Trader (Python)

A Python service that opens Telegram Web in a real browser window, reads the signal chat you open one message after another, classifies each message (rules + OpenCode Go AI), and sends the resulting order to a MetaTrader 5 terminal.

No Telegram API ID/hash is needed: you log in to Telegram Web yourself, once.

## Pipeline

`Telegram Web chat (browser) → new message detected → dedupe/age gate → deterministic parser + AI classification → validation → MT5 execution → audit`

## Message types

| Message | What happens |
|---|---|
| `Close trade alert 🇺🇸 Nasdaq` | Full close of bot-managed Nasdaq positions and pending orders. The AI also accepts obvious variants (casing, emojis, typos). |
| `closed half` / `close 25%` / `closing three quarters` | Partial close. With no instrument named, it uses the **most recent instrument a trade was opened on**. |
| `Stop loss alert 🇺🇸 Nasdaq 24500` | Sets or moves the SL of open Nasdaq positions (and pending orders) to 24500. |
| `Stop loss alert 🇺🇸 Nasdaq breakeven` | Moves the SL of each open Nasdaq position to its own entry price. |
| `Buy nasdaq` / `NAS100 sell SL 24620 TP 24400` | Opens a trade. SL given → lot sized to `RISK_PER_TRADE_PCT`. No SL → per-symbol fallback lot, and only for NAS100, US30, GER40, UK100. |
| Anything else | Ignored. |

One action per message. Priority: close trade alert → stop loss alert → other close wording → breakeven/move-SL → open.

If a message names an instrument that is not mapped in `.env`, it is ignored — it never falls back to the recent instrument (so "close Nikkei" can't close Nasdaq).

## How the AI is used

1. Messages with no trade wording at all (no buy/sell/close/stop/alert/half/…) are ignored without an AI call.
2. Otherwise the deterministic parser and the AI both read the message. The AI returns the message type, instrument, direction, close fraction, SL price and breakeven flag.
3. Every AI field is checked against the text, fail-closed:
   - the instrument must be mapped, and must match the one named in the text if the parser found one;
   - an OPEN needs an actual buy/sell word in the text matching the AI's direction;
   - an SL price must literally appear in the message;
   - any parser/AI disagreement on type, instrument, fraction or price rejects the message.
4. `AI_FAIL_OPEN=false` (default): if the AI times out or errors, the message is rejected.

## Install and run (Windows, same machine as MT5)

```powershell
.\run.ps1            # creates .venv, installs, starts the bot
```

Or manually:

```powershell
python -m venv .venv
.\.venv\Scripts\Activate.ps1
pip install -e ".[dev]"
python -m tg_mt5_copytrader
```

Then:

1. A Chrome window opens on Telegram Web. Log in (QR code or phone). The login is saved in `runtime/browser_profile`.
2. Open the signal chat and leave it scrolled to the latest message. The window can be behind other windows.
3. Messages already in the chat when you open it are treated as history and never traded. Only new messages are processed, in order.

Useful commands (no trading):

```powershell
python -m tg_mt5_copytrader --probe                                   # print what the browser reader sees
python -m tg_mt5_copytrader --test-message "closed half" --recent NAS100  # show how a message is interpreted
python -m pytest -q
```

## `.env` essentials

- `OPENCODE_GO_API_KEY` — required while `AI_CONFIRMATION_ENABLED=true`.
- `MT5_TERMINAL_PATH` — the terminal that is logged in to your account.
- `TRADING_MODE` — `DRY_RUN` only checks orders; `LIVE` places them.
- `FALLBACK_LOT_<KEY>` — lot used when a signal has no SL. Only `NAS100`, `DOW` (US30), `GERMAN30` (GER40) and `UK100` are fallback-eligible; anything else with no SL is rejected. `FALLBACK_LOT_DEFAULT` is only the value for an eligible index with no explicit line.
- `MT5_SYMBOL_<KEY>` — exact broker symbol names. Blank = instrument disabled.
- `BROWSER_CHAT_TITLE` — optional; only watch a chat whose title contains this text.
- `BROWSER_INCLUDE_OUTGOING=true` — also act on messages you send yourself (for testing in Saved Messages).

Built-in aliases live in `symbol_map.py` (e.g. Nasdaq / NAS / US Tech → `NAS100`, Dow / US30 → `DOW`, DAX / GER40 / German DAX Index → `GERMAN30`, FTSE 100 / FTSE 100 Index → `UK100`). Add more with `ALIASES_<KEY>=a,b,c`.

## Risk behavior

- **SL found:** volume risks `RISK_PER_TRADE_PCT` of current equity at the SL.
- **No SL:** per-symbol fallback lot, and only for the four indices NAS100, US30, GER40, UK100. Any other instrument without an SL is rejected (`no_sl_and_symbol_not_fallback_eligible`).
- Volume is floored to broker `volume_min / volume_max / volume_step` and capped by `MAX_VOLUME_LOTS`.
- Partial close avoids leaving a residual below broker minimum.
- `DAILY_LOSS_LOCK_PCT` blocks new entries after the configured intraday equity loss.
- Close and SL changes only touch positions/orders with this bot's `MAGIC_NUMBER`; manual trades are never modified.
- SL changes that the broker would reject (e.g. breakeven while the trade is in loss, or inside the stops level) are reported as failures in the log and audit.

## Entry handling

- No entry price → market order.
- Price inside the entry range or within `ENTRY_TOLERANCE_POINTS` → market order.
- Otherwise → pending limit/stop at the nearest entry.

## Files

- `browser_listener.py`: Playwright browser session; reads Telegram Web (K and A clients) message by message.
- `parser.py`: deterministic signal language (close alert, stop loss alert, partial close, open).
- `ai_confirm.py`: OpenCode Go strict-JSON classification.
- `interpreter.py`: reconciles parser + AI and validates AI fields against the text.
- `engine.py`: age/dedupe gate, dispatch to MT5, audit, recent-instrument tracking.
- `mt5_gateway.py`: open, pending, full/partial close, SL modify / breakeven.
- `risk.py`: risk sizing + broker lot normalization.
- `state.py`: SQLite dedupe/recent instrument + JSONL audit (`runtime/audit.jsonl`).

## Operational notes

- Keep MT5 Algo Trading enabled and the terminal connected.
- Keep the Telegram Web window open and the chat scrolled to the bottom; if you scroll far up, new messages may not render until you scroll back.
- Do not share `.env`, `runtime/browser_profile` (it holds your Telegram login), or API keys.

No automated copier can guarantee profitability; signal quality, spread, slippage, execution delay, broker rules and connectivity still determine outcomes.
