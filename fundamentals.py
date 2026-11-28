"""Weekly refresh of annual revenue and net income -> data/fundamentals.json

Rob's screen: only buy companies whose revenue AND net income rose in every
year of the annual statements Yahoo provides. Yahoo normally gives four fiscal
years, so "every year" means three consecutive increases, not a long record.

Status per symbol:
  pass     revenue and net income both strictly higher each year (>= 4 years)
  fail     at least one year-over-year decline or flat year in either
  no_data  Yahoo returned no usable annual statement (ETFs, funds, new listings)
  error    the fetch failed; any earlier good result is kept untouched

Limits worth knowing: Yahoo's history is restated and not point-in-time, so
this screen cannot be back-tested honestly; it is judged going forward only
(ledger.json compares eligible and ineligible signals). Crypto is skipped.
Runs from the repo root: python fundamentals.py
"""
import json
import subprocess
import sys
import time
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

OUT = Path("data/fundamentals.json")
MIN_YEARS = 4          # four annual statements = three year-over-year changes
REFRESH_DAYS = 7
MAX_PER_RUN = 70       # three nightly runs cover the whole list in one night


def grower_status(revenue, net_income):
    """Both lists oldest -> newest, same fiscal years. Returns pass/fail/no_data."""
    def clean(xs):
        return [x for x in xs if x is not None and x == x]  # drop None / NaN
    r, n = clean(revenue), clean(net_income)
    if len(r) < MIN_YEARS or len(n) < MIN_YEARS or len(r) != len(revenue) \
            or len(n) != len(net_income):
        return "no_data"
    rising = all(b > a for a, b in zip(r, r[1:])) and \
        all(b > a for a, b in zip(n, n[1:]))
    return "pass" if rising else "fail"


def read_annual(symbol):
    """-> (years, revenue, net_income) oldest first, or None if no statement."""
    stmt = yf.Ticker(symbol).income_stmt
    if stmt is None or getattr(stmt, "empty", True):
        return None
    if "Total Revenue" not in stmt.index or "Net Income" not in stmt.index:
        return None
    cols = sorted(stmt.columns)               # oldest first
    years = [str(c)[:10] for c in cols]
    rev = [float(stmt.loc["Total Revenue", c]) for c in cols]
    net = [float(stmt.loc["Net Income", c]) for c in cols]
    return years, rev, net


def main():
    wl = json.loads(Path("watchlist.json").read_text())
    stocks = list(dict.fromkeys(wl.get("stocks", [])))
    today = time.strftime("%Y-%m-%d", time.gmtime())
    cache = json.loads(OUT.read_text()) if OUT.exists() else {}

    def age_days(sym):
        e = cache.get(sym)
        if not e:
            return 10 ** 6
        return (time.mktime(time.strptime(today, "%Y-%m-%d")) -
                time.mktime(time.strptime(e["fetched"], "%Y-%m-%d"))) / 86400

    due = sorted((s for s in stocks if age_days(s) >= REFRESH_DAYS),
                 key=age_days, reverse=True)[:MAX_PER_RUN]
    if yf is None:
        print("yfinance unavailable; keeping cached fundamentals")
        due = []
    done = 0
    for sym in due:
        try:
            got = read_annual(sym)
            if got is None:
                cache[sym] = {"fetched": today, "status": "no_data"}
            else:
                years, rev, net = got
                cache[sym] = {"fetched": today, "years": years,
                              "revenue": rev, "net_income": net,
                              "status": grower_status(rev, net)}
        except Exception as e:  # keep earlier good result if there is one
            old = cache.get(sym)
            if old and old.get("status") in ("pass", "fail"):
                old["last_error"] = f"{today}: {str(e)[:120]}"
            else:
                cache[sym] = {"fetched": today, "status": "error",
                              "detail": str(e)[:160]}
        done += 1
        time.sleep(1.5)
    OUT.parent.mkdir(exist_ok=True)
    OUT.write_text(json.dumps(cache, indent=1, sort_keys=True))
    counts = {}
    for s in stocks:
        st = cache.get(s, {}).get("status", "missing")
        counts[st] = counts.get(st, 0) + 1
    print(f"fundamentals: refreshed {done}; status counts {counts}")


if __name__ == "__main__":
    main()
