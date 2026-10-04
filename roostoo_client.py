import os, time, hmac, hashlib, csv, logging
from datetime import datetime, timezone
import requests

BASE = "https://mock-api.roostoo.com"
API_KEY = os.environ.get("ROOSTOO_API_KEY", "")
SECRET = os.environ.get("ROOSTOO_API_SECRET", "")

logging.basicConfig(level=logging.INFO,
                    format="%(asctime)s %(levelname)s %(message)s",
                    handlers=[logging.StreamHandler(), logging.FileHandler("flow_bot.log")])
log = logging.getLogger("bot")


def ts():
    return str(int(time.time() * 1000))


def _qs(params):
    return "&".join(f"{k}={params[k]}" for k in sorted(params))


def _sign(qs):
    return hmac.new(SECRET.encode(), qs.encode(), hashlib.sha256).hexdigest()


def request(method, path, params=None, signed=False, retries=3):
    if signed and (not API_KEY or not SECRET):
        raise SystemExit("Set ROOSTOO_API_KEY and ROOSTOO_API_SECRET first "
                         "(cmd: set NAME=value | PowerShell: $env:NAME=\"value\")")
    params = dict(params or {})
    headers = {}
    if signed or path == "/v3/ticker":
        params["timestamp"] = ts()
    qs = _qs(params)
    if signed:
        headers["RST-API-KEY"] = API_KEY
        headers["MSG-SIGNATURE"] = _sign(qs)
    for i in range(retries):
        try:
            if method == "GET":
                r = requests.get(f"{BASE}{path}" + (f"?{qs}" if qs else ""), headers=headers, timeout=10)
            else:
                headers["Content-Type"] = "application/x-www-form-urlencoded"
                r = requests.post(f"{BASE}{path}", data=qs, headers=headers, timeout=10)
            r.raise_for_status()
            data = r.json()
            with open("api_log.csv", "a", newline="") as f:
                csv.writer(f).writerow([datetime.now(timezone.utc).isoformat(), method, path,
                                        r.status_code, data.get("Success"), data.get("ErrMsg", "")])
            return data
        except Exception as e:
            log.warning("%s %s failed (%d/%d): %s", method, path, i + 1, retries, e)
            time.sleep(1.5 * (i + 1))
    return {"Success": False, "ErrMsg": "request failed after retries"}


def server_time():   return request("GET", "/v3/serverTime")
def exchange_info(): return request("GET", "/v3/exchangeInfo")
def ticker(pair=None): return request("GET", "/v3/ticker", {"pair": pair} if pair else None)
def balance():       return request("GET", "/v3/balance", signed=True)


def place_order(pair, side, qty, otype="MARKET", price=None):
    p = {"pair": pair, "side": side, "type": otype, "quantity": qty}
    if otype == "LIMIT":
        p["price"] = price
    return request("POST", "/v3/place_order", p, signed=True)


def get_wallet(bal):
    for k in ("SpotWallet", "Wallet"):
        if isinstance(bal.get(k), dict):
            return bal[k]
    log.error("Unknown balance format, keys: %s", list(bal.keys()))
    return {}


def equity_usd(wallet, prices):
    tot = 0.0
    for c, v in wallet.items():
        q = v.get("Free", 0) + v.get("Lock", 0)
        tot += q if c == "USD" else q * prices.get(f"{c}/USD", 0)
    return tot


if __name__ == "__main__":      # read-only check, places no orders
    print("server:", server_time())
    b = balance()
    print("balance ok:", b.get("Success"),
          {k: v for k, v in get_wallet(b).items() if v.get("Free") or v.get("Lock")})