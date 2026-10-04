# Roostoo Quant Hackathon Bot (VCP)

Flow: tradable-coin screen (Roostoo CanTrade, spread, volume + Binance history) -> VCP breakout.

- VCP: pivot highs/lows, >= min_c shrinking pullbacks, enter on close above the last pivot high,
  stop at the last pivot low, exit when RSI(14) crosses back below 70.
- Optional volatility-regime gate (off by default).

## Run
pip install -r requirements.txt
export ROOSTOO_API_KEY=... ROOSTOO_API_SECRET=...
python flow_backtest.py     # historical test
python check_vcp.py         # robustness check
python flow_bot.py dry      # logs signals, no orders
python flow_bot.py live     # sends orders to Roostoo