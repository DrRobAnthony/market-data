"""Nightly market-data fetcher.

Runs inside GitHub Actions. Downloads full daily OHLCV history for every
symbol in watchlist.json and writes one CSV per symbol into data/.

Sources (no API keys needed):
  - Stocks: Stooq (https://stooq.com) daily CSV download
  - Crypto: CryptoCompare histoday endpoint (full history)

Output CSV columns: date,open,high,low,close,volume  (oldest first)
"""
import csv
import io
import json
import sys
import time
import urllib.request
from pathlib import Path

UA = {"User-Agent": "Mozilla/5.0 (data pipeline; personal use)"}
DATA = Path("data")
DATA.mkdir(exist_ok=True)


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


def validate(rows):
    """Basic sanity checks on parsed rows [(date, o, h, l, c, v), ...]."""
    if len(rows) < 100:
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


def fetch_stock(symbol):
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
    write_csv(symbol, validate(rows))


def fetch_crypto(symbol):
    url = ("https://min-api.cryptocompare.com/data/v2/histoday"
           f"?fsym={symbol}&tsym=USD&allData=true")
    js = json.loads(http_get(url))
    if js.get("Response") != "Success":
        raise RuntimeError(f"cryptocompare error: {js.get('Message')}")
    rows = []
    for d in js["Data"]["Data"]:
        if d["close"] <= 0:
            continue  # pre-listing zero rows
        date = time.strftime("%Y-%m-%d", time.gmtime(d["time"]))
        rows.append((date, d["open"], d["high"], d["low"], d["close"],
                     d["volumeto"]))
    write_csv(symbol, validate(rows))


def main():
    wl = json.loads(Path("watchlist.json").read_text())
    failures = []
    total = 0
    for sym in wl.get("stocks", []):
        total += 1
        print(f"[stock] {sym}")
        try:
            fetch_stock(sym)
        except Exception as e:
            print(f"  FAILED: {e}")
            failures.append(sym)
        time.sleep(1.5)
    for sym in wl.get("cryptos", []):
        total += 1
        print(f"[crypto] {sym}")
        try:
            fetch_crypto(sym)
        except Exception as e:
            print(f"  FAILED: {e}")
            failures.append(sym)
        time.sleep(1.5)

    status = {"updated_utc": time.strftime("%Y-%m-%d %H:%M:%S", time.gmtime()),
              "ok": total - len(failures), "failed": failures}
    Path("data/status.json").write_text(json.dumps(status, indent=2))
    print(json.dumps(status))
    if len(failures) > total * 0.3:
        sys.exit(1)  # mostly broken -> fail the run visibly


if __name__ == "__main__":
    main()
