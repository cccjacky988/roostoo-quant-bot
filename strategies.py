import time, requests
import numpy as np, pandas as pd

BINANCE_URLS = ["https://data-api.binance.vision/api/v3/klines",
                "https://api.binance.com/api/v3/klines"]
ROOSTOO = "https://mock-api.roostoo.com"

STABLES = {"USDT", "USDC", "FDUSD", "TUSD", "DAI", "USDE", "PYUSD", "USD1", "USDD", "BUSD"}
MAX_SPREAD = 0.0015
MIN_ROOSTOO_VOL_USD = 5_000_000
MIN_DAILY_VOL_USD = 5_000_000
VR_LOW = 0.8                      # vol ratio < VR_LOW => low-vol regime (used only if gate=True)
IV_HOURS = {"1h": 1, "4h": 4}


# ---------------- Data ----------------
def fetch_klines(coin, bars=1000):
    """Closed 1h bars from Binance (oldest first). None if unavailable."""
    out, end = [], None
    while len(out) < bars:
        lim = min(1000, bars - len(out))
        p = {"symbol": f"{coin}USDT", "interval": "1h", "limit": lim}
        if end:
            p["endTime"] = end
        data = None
        for u in BINANCE_URLS:
            try:
                r = requests.get(u, params=p, timeout=10)
                r.raise_for_status()
                data = r.json()
                break
            except Exception:
                continue
        if not data:
            break
        out = data + out
        end = data[0][0] - 1
        if len(data) < lim:
            break
    if len(out) < 500:
        return None
    df = pd.DataFrame(out).iloc[:, [0, 1, 2, 3, 4, 7]]
    df.columns = ["ot", "o", "h", "l", "c", "qv"]
    for c in ["o", "h", "l", "c", "qv"]:
        df[c] = df[c].astype(float)
    df["ot"] = pd.to_datetime(df["ot"], unit="ms")
    return df.iloc[:-1].reset_index(drop=True)      # drop the still-forming bar


def resample(df, iv):
    k = IV_HOURS[iv]
    if k == 1:
        return df
    g = df.set_index("ot").resample(f"{k}h").agg(
        o=("o", "first"), h=("h", "max"), l=("l", "min"),
        c=("c", "last"), qv=("qv", "sum"), n=("c", "count"))
    g = g[g.n == k].drop(columns="n")
    return g.reset_index()


# ---------------- Screener ----------------
def roostoo_candidates():
    """Tradable, non-stable, tight-spread, liquid /USD pairs on Roostoo (public endpoints)."""
    ei = requests.get(f"{ROOSTOO}/v3/exchangeInfo", timeout=10).json()["TradePairs"]
    tk = requests.get(f"{ROOSTOO}/v3/ticker", params={"timestamp": int(time.time() * 1000)},
                      timeout=10).json()["Data"]
    out = []
    for pair, info in ei.items():
        coin = pair.split("/")[0]
        if not pair.endswith("/USD") or coin in STABLES or pair not in tk:
            continue
        if not info.get("CanTrade", True):
            continue
        t = tk[pair]
        bid, ask = t.get("MaxBid", 0), t.get("MinAsk", 0)
        if bid <= 0 or ask <= 0:
            continue
        spread = (ask - bid) / ((ask + bid) / 2)
        if spread > MAX_SPREAD or t.get("UnitTradeValue", 0) < MIN_ROOSTOO_VOL_USD:
            continue
        out.append(coin)
    return out


def liquid(df1h):
    return df1h.qv.tail(24 * 30).sum() / 30 >= MIN_DAILY_VOL_USD


# ---------------- Indicators ----------------
def rsi(c, n=14):
    d = pd.Series(c).diff()
    up = d.clip(lower=0).ewm(alpha=1 / n, adjust=False).mean()
    dn = (-d.clip(upper=0)).ewm(alpha=1 / n, adjust=False).mean()
    return (100 - 100 / (1 + up / dn)).values


def vol_ratio(c, bpd):
    r = pd.Series(np.log(c)).diff()
    return (r.rolling(3 * bpd).std() / r.rolling(30 * bpd).std()).values


# ---------------- VCP signal ----------------
def _add_pivot(piv, kind, j, price):
    if piv and piv[-1][0] == kind:
        better = price > piv[-1][2] if kind == "H" else price < piv[-1][2]
        if better:
            piv[-1] = (kind, j, price)
            return True
        return False
    piv.append((kind, j, price))
    return True


def _find_setup(piv, min_c):
    m = min_c + 1                               # drops needed (min_c shrinking steps)
    if len(piv) < 2 * m or piv[-1][0] != "L":
        return None
    pairs = [(piv[-2 * m + 2 * k][2], piv[-2 * m + 2 * k + 1][2]) for k in range(m)]
    depths = [(hh - ll) / hh for hh, ll in pairs]
    if all(depths[k + 1] < depths[k] for k in range(m - 1)):
        return pairs[-1][0], pairs[-1][1]       # trigger = last pivot high, stop = last pivot low
    return None


def vcp_signals(df, bpd, L, min_c, gate=False, max_risk=0.10, max_wait=60):
    """{bar_index: (entry, stop)}. Pivots are confirmed L bars late (no look-ahead).
    Entry: close above the last pivot high after >= min_c shrinking pullbacks.
    Stop: last pivot low. If gate=True, only trade when the previous bar's vol ratio < VR_LOW."""
    h, l, c = df.h.values, df.l.values, df.c.values
    vr = vol_ratio(c, bpd)
    start = 30 * bpd + 1
    piv, armed, sig = [], None, {}
    for i in range(2 * L + 1, len(c)):
        j = i - L
        is_h = h[j] == h[j - L:j + L + 1].max()
        is_l = l[j] == l[j - L:j + L + 1].min()
        if is_h != is_l:
            if _add_pivot(piv, "H" if is_h else "L", j, h[j] if is_h else l[j]):
                s = _find_setup(piv, min_c)
                armed = (s[0], s[1], i + max_wait) if s else None
        if i < start or not armed:
            continue
        trig, stp, exp = armed
        if c[i] < stp or i > exp:
            armed = None
        elif c[i] > trig:
            if ((not gate) or vr[i - 1] < VR_LOW) and (c[i] - stp) / c[i] <= max_risk:
                sig[i] = (c[i], stp)
            armed = None
    return sig