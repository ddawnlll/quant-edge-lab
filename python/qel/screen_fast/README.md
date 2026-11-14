# screen_fast — vectorized screening skeleton

**Screening only. No claims.** This is not a backtest of record and not part of
the Nautilus replay path.

One hypothesis is implemented: **funding carry** — hold against the funding sign
around each settlement (short when funding is positive, long when negative),
enter at the mark close of the settlement minute, exit at the next settlement
(or the day's last mark minute). Costs are charged explicitly: 4 bps taker per
side plus half a tick of spread per side.

## Run

```sh
PYTHONPATH=python .venv/bin/python -m qel.screen_fast.funding_carry \
  data/raw/ARCHIVE_BINANCE_USDM/BTCUSDT \
  data/raw/ARCHIVE_BINANCE_USDM/BTCUSDT/funding/BTCUSDT-fundingRate-2024-09.zip \
  --days 2024-09-01 2024-09-02 2024-09-03 2024-09-04 2024-09-05 2024-09-06 2024-09-07 \
  --out /tmp/screen_fast.json
```

Mark inputs must be real `markPriceKlines` (`BTCUSDT-markPriceKlines-1m-<day>.zip`);
the loader fails closed on trade klines.

## Why numpy, not pandas/polars

Neither pandas nor polars is installed in this environment, and adding one would
be a new project dependency. The arithmetic uses numpy, which is already a
dependency.

## Output

Per day: `gross_pnl`, `funding_pnl`, `fees`, `net_pnl`, `trades`. All values are
USDT on a 1000 USDT notional, development week only.
