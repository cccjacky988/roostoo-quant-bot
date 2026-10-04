import heapq, itertools, os, pickle, time
import numpy as np, pandas as pd
from strategies import fetch_klines, resample, roostoo_candidates, liquid, rsi, vcp_signals

BARS_1H = 6000
FEE = 0.001                       # per side, market order
POS_PCT, MAX_POS, START_EQ = 0.10, 5, 100_000
BPD = {"1h": 24, "4h": 6}
CACHE_FILE, CACHE_HOURS = "hist_cache.pkl", 6


def load_cache():
    if os.path.exists(CACHE_FILE) and time.time() - os.path.getmtime(CACHE_FILE) < CACHE_HOURS * 3600:
        return pickle.load(open(CACHE_FILE, "rb"))
    return {}


cache = load_cache()


def hist(coin):
    if coin not in cache:
        d = fetch_klines(coin, BARS_1H)
        cache[coin] = d if d is not None and len(d) >= 1500 else None
        time.sleep(0.05)
    return cache[coin]


def run_vcp(df, bpd, L, min_c, gate):
    sig = vcp_signals(df, bpd, L, min_c, gate)
    o, h, l, c = (df[x].values for x in "ohlc")
    r = rsi(c)
    t = df.ot.tolist()
    trades, pos = [], None
    for i in range(len(c)):
        if pos is not None:
            e, stop, ei = pos
            if l[i] <= stop:
                trades.append((t[ei], t[i], min(o[i], stop) / e - 1)); pos = None
            elif r[i - 1] > 70 and r[i] < 70:
                trades.append((t[ei], t[i], c[i] / e - 1)); pos = None
            continue
        if i in sig:
            pos = (sig[i][0], sig[i][1], i)
    return trades


def portfolio(trades):
    tr = sorted(trades, key=lambda x: x[1])
    if not tr:
        return None
    eq, heap, curve, nets = START_EQ, [], [(tr[0][1], START_EQ)], []
    for n_, (coin, ti, to, ret) in enumerate(tr):
        while heap and heap[0][0] <= ti:
            tt, pnl, _ = heapq.heappop(heap)
            eq += pnl
            curve.append((tt, eq))
        if len(heap) >= MAX_POS:
            continue
        net = ret - 2 * FEE
        nets.append(net)
        heapq.heappush(heap, (to, POS_PCT * eq * net, n_))
    while heap:
        tt, pnl, _ = heapq.heappop(heap)
        eq += pnl
        curve.append((tt, eq))
    s = pd.Series([v for _, v in curve], index=pd.DatetimeIndex([t for t, _ in curve]))
    s = s.sort_index().groupby(level=0).last()
    daily = s.resample("1D").last().ffill()
    rr = daily.pct_change().dropna()
    days = max((daily.index[-1] - daily.index[0]).days, 1)
    ann = (eq / START_EQ) ** (365 / days) - 1
    mdd = (daily / daily.cummax() - 1).min()
    sharpe = rr.mean() / rr.std() * np.sqrt(365) if rr.std() > 0 else np.nan
    dn = rr[rr < 0].std()
    sortino = rr.mean() / dn * np.sqrt(365) if dn and dn > 0 else np.nan
    calmar = ann / abs(mdd) if mdd < 0 else np.nan
    score = (0.4 * np.nan_to_num(sortino) + 0.3 * np.nan_to_num(sharpe) + 0.3 * np.nan_to_num(calmar))
    return {"trades": len(nets), "per_day": round(len(nets) / days, 2),
            "win%": round(float(np.mean(np.array(nets) > 0)) * 100, 1),
            "avg%": round(float(np.mean(nets)) * 100, 3),
            "total%": round((eq / START_EQ - 1) * 100, 2), "maxDD%": round(mdd * 100, 2),
            "sharpe": round(sharpe, 2), "sortino": round(sortino, 2),
            "calmar": round(calmar, 2), "score": round(score, 2)}


if __name__ == "__main__":
    cands = roostoo_candidates()
    print(f"{len(cands)} coins passed the Roostoo tradable/spread/volume screen")
    base = {}
    for c in cands:
        d = hist(c)
        if d is not None and liquid(d):
            base[c] = d
    pickle.dump(cache, open(CACHE_FILE, "wb"))
    print(f"{len(base)} coins also pass Binance history/volume:", list(base))

    frames = {(c, iv): resample(d, iv) for c, d in base.items() for iv in BPD}
    rows = []
    for iv, L, mc, gate in itertools.product(["1h", "4h"], [3, 6, 12], [1, 2], [False, True]):
        allt = [(c, *x) for c in base for x in run_vcp(frames[(c, iv)], BPD[iv], L, mc, gate)]
        m = portfolio(allt)
        if m:
            rows.append({"iv": iv, "L": L, "min_c": mc, "gate": gate, **m})

    res = pd.DataFrame(rows).sort_values("score", ascending=False)
    res.to_csv("flow_results.csv", index=False)
    print(res.to_string(index=False))