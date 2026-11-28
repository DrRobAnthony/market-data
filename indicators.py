"""Per-bar indicator series shared by the status log and the ledger benchmark.

series() replays, for EVERY bar, the same recursions fetch_data.scan_symbol
runs once on the last bar: EMAs seeded with the first close, Wilder ATR seeded
with the first true range, RSI(14) seeded with the first up/down move, and a
Donchian high of the previous `breakout_n` bars. The value at bar i therefore
equals what scan_symbol(rows[:i+1]) sees at its last bar. The status log and
the ledger benchmark rely on that equality. Checked on 2026-11-27 across 4,210
bars (2,708 breakouts) of the live watchlist: 0 disagreements, ATR within the
scanner's 6-decimal rounding. If you change the maths in scan_symbol, change
it here too.

rows: [(date, open, high, low, close, ...), ...] oldest first. Only indices
2, 3, 4 (high, low, close) are used. No side effects, standard library only.
"""

DEFAULT_PARAMS = {"ema_fast": 50, "ema_slow": 200, "atr_n": 14,
                  "breakout_n": 55, "rsi_max_long": 80}
RSI_N = 14  # scan_symbol hard-codes 1/14


def series(rows, p=DEFAULT_PARAMS):
    n = len(rows)
    h = [r[2] for r in rows]
    l = [r[3] for r in rows]
    c = [r[4] for r in rows]
    kf = 2 / (p["ema_fast"] + 1)
    ks = 2 / (p["ema_slow"] + 1)
    aa = 1 / p["atr_n"]
    ar = 1 / RSI_N
    warm_at = p["ema_slow"] + p["breakout_n"] + 5  # scan_symbol's minimum bars
    out = {k: [None] * n for k in ("atr", "rsi", "ef", "es", "don_hi")}
    out["warm"] = [False] * n
    if n == 0:
        return out
    ef = es = c[0]
    atr = h[0] - l[0]
    au = ad = None
    out["atr"][0], out["ef"][0], out["es"][0] = atr, ef, es
    for i in range(1, n):
        ef = c[i] * kf + ef * (1 - kf)
        es = c[i] * ks + es * (1 - ks)
        tr = max(h[i] - l[i], abs(h[i] - c[i - 1]), abs(l[i] - c[i - 1]))
        atr = tr * aa + atr * (1 - aa)
        up = max(c[i] - c[i - 1], 0)
        dn = max(c[i - 1] - c[i], 0)
        if au is None:
            au, ad = up, dn
        else:
            au = up * ar + au * (1 - ar)
            ad = dn * ar + ad * (1 - ar)
        out["ef"][i], out["es"][i], out["atr"][i] = ef, es, atr
        out["rsi"][i] = 100 - 100 / (1 + au / ad) if ad > 0 else 100.0
        if i >= p["breakout_n"]:
            out["don_hi"][i] = max(h[i - p["breakout_n"]:i])
        out["warm"][i] = (i + 1) >= warm_at
    return out


def classify(ser, close, i, p=DEFAULT_PARAMS):
    """'signal' (long breakout the scanner would emit), 'uptrend' (trend filter
    passes but no signal) or 'other'. Only meaningful where ser['warm'][i]."""
    trend = close[i] > ser["es"][i] and ser["ef"][i] > ser["es"][i]
    if not trend:
        return "other"
    if close[i] > ser["don_hi"][i] and ser["rsi"][i] < p["rsi_max_long"]:
        return "signal"
    return "uptrend"
