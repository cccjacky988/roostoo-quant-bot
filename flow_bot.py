import sys, json, math, os, time, csv
from datetime import datetime, timezone
from roostoo_client import (log, exchange_info, ticker, balance, place_order,
                            get_wallet, equity_usd)
from strategies import (fetch_klines, resample, roostoo_candidates, liquid,
                        rsi, vcp_signals, IV_HOURS)

DRY = not (len(sys.argv) > 1 and sys.argv[1] == "live")

# ---- Settings (from flow_backtest.py results) ----
VCP_CFG = dict(iv="4h", L=6, min_c=1, gate=False)
POS_PCT, MAX_POS = 0.10, 5          # 10% of equity per position, max 5 positions
# --------------------------------------------------
LOOP_SECONDS = 60
UNIVERSE_REFRESH = 6 * 3600
STATE_FILE = "flow_state.json"
BPD = 24 // IV_HOURS[VCP_CFG["iv"]]


def load_state():
    return json.load(open(STATE_FILE)) if os.path.exists(STATE_FILE) else {}


def save_state(s):
    json.dump(s, open(STATE_FILE, "w"), indent=2)


def log_trade(coin, side, qty, px, why, res):
    with open("flow_trades.csv", "a", newline="") as f:
        csv.writer(f).writerow([datetime.now(timezone.utc).isoformat(), coin, side, qty, px,
                                why, res.get("Success"), res.get("ErrMsg", "")])


def floor_qty(x, prec):
    m = 10 ** prec
    return math.floor(x * m) / m


def fill_price(res, fallback):
    d = res.get("OrderDetail", {}) or {}
    return float(d.get("FilledAverPrice") or d.get("Price") or fallback)


def refresh_universe():
    uni = []
    for c in roostoo_candidates():
        d = fetch_klines(c, 1000)
        time.sleep(0.05)
        if d is not None and len(d) >= 800 and liquid(d):
            uni.append(c)
    log.info("universe (%d): %s", len(uni), uni)
    return uni


def sell(coin, reason, px, ctx):
    state, ei, wallet = ctx["state"], ctx["ei"], ctx["wallet"]
    pos, pair = state[coin], f"{coin}/USD"
    info = ei[pair]
    held = pos["qty"] if DRY else wallet.get(coin, {}).get("Free", 0)
    qty = floor_qty(held, info["AmountPrecision"])
    if qty <= 0 or qty * px <= info["MiniOrder"]:
        log.info("%s: nothing sellable, dropping position", coin)
        state.pop(coin); return
    res = {"Success": True} if DRY else place_order(pair, "SELL", qty)
    log.info("%sSELL %s %s @ %s (%s) -> %s %s", "[DRY] " if DRY else "", qty, pair, px,
             reason, res.get("Success"), res.get("ErrMsg", ""))
    log_trade(coin, "SELL", qty, px, reason, res)
    if res.get("Success"):
        log.info("%s closed, approx PnL %.2f%%", coin, (px / pos["entry"] - 1) * 100)
        state.pop(coin)


def buy(coin, stop, px, ctx):
    state, ei = ctx["state"], ctx["ei"]
    pair = f"{coin}/USD"
    info = ei[pair]
    if px <= stop:
        return
    qty = floor_qty(POS_PCT * ctx["equity"] / px, info["AmountPrecision"])
    if qty <= 0 or qty * px <= info["MiniOrder"] or qty * px * 1.003 > ctx["usd"]:
        log.info("%s: order too small or insufficient USD", coin); return
    res = {"Success": True} if DRY else place_order(pair, "BUY", qty)
    log.info("%sBUY %s %s @ %s stop=%.6g -> %s %s", "[DRY] " if DRY else "", qty, pair, px,
             stop, res.get("Success"), res.get("ErrMsg", ""))
    log_trade(coin, "BUY", qty, px, "VCP", res)
    if res.get("Success"):
        ctx["usd"] -= qty * px
        state[coin] = {"entry": px if DRY else fill_price(res, px), "stop": float(stop),
                       "qty": qty, "time": datetime.now(timezone.utc).isoformat()}


def scan(universe, ctx, allow_entries):
    """Runs once per newly closed bar: RSI exits for open positions, VCP entries for the rest."""
    state, prices, iv = ctx["state"], ctx["prices"], VCP_CFG["iv"]
    for coin in sorted(set(universe) | set(state)):
        px = prices.get(f"{coin}/USD")
        raw = fetch_klines(coin, 1000)
        time.sleep(0.05)
        if raw is None or px is None or f"{coin}/USD" not in ctx["ei"]:
            continue
        df = resample(raw, iv)
        if len(df) < 30 * BPD + 30:
            continue
        if coin in state:
            r = rsi(df.c.values)
            if r[-2] > 70 and r[-1] < 70:
                sell(coin, "RSI_EXIT", px, ctx)
            continue
        if not allow_entries or len(state) >= MAX_POS:
            continue
        sig = vcp_signals(df, BPD, VCP_CFG["L"], VCP_CFG["min_c"], VCP_CFG["gate"])
        if len(df) - 1 in sig:
            buy(coin, sig[len(df) - 1][1], px, ctx)


def main():
    log.info("Starting VCP bot | mode=%s | cfg=%s", "DRY" if DRY else "LIVE", VCP_CFG)
    ei = exchange_info()["TradePairs"]
    state, universe, last_uni, last_bucket = load_state(), [], 0, None
    hrs = IV_HOURS[VCP_CFG["iv"]]

    while True:
        t0 = time.time()
        tk, bal = ticker(), balance()
        if not tk.get("Success") or not bal.get("Success"):
            log.error("ticker/balance failed"); time.sleep(LOOP_SECONDS); continue
        prices = {p: v["LastPrice"] for p, v in tk["Data"].items()}
        wallet = get_wallet(bal)

        for coin in list(state):
            held = wallet.get(coin, {}).get("Free", 0) + wallet.get(coin, {}).get("Lock", 0)
            if held <= 0 and not DRY:
                log.info("%s not in wallet, dropping from state", coin); state.pop(coin)

        ctx = {"state": state, "ei": ei, "wallet": wallet, "prices": prices,
               "equity": equity_usd(wallet, prices),
               "usd": wallet.get("USD", {}).get("Free", 0)}

        if time.time() - last_uni > UNIVERSE_REFRESH:
            try:
                ei = exchange_info()["TradePairs"]; ctx["ei"] = ei
                new = refresh_universe()
                if new:
                    universe = new
                last_uni = time.time()
            except Exception as e:
                log.error("universe refresh failed: %s", e)
                last_uni = time.time() - UNIVERSE_REFRESH + 600

        for coin in list(state):                       # stop losses, checked every loop
            px = prices.get(f"{coin}/USD")
            if px is not None and px <= state[coin]["stop"]:
                sell(coin, "STOP", px, ctx)

        bucket = int((time.time() - 45) // (3600 * hrs))
        if bucket != last_bucket:                      # new closed bar
            scan(universe, ctx, allow_entries=last_bucket is not None)
            last_bucket = bucket

        save_state(state)
        log.info("equity=%.2f | open: %s", ctx["equity"],
                 {c: round(p["entry"], 4) for c, p in state.items()})
        time.sleep(max(0, LOOP_SECONDS - (time.time() - t0)))


if __name__ == "__main__":
    main()