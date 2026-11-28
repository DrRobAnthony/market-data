"""Relative-strength pinger (PAPER MODE: nothing here is a trade, it only records and scores).

Rule (the one that held up in the 2012-2026 test, +0.3% per 20-day trade over a random-day baseline):
  Rank every watchlist stock by its 126-trading-day price change. A stock that ENTERS the top 20%
  today (it was not there yesterday) is a ping. Hold 20 trading days, exit at the close.
Each ping is logged as a paper trade and scored when it closes: after 0.2% costs, versus the average
20-day return of ALL watchlist stocks over the same dates. If it does not beat that, stop trusting it.
Growth screen (revenue and profit up every year) is applied as in fetch_data.py; only eligible
pings are emailed, all are logged so the screen can be judged.
"""
import csv, json, os
from pathlib import Path

DATA = Path("data")
OUT_SIG = DATA / "rs_signals.json"
OUT_PAPER = DATA / "rs_paper.json"
HOLD = 20; LOOK = 126; TOP = 0.80; COST = 0.002; MAX_PINGS = 5
MAX_POSITION_PCT = 10.0     # same cap as the breakout bot: a 50% drop costs at most 5% of the account
EMAIL_STATUSES = {"pass"}


def load_closes(sym):
    p = DATA / f"{sym}.csv"
    if not p.exists():
        return None
    rows = []
    with p.open() as f:
        for r in csv.DictReader(f):
            try:
                rows.append((r["date"], float(r["close"])))
            except (ValueError, KeyError):
                pass
    return rows if len(rows) > LOOK + 5 else None


def main():
    wl = json.loads(Path("watchlist.json").read_text())
    px = {s: load_closes(s) for s in wl.get("stocks", [])}
    px = {s: r for s, r in px.items() if r}
    # the trading day most stocks have a bar for (a couple of foreign listings can be a day ahead)
    from collections import Counter
    last = Counter(r[-1][0] for r in px.values()).most_common(1)[0][0]
    for s, r in px.items():                      # a symbol one day ahead is trimmed back to the common day
        while r and r[-1][0] > last:
            r.pop()
    live = {s: r for s, r in px.items() if r and r[-1][0] == last}    # drop stale or dead tickers
    # momentum today and yesterday, aligned by position from the end of each series
    def mom(r, back):
        i = len(r) - 1 - back
        return r[i][1] / r[i - LOOK][1] - 1 if i - LOOK >= 0 else None
    def ranks(back):
        m = {s: mom(r, back) for s, r in live.items()}
        m = {s: v for s, v in m.items() if v is not None}
        order = sorted(m, key=m.get)
        n = len(order)
        return {s: (i + 1) / n for i, s in enumerate(order)}, m
    rk_t, m_t = ranks(0)
    rk_y, _ = ranks(1)
    fund_file = DATA / "fundamentals.json"
    fund = json.loads(fund_file.read_text()) if fund_file.exists() else None
    paper = json.loads(OUT_PAPER.read_text()) if OUT_PAPER.exists() else {"open": [], "closed": []}
    have = {(p["symbol"], p["date"]) for p in paper["open"] + paper["closed"]}
    new = []
    for s in rk_t:
        if rk_t[s] >= TOP and rk_y.get(s, 0) < TOP and (s, last) not in have:
            st = "unknown" if fund is None else (fund.get(s) or {}).get("status", "no_data")
            new.append({"symbol": s, "date": last, "entry": live[s][-1][1], "mom126_pct": round(100 * m_t[s], 1),
                        "rank_pct": round(100 * rk_t[s], 1), "fundamentals": st,
                        "eligible": fund is None or st in EMAIL_STATUSES})
    # paper trades: close the ones that have run HOLD trading days
    all_close = {s: dict(r) for s, r in px.items()}
    still = []
    for p in paper["open"]:
        r = px.get(p["symbol"])
        if not r:
            still.append(p); continue
        dates = [d for d, _ in r]
        if p["date"] not in dates:
            still.append(p); continue
        i = dates.index(p["date"])
        if len(r) - 1 < i + HOLD:
            still.append(p); continue
        exit_d, exit_px = r[i + HOLD]
        ret = exit_px / p["entry"] - 1 - COST
        bm = []
        for s, rr in px.items():
            d = [x for x, _ in rr]
            if p["date"] in d:
                j = d.index(p["date"])
                if len(rr) - 1 >= j + HOLD:
                    bm.append(rr[j + HOLD][1] / rr[j][1] - 1 - COST)
        p.update({"exit_date": exit_d, "exit": exit_px, "ret_pct": round(100 * ret, 2),
                  "universe_avg_pct": round(100 * sum(bm) / len(bm), 2) if bm else None})
        paper["closed"].append(p)
    paper["open"] = still + [dict(n, status="open") for n in new]
    # one-line scoreboard
    cl = paper["closed"]
    def agg(xs):
        xs = [x for x in xs if x["universe_avg_pct"] is not None]
        if not xs: return {"n": 0}
        e = [x["ret_pct"] - x["universe_avg_pct"] for x in xs]
        return {"n": len(xs), "avg_ret_pct": round(sum(x["ret_pct"] for x in xs) / len(xs), 2),
                "win_pct": round(100 * sum(x["ret_pct"] > 0 for x in xs) / len(xs), 1),
                "universe_avg_pct": round(sum(x["universe_avg_pct"] for x in xs) / len(xs), 2),
                "excess_pct": round(sum(e) / len(e), 2)}
    paper["summary"] = {"all": agg(cl), "eligible_only": agg([x for x in cl if x["eligible"]]),
                        "open_positions": len(paper["open"]), "updated": last}
    OUT_PAPER.write_text(json.dumps(paper, indent=1))
    pings = sorted([n for n in new if n["eligible"]], key=lambda n: -n["mom126_pct"])[:MAX_PINGS]
    OUT_SIG.write_text(json.dumps({"date": last, "rule": "enter top 20% of 126-day momentum; hold 20 trading days",
                                   "paper_mode": True, "max_position_pct": MAX_POSITION_PCT,
                                   "pings": pings, "skipped_by_growth_screen": len(new) - len([n for n in new if n["eligible"]]),
                                   "scoreboard": paper["summary"]}, indent=1))
    print(f"{last}: {len(new)} new top-20% entrants, {len(pings)} to ping, "
          f"{len(paper['open'])} open, {len(cl)} closed. Scoreboard: {paper['summary']['all']}")


if __name__ == "__main__":
    main()
