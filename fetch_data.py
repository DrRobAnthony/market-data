"""Nightly market-data fetcher + signal scanner (v3).

Runs inside GitHub Actions. Steps:
  1. Download full daily OHLCV history for every symbol in watchlist.json
     (stocks: Yahoo/yfinance primary, Stooq backup; crypto: CryptoCompare
     primary, Kraken backup) -> data/<SYMBOL>.csv
  2. Run the validated trend-breakout scanner on each symbol's latest bar
     -> data/signals.json (entry / stop-loss / take-profit / thesis /
     Monte Carlo 30-day price range)

Strategy: daily-bar Donchian breakout with EMA50/200 trend filter,
ATR(14)-based levels (SL 3.0x, TP 6.0x). Out-of-sample validated.
"""
import csv
import io
import json
import random
import subprocess
import sys
import time
import urllib.request
from pathlib import Path

try:
    import yfinance as yf
except ImportError:
    subprocess.run([sys.executable, "-m", "pip", "install", "-q", "yfinance"],
                   check=False)
    try:
        import yfinance as yf
    except ImportError:
        yf = None

UA = {"User-Agent": "Mozilla/5.0 (data pipeline; personal use)"}
DATA = Path("data")
DATA.mkdir(exist_ok=True)

KRAKEN_PAIRS = {"BTC": "XBTUSD", "ETH": "ETHUSD", "SOL": "SOLUSD",
                "XRP": "XRPUSD", "ADA": "ADAUSD", "DOGE": "DOGEUSD",
                "LINK": "LINKUSD"}

PARAMS = {"ema_fast": 50, "ema_slow": 200, "atr_n": 14, "breakout_n": 55,
          "sl_atr": 3.0, "tp_atr": 7.5, "rsi_max_long": 80,
          "rsi_min_short": 999}


# ---------------- data fetching ----------------

def http_get(url, timeout=60, retries=3):
    last = None
    for attempt in range(retries):
        try:
            req = urllib.request.Request(url, headers=UA)
            with urllib.request.urlopen(req, timeout=timeout) as r:
                return r.read()
        except Exception as e:
            last = e
            time.sleep(3 * (attempt + 1))
    raise RuntimeError(f"GET failed after {retries} tries: {url} ({last})")


def validate(rows, min_rows=100):
    if len(rows) < min_rows:
        raise ValueError(f"too few rows: {len(rows)}")
    for d, o, h, l, c, v in rows[-500:]:
        if not (h >= l and h >= c >= 0 and h >= o >= 0 and c > 0):
            raise ValueError(f"bad OHLC on {d}: o={o} h={h} l={l} c={c}")
    dates = [r[0] for r in rows]
    if dates != sorted(dates):
        raise ValueError("dates not ascending")
    return rows


def write_csv(symbol, rows):
    path = DATA / f"{symbol}.csv"
    with path.open("w", newline="") as f:
        w = csv.writer(f)
        w.writerow(["date", "open", "high", "low", "close", "volume"])
        w.writerows(rows)
    print(f"  wrote {path} ({len(rows)} rows, last: {rows[-1][0]})")


def stock_yfinance(symbol):
    if yf is None:
        raise RuntimeError("yfinance unavailable")
    hist = yf.Ticker(symbol).history(period="max", auto_adjust=True)
    rows = []
    for idx, rec in hist.iterrows():
        rows.append((idx.strftime("%Y-%m-%d"), round(float(rec["Open"]), 4),
                     round(float(rec["High"]), 4), round(float(rec["Low"]), 4),
                     round(float(rec["Close"]), 4), float(rec["Volume"])))
    return rows


def stock_stooq(symbol):
    url = f"https://stooq.com/q/d/l/?s={symbol.lower()}.us&i=d"
    raw = http_get(url).decode("utf-8", "replace")
    rows = []
    for rec in csv.DictReader(io.StringIO(raw)):
        try:
            rows.append((rec["Date"], float(rec["Open"]), float(rec["High"]),
                         float(rec["Low"]), float(rec["Close"]),
                         float(rec.get("Volume") or 0)))
        except (KeyError, ValueError, TypeError):
            continue
    return rows


def crypto_cryptocompare(symbol):
    url = ("https://min-api.cryptocompare.com/data/v2/histoday"
           f"?fsym={symbol}&tsym=USD&allData=true")
    js = json.loads(http_get(url))
    if js.get("Response") != "Success":
        raise RuntimeError(f"cryptocompare error: {js.get('Message')}")
    rows = []
    for d in js["Data"]["Data"]:
        if d["close"] <= 0:
            continue
        date = time.strftime("%Y-%m-%d", time.gmtime(d["time"]))
        rows.append((date, d["open"], d["high"], d["low"], d["close"],
                     d["volumeto"]))
    return rows


def crypto_kraken(symbol):
    pair = KRAKEN_PAIRS.get(symbol, symbol + "USD")
    url = f"https://api.kraken.com/0/public/OHLC?pair={pair}&interval=1440"
    js = json.loads(http_get(url))
    if js.get("error"):
        raise RuntimeError(f"kraken error: {js['error']}")
    key = [k for k in js["result"] if k != "last"][0]
    rows = []
    for c in js["result"][key]:
        date = time.strftime("%Y-%m-%d", time.gmtime(int(c[0])))
        rows.append((date, float(c[1]), float(c[2]), float(c[3]),
                     float(c[4]), float(c[6])))
    return rows


def try_sources(symbol, sources, min_rows=100):
    errors = []
    for name, fn in sources:
        try:
            rows = validate(fn(symbol), min_rows)
            print(f"  source: {name}")
            write_csv(symbol, rows)
            return True
        except Exception as e:
            errors.append(f"{name}: {e}")
    print(f"  FAILED all sources: {' | '.join(errors)}")
    return False


# ---------------- signal scanner (pure python, validated) ----------------

def ema_last(vals, span):
    k = 2 / (span + 1)
    e = vals[0]
    for v in vals[1:]:
        e = v * k + e * (1 - k)
    return e


def scan_symbol(rows, p=PARAMS):
    """rows: [(date,o,h,l,c,v), ...] oldest first. Signal on last bar or None."""
    o = [r[1] for r in rows]; h = [r[2] for r in rows]
    l = [r[3] for r in rows]; c = [r[4] for r in rows]
    n = len(c)
    if n < p["ema_slow"] + p["breakout_n"] + 5:
        return None
    ef = ema_last(c, p["ema_fast"])
    es = ema_last(c, p["ema_slow"])
    trs = [h[0] - l[0]]
    for i in range(1, n):
        trs.append(max(h[i] - l[i], abs(h[i] - c[i - 1]),
                       abs(l[i] - c[i - 1])))
    a = trs[0]
    alpha = 1 / p["atr_n"]
    for t in trs[1:]:
        a = t * alpha + a * (1 - alpha)
    ups = [max(c[i] - c[i - 1], 0) for i in range(1, n)]
    dns = [max(c[i - 1] - c[i], 0) for i in range(1, n)]
    au, ad = ups[0], dns[0]
    al = 1 / 14
    for u, d in zip(ups[1:], dns[1:]):
        au = u * al + au * (1 - al)
        ad = d * al + ad * (1 - al)
    rsi = 100 - 100 / (1 + au / ad) if ad > 0 else 100.0
    don_hi = max(h[-p["breakout_n"] - 1:-1])
    don_lo = min(l[-p["breakout_n"] - 1:-1])
    px = c[-1]
    sig = None
    if px > es and ef > es and px > don_hi and rsi < p["rsi_max_long"]:
        sig = {"side": "long", "entry": px, "sl": px - p["sl_atr"] * a,
               "tp": px + p["tp_atr"] * a,
               "thesis": (f"Confirmed uptrend (price above 200-day trend line, "
                          f"50-day above 200-day) and today closed above the "
                          f"prior {p['breakout_n']}-day high — a momentum "
                          f"breakout in trend direction.")}
    elif px < es and ef < es and px < don_lo and rsi > p["rsi_min_short"]:
        sig = {"side": "short", "entry": px, "sl": px + p["sl_atr"] * a,
               "tp": px - p["tp_atr"] * a,
               "thesis": (f"Confirmed downtrend (price below 200-day trend "
                          f"line, 50-day below 200-day) and today closed below "
                          f"the prior {p['breakout_n']}-day low — a momentum "
                          f"breakdown in trend direction.")}
    if sig is None:
        return None
    # Monte Carlo 30-day range from last 500 daily returns
    rets = [c[i] / c[i - 1] - 1 for i in range(max(1, n - 500), n)
            if c[i - 1] > 0]
    rng = random.Random(rows[-1][0])  # seeded by date -> reproducible
    finals = []
    for _ in range(2000):
        v = px
        for _ in range(30):
            v *= (1 + rng.choice(rets))
        finals.append(v)
    finals.sort()
    sig.update({
        "date": rows[-1][0],
        "atr": round(a, 6), "rsi": round(rsi, 1),
        "risk_pct": round(abs(sig["entry"] - sig["sl"]) / sig["entry"] * 100, 2),
        "reward_pct": round(abs(sig["tp"] - sig["entry"]) / sig["entry"] * 100, 2),
        "mc30_p5": round(finals[int(0.05 * len(finals))], 4),
        "mc30_p50": round(finals[len(finals) // 2], 4),
        "mc30_p95": round(finals[int(0.95 * len(finals))], 4),
    })
    sig["entry"] = round(sig["entry"], 6)
    sig["sl"] = round(sig["sl"], 6)
    sig["tp"] = round(sig["tp"], 6)
    return sig


# ---------------- main ----------------

def main():
    wl = json.loads(Path("watchlist.json").read_text())
    failures = []
    signals = []
    for group, syms, sources, min_rows in [
        ("stock", wl.get("stocks", []),
         [("yfinance", stock_yfinance), ("stooq", stock_stooq)], 100),
        ("crypto", wl.get("cryptos", []),
         [("cryptocompare", crypto_cryptocompare),
          ("kraken", crypto_kraken)], 300),
    ]:
        for sym in syms:
            print(f"[{group}] {sym}")
            if not try_sources(sym, sources, min_rows):
                failures.append(sym)
            else:
                rows = []
                with (DATA / f"{sym}.csv").open() as f:
                    for rec in csv.DictReader(f):
                        rows.append((rec["date"], float(rec["open"]),
                                     float(rec["high"]), float(rec["low"]),
                                     float(rec["close"]),
                                     float(rec["volume"])))
                sig = scan_symbol(rows)
                if sig:
                    sig["symbol"] = sym
                    sig["asset_class"] = group
                    signals.append(sig)
                    print(f"  *** SIGNAL: {sig['side'].upper()} "
                          f"entry={sig['entry']} sl={sig['sl']} tp={sig['tp']}")
            time.sleep(2)

    total = len(wl.get("stocks", [])) + len(wl.get("cryptos", []))
    now = time.strftime("%Y-%m-%d %H:%M:%S", time.gmtime())
    Path("data/status.json").write_text(json.dumps(
        {"updated_utc": now, "ok": total - len(failures), "total": total,
         "failed": failures, "signals_found": len(signals)}, indent=2))
    Path("data/signals.json").write_text(json.dumps(
        {"scanned_utc": now, "strategy": "daily trend-breakout v1",
         "signals": signals}, indent=2))
    print(json.dumps({"failures": failures, "signals": len(signals)}))


if __name__ == "__main__":
    main()
