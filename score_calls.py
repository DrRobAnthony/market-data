"""Score a channel's posted trade calls against real hourly prices.

Rules (fixed in advance, same for every call):
  - Trade the FIRST entry only. "CMP" entries fill at the open of the first hour after the post;
    limit entries must be touched within 7 days or the call is "no fill".
  - Stop = the stop-loss posted with the call. 1R = |entry - stop|.
  - Take half off at +1R and move the stop to entry; exit the rest at +2R, or at entry (breakeven), or at stop.
  - If stop and target are inside the same hour, the stop is assumed hit first.
  - Open trades are closed at the market after 14 days. Costs: 0.1% of entry price per round trip.
Control: for each call, 20 random start times for the same coin (within 30 days before the call),
same side, same stop distance %, same exits and costs. If the calls beat the control, there is skill.
"""
import json, os, random, sys, time, urllib.request, urllib.parse
from pathlib import Path

HERE = Path(__file__).parent
CALLS = HERE / "data/external/prof_calls.json"
OUT = HERE / "data/external/prof_scored.json"
KEY = os.environ.get("CRYPTOCOMPARE_API_KEY", "")
H = 3600; HORIZON = 14 * 24; FILL_WAIT = 7 * 24; COST = 0.001; N_CONTROL = 20
random.seed(7)


def get(url):
    req = urllib.request.Request(url, headers={"Accept": "application/json", "User-Agent": "Mozilla/5.0"})
    with urllib.request.urlopen(req, timeout=30) as r:
        return json.load(r)


def bars_binance(sym, start, end):
    rows = {}
    t0 = start
    while t0 < end:
        q = urllib.parse.urlencode({"symbol": f"{sym}USDT", "interval": "1h", "startTime": t0 * 1000,
                                    "endTime": end * 1000, "limit": 1000})
        data = get("https://data-api.binance.vision/api/v3/klines?" + q)
        if not data:
            break
        for c in data:
            ts = int(c[0]) // 1000
            rows[ts] = {"t": ts, "o": float(c[1]), "h": float(c[2]), "l": float(c[3]), "c": float(c[4])}
        t0 = int(data[-1][0]) // 1000 + H
        time.sleep(0.1)
    return rows


def bars_gate(sym, start, end):
    rows = {}
    t0 = start
    while t0 < end:
        t1 = min(end, t0 + 900 * H)
        q = urllib.parse.urlencode({"currency_pair": f"{sym}_USDT", "interval": "1h", "from": t0, "to": t1})
        for c in get("https://api.gateio.ws/api/v4/spot/candlesticks?" + q):
            ts = int(c[0]); rows[ts] = {"t": ts, "o": float(c[5]), "h": float(c[3]),
                                        "l": float(c[4]), "c": float(c[2])}
        t0 = t1
        time.sleep(0.15)
    return rows


def bars(sym, end_ts, n=None):
    """Hourly candles, 45 days ending at end_ts. Binance public mirror first (deep history), Gate.io fallback."""
    mult = 1
    if sym.startswith("1000") and len(sym) > 4:
        sym, mult = sym[4:], 1000
    start = end_ts - 45 * 24 * H
    rows = {}
    for name, fn in (("binance", bars_binance), ("gate", bars_gate)):
        try:
            rows = fn(sym, start, end_ts)
            if len(rows) > 24:
                break
        except Exception as e:
            print("fetch error", name, sym, e, file=sys.stderr)
    out = [rows[k] for k in sorted(rows) if rows[k]["c"] > 0]
    if len(out) <= 24:
        return None
    return [{"t": b["t"], "o": b["o"]*mult, "h": b["h"]*mult, "l": b["l"]*mult, "c": b["c"]*mult} for b in out]


def run_trade(B, i0, side, entry, stop):
    """B bars, trade starts at bar i0 filled at `entry`. Returns R (after costs) and outcome."""
    d = 1 if side == "LONG" else -1
    risk = abs(entry - stop)
    if risk <= 0 or (entry - stop) * d <= 0:
        return None
    one, two = entry + d * risk, entry + d * 2 * risk
    half = False; cur_stop = stop; realized = 0.0
    last = min(len(B), i0 + HORIZON)
    for i in range(i0, last):
        b = B[i]
        hit_stop = b["l"] <= cur_stop if d == 1 else b["h"] >= cur_stop
        if hit_stop:
            r = (cur_stop - entry) * d / risk
            realized += (0.5 if half else 1.0) * r
            return realized - COST * entry / risk, ("win_then_be" if half else "stop")
        if not half and ((b["h"] >= one) if d == 1 else (b["l"] <= one)):
            half = True; realized += 0.5 * 1.0; cur_stop = entry
            hit2 = (b["h"] >= two) if d == 1 else (b["l"] <= two)
            if hit2:
                realized += 0.5 * 2.0
                return realized - COST * entry / risk, "win_2R"
            continue
        if half and ((b["h"] >= two) if d == 1 else (b["l"] <= two)):
            realized += 0.5 * 2.0
            return realized - COST * entry / risk, "win_2R"
    c = B[last - 1]["c"]
    r = (c - entry) * d / risk
    realized += (0.5 if half else 1.0) * r
    return realized - COST * entry / risk, "timeout"


def score(call):
    # Telegram Desktop exports in the computer's local time (Rob: America/Chicago)
    from datetime import datetime
    from zoneinfo import ZoneInfo
    ts = int(datetime.strptime(call["t"], "%Y-%m-%dT%H:%M:%S").replace(tzinfo=ZoneInfo("America/Chicago")).timestamp())
    B = bars(call["sym"], ts + (HORIZON + 24) * H)
    if not B:
        return {"status": "no_data"}
    i0 = next((i for i, b in enumerate(B) if b["t"] >= ts), None)
    if i0 is None or i0 + 2 >= len(B):
        return {"status": "no_data"}
    side, e0, sl = call["side"], call["entries"][0], call["sl"]
    if call["cmp"]:
        entry, istart = B[i0]["o"], i0
        # the posted stop is relative to the posted price; keep the posted distance as a percent
        pct = abs(e0 - sl) / e0
        stop = entry * (1 - pct) if side == "LONG" else entry * (1 + pct)
    else:
        entry, stop, istart = e0, sl, None
        for i in range(i0, min(len(B), i0 + FILL_WAIT)):
            if B[i]["l"] <= entry <= B[i]["h"]:
                istart = i; break
        if istart is None:
            return {"status": "no_fill"}
    res = run_trade(B, istart, side, entry, stop)
    if not res:
        return {"status": "bad_levels"}
    R, why = res
    pct = abs(entry - stop) / entry
    ctrl = []
    lo_i = max(0, i0 - 30 * 24)
    pool = list(range(lo_i, max(lo_i + 1, i0 - 1)))
    for i in random.sample(pool, min(N_CONTROL, len(pool))) if pool else []:
        e = B[i]["o"]; s = e * (1 - pct) if side == "LONG" else e * (1 + pct)
        r2 = run_trade(B, i, side, e, s)
        if r2: ctrl.append(r2[0])
    return {"status": "scored", "R": round(R, 3), "why": why, "stop_pct": round(pct * 100, 2),
            "control_R": round(sum(ctrl) / len(ctrl), 3) if ctrl else None}


def main():
    calls = json.load(open(CALLS))
    results = []
    for c in calls:
        r = score(c); r.update({k: c[k] for k in ("t", "sym", "side", "cmp")})
        results.append(r); time.sleep(0.25)
    sc = [r for r in results if r["status"] == "scored"]
    def summ(rs):
        if not rs: return {}
        n = len(rs); rv = [r["R"] for r in rs]
        cr = [r["control_R"] for r in rs if r["control_R"] is not None]
        return {"n": n,
                "stopped_out_pct": round(100 * sum(r["why"] == "stop" for r in rs) / n, 1),
                "hit_1R_then_breakeven_pct": round(100 * sum(r["why"] == "win_then_be" for r in rs) / n, 1),
                "hit_2R_pct": round(100 * sum(r["why"] == "win_2R" for r in rs) / n, 1),
                "timeout_pct": round(100 * sum(r["why"] == "timeout" for r in rs) / n, 1),
                "any_profit_pct": round(100 * sum(x > 0 for x in rv) / n, 1),
                "avg_R": round(sum(rv) / n, 3), "total_R": round(sum(rv), 1),
                "control_avg_R": round(sum(cr) / len(cr), 3) if cr else None}
    summary = {"all_calls": len(calls), "no_data": sum(r["status"] == "no_data" for r in results),
               "no_fill": sum(r["status"] == "no_fill" for r in results),
               "scored": summ(sc),
               "longs": summ([r for r in sc if r["side"] == "LONG"]),
               "shorts": summ([r for r in sc if r["side"] == "SHORT"]),
               "cmp_entries": summ([r for r in sc if r["cmp"]]),
               "limit_entries": summ([r for r in sc if not r["cmp"]])}
    OUT.write_text(json.dumps({"summary": summary, "results": results}, indent=1))
    print(json.dumps(summary, indent=1))

if __name__ == "__main__":
    main()
