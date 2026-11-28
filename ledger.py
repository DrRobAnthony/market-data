"""Signal ledger: scores every signal in data/signal_log.json against the
daily bars in data/<SYMBOL>.csv and writes data/ledger.json.

Why this exists: "win rate of resolved signals" alone is biased low for a
young record, because stops sit close and resolve in days while targets take
weeks. This ledger reports average R (1R = the distance to that signal's own
stop) with still-open positions marked to market, counts each name once per
breakout ("independent"), and splits results by signal age and by the
stop/target settings each signal was issued with.

Scoring rules: signals are scored with the levels stored at emission time.
Bars after the signal date are used (legacy crypto signals generated from an
in-progress bar also use the signal date). If stop and target are both
touched on one bar, the stop is assumed first. Results are gross of costs and
assume a fill at the logged entry (signal-bar close), which is optimistic for
stocks entered at the next open.

Run from the repo root:  python ledger.py
"""
import csv
import json
import time
from collections import defaultdict
from pathlib import Path

DATA = Path("data")

import re
import sys as _sys

_sys.path.insert(0, str(Path(__file__).resolve().parent))
import indicators  # same maths the scanner uses (see indicators.py docstring)

# Reference only: replay of the scanner rules over 176 symbols, 2022 onward,
# gross of costs, on a watchlist chosen with hindsight (optimistic).
BACKTEST_REF = {
    "SL3.0xATR/TP6.0xATR": {"win_pct_of_resolved": 42.1, "profit_factor": 1.45,
                            "expectancy_R": 0.26},
    "SL3.0xATR/TP7.5xATR": {"win_pct_of_resolved": 38.0, "profit_factor": 1.53,
                            "expectancy_R": 0.33},
    "SL1.5xATR/TP3.75xATR": {"win_pct_of_resolved": 34.2, "profit_factor": 1.30,
                             "expectancy_R": 0.20},
}


def load_bars(sym):
    p = DATA / f"{sym}.csv"
    if not p.exists():
        return []
    with p.open() as f:
        return [(r["date"], float(r["open"]), float(r["high"]),
                 float(r["low"]), float(r["close"])) for r in csv.DictReader(f)]


def completed(bars, today):
    """Drop a trailing bar dated today (UTC) or later: it is still forming."""
    return bars[:-1] if bars and bars[-1][0] >= today else bars


def score(sig, bars):
    long_ = sig["side"] == "long"
    include_signal_day = sig.get("legacy_partial_bar", False)
    for d, o, h, l, c in bars:
        if d < sig["date"] or (d == sig["date"] and not include_signal_day):
            continue
        hit_sl = l <= sig["sl"] if long_ else h >= sig["sl"]
        hit_tp = h >= sig["tp"] if long_ else l <= sig["tp"]
        if hit_sl:
            return "SL", d
        if hit_tp:
            return "TP", d
    return "OPEN", None


def mean(xs):
    return sum(xs) / len(xs) if xs else None


def r2(x):
    return None if x is None else round(x, 2)


def summarize(rows):
    tp = sum(1 for x in rows if x["result"] == "TP")
    sl = sum(1 for x in rows if x["result"] == "SL")
    op = sum(1 for x in rows if x["result"] == "OPEN")
    res = tp + sl
    return {
        "n": len(rows), "tp": tp, "sl": sl, "open": op,
        "win_pct_of_resolved": round(tp / res * 100, 1) if res else None,
        "avg_R_resolved": r2(mean([x["R"] for x in rows if x["result"] != "OPEN"])),
        "avg_R_incl_open": r2(mean([x["R"] for x in rows])),
    }


def run_benchmark(log, today):
    """Would random entries have done as well as the breakout rule?

    For every settings era in the stock signal log, take the SAME stocks over
    the SAME dates and score three groups of entries with the SAME stop and
    target multiples and the SAME scoring as the live signals (TP, SL, or open
    marked to the last completed close, in R):
      breakout_rule        the scanner's own long rule, recomputed from prices
      uptrend_no_breakout  trend filter passes but no breakout
      any_day              every day, no filter (the random-entry benchmark)
    If breakout_rule is not clearly above any_day, the rule adds nothing.
    Stocks only (crypto history is too short). Gross of costs.
    """
    try:
        wl = json.loads(Path("watchlist.json").read_text())
        sj = json.loads((DATA / "signals.json").read_text())
    except Exception:
        return None
    p = dict(indicators.DEFAULT_PARAMS)
    p.update({k: v for k, v in (sj.get("params") or {}).items() if k in p})
    stock_sigs = [s for s in log if s["asset_class"] == "stock"]
    if not stock_sigs:
        return None
    eras = {}
    for s in stock_sigs:
        risk = abs(s["entry"] - s["sl"])
        key = (f"SL{round(risk / s['atr'], 2)}xATR/"
               f"TP{round(abs(s['tp'] - s['entry']) / s['atr'], 2)}xATR")
        eras.setdefault(key, []).append(s["date"])
    ordered = sorted(eras.items(), key=lambda kv: min(kv[1]))
    windows = {}
    for n, (key, dates) in enumerate(ordered):
        start = min(dates)
        if n + 1 < len(ordered):
            nxt = min(ordered[n + 1][1])
            end = time.strftime("%Y-%m-%d", time.gmtime(
                time.mktime(time.strptime(nxt, "%Y-%m-%d")) - 86400))
        else:
            end = today
        windows[key] = (start, end)
    acc = {k: {"breakout_rule": [], "uptrend_no_breakout": [], "any_day": []}
           for k in windows}
    names = {"signal": "breakout_rule", "uptrend": "uptrend_no_breakout"}
    universe = 0
    for sym in dict.fromkeys(wl.get("stocks", [])):
        bars = completed(load_bars(sym), today)
        if len(bars) < 300:
            continue
        universe += 1
        ser = indicators.series(bars, p)
        d = [b[0] for b in bars]
        h = [b[2] for b in bars]
        l = [b[3] for b in bars]
        c = [b[4] for b in bars]
        for key, (a, b) in windows.items():
            m = re.match(r"SL([\d.]+)xATR/TP([\d.]+)xATR", key)
            ksl, ktp = float(m.group(1)), float(m.group(2))
            for i in range(len(d)):
                if d[i] < a or d[i] > b or not ser["warm"][i]:
                    continue
                cls = indicators.classify(ser, c, i, p)
                risk = ksl * ser["atr"][i]
                sl, tp = c[i] - risk, c[i] + ktp * ser["atr"][i]
                result = "OPEN"
                for j in range(i + 1, len(c)):
                    if l[j] <= sl:
                        result = "SL"
                        break
                    if h[j] >= tp:
                        result = "TP"
                        break
                r = (ktp / ksl if result == "TP" else -1.0 if result == "SL"
                     else (c[-1] - c[i]) / risk)
                row = {"result": result, "R": r}
                acc[key]["any_day"].append(row)
                if cls in names:
                    acc[key][names[cls]].append(row)
    out = {"note": ("Stocks only, same dates, same stops, same scoring as the "
                    "live signals. breakout_rule vs any_day is the test: if the "
                    "rule does not beat random entries, it has no edge here. "
                    "Gross of costs; watchlist is today's names (hindsight)."),
           "universe_stocks": universe, "eras": {}}
    for key, (a, b) in windows.items():
        g = acc[key]
        any_r = summarize(g["any_day"])["avg_R_incl_open"]
        brk = summarize(g["breakout_rule"])
        out["eras"][key] = {
            "window": [a, b],
            "live_stock_signals": None,   # filled by main() from scored rows
            "breakout_rule": brk,
            "uptrend_no_breakout": summarize(g["uptrend_no_breakout"]),
            "any_day": summarize(g["any_day"]),
            "breakout_minus_any_day_R": (
                None if brk["avg_R_incl_open"] is None or any_r is None
                else round(brk["avg_R_incl_open"] - any_r, 2))}
    return out


def main():
    log = json.loads((DATA / "signal_log.json").read_text())
    today = time.strftime("%Y-%m-%d", time.gmtime())
    now = time.strftime("%Y-%m-%d %H:%M:%S", time.gmtime())
    bars_cache = {}
    rows = []
    for sig in sorted(log, key=lambda s: (s["date"], s["symbol"])):
        sym = sig["symbol"]
        if sym not in bars_cache:
            bars_cache[sym] = completed(load_bars(sym), today)
        bars = bars_cache[sym]
        if not bars:
            continue
        result, exit_date = score(sig, bars)
        risk = abs(sig["entry"] - sig["sl"])
        if result == "TP":
            r = abs(sig["tp"] - sig["entry"]) / risk
        elif result == "SL":
            r = -1.0
        else:
            sign = 1 if sig["side"] == "long" else -1
            r = sign * (bars[-1][4] - sig["entry"]) / risk
        ksl = round(risk / sig["atr"], 2)
        ktp = round(abs(sig["tp"] - sig["entry"]) / sig["atr"], 2)
        age = (time.mktime(time.strptime(today, "%Y-%m-%d"))
               - time.mktime(time.strptime(sig["date"], "%Y-%m-%d"))) / 86400
        rows.append({"symbol": sym, "date": sig["date"],
                     "asset_class": sig["asset_class"], "side": sig["side"],
                     "result": result, "exit_date": exit_date, "R": round(r, 3),
                     "settings": f"SL{ksl}xATR/TP{ktp}xATR", "age_days": int(age),
                     "legacy_partial_bar": bool(sig.get("legacy_partial_bar")),
                     "eligible": sig.get("eligible"),
                     "fundamentals": sig.get("fundamentals")})

    # one entry per breakout: a signal counts only once the previous counted
    # signal for the same symbol has resolved (same-day re-entry allowed)
    free = {}
    for x in rows:
        if x["date"] >= free.get(x["symbol"], ""):
            x["independent"] = True
            free[x["symbol"]] = x["exit_date"] or "9999-12-31"
        else:
            x["independent"] = False

    ages = [(0, 7), (8, 14), (15, 30), (31, 99999)]
    summary = {
        "all_signals": summarize(rows),
        "independent_entries": summarize([x for x in rows if x["independent"]]),
        "by_asset_class": {c: summarize([x for x in rows if x["asset_class"] == c])
                           for c in sorted({x["asset_class"] for x in rows})},
        "by_settings": {s: dict(summarize([x for x in rows if x["settings"] == s]),
                                backtest_ref=BACKTEST_REF.get(s))
                        for s in sorted({x["settings"] for x in rows})},
        "by_signal_age_days": {
            (f"{lo}-{hi}" if hi < 99999 else f"{lo}+"):
            summarize([x for x in rows if lo <= x["age_days"] <= hi])
            for lo, hi in ages},
        # Growth screen (added 2026-11-27): judged going forward only. Older
        # signals carry no flag (None) because Yahoo's history is not
        # point-in-time and cannot be back-tested honestly.
        "by_growth_screen": {
            lab: summarize([x for x in rows if x["eligible"] is val])
            for lab, val in (("eligible", True), ("ineligible", False),
                             ("not_screened_yet", None))
            if any(x["eligible"] is val for x in rows)},
    }
    try:
        bench = run_benchmark(log, today)
    except Exception as e:  # never let the benchmark break the ledger
        bench = {"error": str(e)[:200]}
    if bench and "eras" in bench:
        for key, era in bench["eras"].items():
            era["live_stock_signals"] = summarize(
                [x for x in rows if x["settings"] == key
                 and x["asset_class"] == "stock"])
        summary["benchmark"] = bench
    elif bench:
        summary["benchmark"] = bench
    out = {"as_of_utc": now,
           "how_to_read": ("avg_R_incl_open: 1R = distance to the stop; open "
                           "positions marked to last completed close. Compare "
                           "with backtest_ref.expectancy_R. Gross of costs."),
           "summary": summary, "signals": rows}
    (DATA / "ledger.json").write_text(json.dumps(out, indent=1))

    a, i = summary["all_signals"], summary["independent_entries"]
    print(f"LEDGER as of {now} UTC")
    print(f"  all signals        n={a['n']:3d} TP={a['tp']:3d} SL={a['sl']:3d} "
          f"open={a['open']:3d} win%={a['win_pct_of_resolved']} "
          f"avgR(resolved)={a['avg_R_resolved']} avgR(incl open)={a['avg_R_incl_open']}")
    print(f"  independent entries n={i['n']:3d} TP={i['tp']:3d} SL={i['sl']:3d} "
          f"open={i['open']:3d} win%={i['win_pct_of_resolved']} "
          f"avgR(resolved)={i['avg_R_resolved']} avgR(incl open)={i['avg_R_incl_open']}")
    for k, v in summary["by_settings"].items():
        print(f"  settings {k:24s} n={v['n']:3d} TP={v['tp']:3d} SL={v['sl']:3d} "
              f"open={v['open']:3d} win%={v['win_pct_of_resolved']} "
              f"avgR(incl open)={v['avg_R_incl_open']}")
    for k, v in summary["by_signal_age_days"].items():
        print(f"  age {k:7s} days    n={v['n']:3d} TP={v['tp']:3d} SL={v['sl']:3d} "
              f"open={v['open']:3d} avgR(incl open)={v['avg_R_incl_open']}")
    for k, v in summary["by_growth_screen"].items():
        print(f"  growth screen {k:17s} n={v['n']:3d} TP={v['tp']:3d} "
              f"SL={v['sl']:3d} open={v['open']:3d} avgR(incl open)={v['avg_R_incl_open']}")
    bm = summary.get("benchmark") or {}
    for key, era in (bm.get("eras") or {}).items():
        print(f"  BENCHMARK {key} {era['window'][0]}..{era['window'][1]} "
              f"(avgR incl open; live={era['live_stock_signals']['avg_R_incl_open']}):")
        for g in ("breakout_rule", "uptrend_no_breakout", "any_day"):
            v = era[g]
            print(f"     {g:20s} n={v['n']:6d} win%={v['win_pct_of_resolved']} "
                  f"avgR={v['avg_R_incl_open']}")
        print(f"     breakout minus any_day = {era['breakout_minus_any_day_R']}R")


if __name__ == "__main__":
    main()
