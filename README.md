# eth-desk

Paper trading terminal for Polymarket's 15-minute **Ethereum Up or Down** markets. It reads the live market rules, the Chainlink settlement stream named by each market, and the CLOB best ask. It does not send orders.

Live order entry is behind `--live` and `POLYMARKET_LIVE=1`, and even then this version only reaches a stub that records nothing on the book.

## Run paper mode

```bash
python -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
python -m eth_desk
```

One pass, then exit:

```bash
python -m eth_desk --once
```

No environment variables are required for paper mode. The process calls `https://polymarket.com/api/geoblock` before the loop. If that endpoint reports `blocked: true`, or the check errors, the process prints a refusal and exits non-zero. Nothing is ordered.

Stop a running process by creating a file named `FREEZE` in the working directory (or whatever path you passed to `--freeze`). The loop checks that path every pass and again immediately before a fill. If the file exists, the process stops and places nothing.

```bash
touch FREEZE
```

## Configuration

| Flag / variable | Default | Role |
| --- | --- | --- |
| `--once` | off | Single loop, then print the daily summary and exit. |
| `--interval` | 5 | Seconds between loops. |
| `--log` | `logs/eth-desk.jsonl` | Append-only JSONL log. |
| `--state` | `state/desk.json` | Cash, open paper positions, and the daily stop. |
| `--freeze` | `FREEZE` | Halt when this path exists. |
| `--heartbeat` | 60 | Seconds between heartbeat lines. A line is also written on the first loop. |
| `--live` | off | Arm the live branch. Does nothing unless `POLYMARKET_LIVE=1`. |
| `POLYMARKET_LIVE` | unset | Must be `1` together with `--live`. |
| `POLYMARKET_PRIVATE_KEY` | unset | Required only when live mode is armed. Never logged. |
| `POLYMARKET_API_KEY` | unset | Required only when live mode is armed. Never logged. |
| `POLYMARKET_API_SECRET` | unset | Required only when live mode is armed. Never logged. |
| `POLYMARKET_API_PASSPHRASE` | unset | Required only when live mode is armed. Never logged. |

Hard limits are constants in `eth_desk/limits.py`. Environment variables cannot raise them.

## What one loop does

1. Refuse to run unless the geoblock check returns `blocked: false`.
2. Load the ETH 15-minute series from gamma (`eth-up-or-down-15m`) and the open events on that series.
3. Read each market's rules from the payload: `resolutionSource`, `feeSchedule` (`rate`, `exponent`, `takerOnly`, `rebateRate`), `feesEnabled`, and `orderMinSize`.
4. Settle any paper position whose window has ended, using that market's Chainlink open and close. Up wins when the close print is greater than or equal to the open print.
5. Inside the last 4 minutes, price P(Up) from the window open, the live settlement-stream price, time left, and recent volatility.
6. In paper mode, buy at the best ask only, and only up to the size resting at that price. Charge the taker fee from that market's schedule.

The fair-value model is `eth_desk.pricing.ZeroDriftLognormalModel`. Replace it by passing any object with `probability_up(PricingInput) -> Decimal` into the engine. `PricingInput` carries the open price, the current settlement-stream price, seconds remaining, and volatility per square-root-second.

## Hard limits

Enforced in `decide_bet` and again in `DeskState.commit_fill`:

- Bankroll starts at **$250**. A fill cannot spend more cash than is left. Wins and losses move the cash balance; the starting bankroll constant does not change.
- **$5** maximum cash outlay per bet (share cost plus taker fee).
- **$25** maximum cash outlay per 15-minute window.
- Never buy above **$0.72**. $0.72 itself is allowed.
- Trade only while more than 0 and at most **240** seconds remain in the window.
- Stop for the rest of the UTC day once realized PnL after fees is **-$15** or worse. The latch clears at the next UTC date.
- Buy a side only when its fair probability exceeds the ask by at least **6 percentage points after the taker fee** (`fair - price - fee/shares >= 0.06`).
- Skip the quote when the size, after the book cap and the dollar caps, is below the market's `orderMinSize` (shares).

`orderMinSize` from gamma matched the CLOB book's `min_order_size` on the markets checked while building this (both `5`). Sizes on the book are share amounts, so the minimum is treated as shares. If the book reports a higher minimum than gamma, the larger one is used.

## Fees

The fee is never a built-in constant. For a taker buy of `C` shares at price `p`, with the market's `feeSchedule`:

```text
fee = C × rate × (p × (1 − p)) ^ exponent
```

Rounded half-up to 5 decimal places. Anything under 0.00001 USDC is charged as 0. When `exponent` is 1 this is the formula on Polymarket's fee page (`C × feeRate × p × (1 − p)`). `feesEnabled: false` charges 0. `rebateRate` is stored and logged with the rules, and it is not subtracted: on these markets it is the maker rebate, and this terminal takes the ask. `makerBaseFee` / `takerBaseFee` are ignored; gamma's `feeSchedule` is the schedule the fee page says to use.

## Prices

`resolutionSource` on current ETH 15-minute markets is `https://data.chain.link/streams/eth-usd-twap-60s-streams`. The window open (and the close, once the window is over) is read from Polymarket's public crypto-price endpoint with `symbol=ETH` and `variant=fifteen`. That variant is the Chainlink series: on the windows observed while building this, each window's close equalled the next window's open. The live price and the volatility sample come from Polymarket's public RTDS topic `crypto_prices_twap_sixty` for `eth/usd`, which is the 60-second Chainlink ETH/USD TWAP. A market whose resolution source is not that Chainlink ETH stream is not priced and is not traded.

## Logs

`logs/eth-desk.jsonl` (gitignored) gets one JSON object per line:

- `fill` — market id, side, price, size, fee, fair probability, edge, timestamp, mode
- `heartbeat` — about once a minute, also echoed as a single stdout line
- `daily_summary` — PnL after fees, fill count, win rate of settled paper trades, fees paid. Written at UTC day change and when the process stops
- `market_rules`, `settlement`, `skip`, `freeze`

Win rate is settled wins divided by settled trades. Fills that have not resolved yet are not in that ratio.

Fields whose names look like a key, secret, passphrase, or private key are replaced with `[REDACTED]`. Values of those environment variables are stripped out of log text as well.

## Tests

```bash
pip install -r requirements.txt
pytest
```

Network tests are not part of `pytest`. Fixtures under `tests/fixtures/` are trimmed recordings of gamma, the geoblock response, a CLOB book, a completed crypto-price window, and one RTDS TWAP tick.
