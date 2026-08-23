"""Nightly market-data fetcher (v2).

Runs inside GitHub Actions. Downloads full daily OHLCV history for every
symbol in watchlist.json and writes one CSV per symbol into data/.

Sources:
  - Stocks: Yahoo Finance via yfinance (primary), Stooq (backup)
  - Crypto: CryptoCompare full history (primary), Kraken ~2yrs (backup)

Output CSV columns: date,open,high,low,close,volume  (oldest first)
"""
import csv
import io
import json
import subprocess
import sys
import time
import urllib.request
from pathlib import Path

# Self-install yfinance so the workflow file needs no changes.
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


def main():
    wl = json.loads(Path("watchlist.json").read_text())
    failures = []
    for sym in wl.get("stocks", []):
        print(f"[stock] {sym}")
        if not try_sources(sym, [("yfinance", stock_yfinance),
                                 ("stooq", stock_stooq)]):
            failures.append(sym)
        time.sleep(2)
    for sym in wl.get("cryptos", []):
        print(f"[crypto] {sym}")
        if not try_sources(sym, [("cryptocompare", crypto_cryptocompare),
                                 ("kraken", crypto_kraken)], min_rows=300):
            failures.append(sym)
        time.sleep(2)

    total = len(wl.get("stocks", [])) + len(wl.get("cryptos", []))
    status = {"updated_utc": time.strftime("%Y-%m-%d %H:%M:%S", time.gmtime()),
              "ok": total - len(failures), "total": total, "failed": failures}
    Path("data/status.json").write_text(json.dumps(status, indent=2))
    print(json.dumps(status))
    # Always exit 0 so whatever data DID succeed gets committed.


if __name__ == "__main__":
    main()
