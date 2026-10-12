# MT5 repository concept map

This package is an independent Telegram copy-trading service. It does not import the user's PO3/MT5 repository, but it deliberately reuses several architectural ideas from it rather than copying strategy code.

| Reference concept | How it is used here |
|---|---|
| `python/ai_gate.py` deterministic facts + AI advisory layer | Telegram text is first parsed by deterministic rules. AI can only approve/reject the deterministic interpretation; it cannot create or change trade facts. |
| Strict structured-output validation in `ai_provider.py` | AI confirmation uses a strict JSON schema through OpenCode Go. Invalid/malformed AI output is treated as a failed confirmation. |
| Fail-closed decision handling | AI outage/rejection blocks execution by default (`AI_FAIL_OPEN=false`). |
| `Risk.mqh` equity percentage risk | With a Stop Loss, risk money is `equity × RISK_PER_TRADE_PCT`, then converted to broker-valid volume from the one-lot loss to SL. |
| `Risk.mqh` broker volume normalization | Opening and partial-closing lots are floored to broker min/max/step. |
| Managed-exposure ownership via magic number | Close commands affect only positions/orders opened under this package's `MAGIC_NUMBER`. |
| Daily-loss stop concept | New entries are blocked after the configured intraday equity loss threshold. |
| Request lifecycle / idempotency | Telegram `(chat_id, message_id)` is stored in SQLite so reconnect/replay cannot execute the same message twice. |
| Audit trail | Parsed signals, AI decisions, failures, and broker outcomes are written to JSONL. |

The Telegram channel's signal provider remains the source of the trade idea. This package does not perform market analysis or alter the provider's direction.
