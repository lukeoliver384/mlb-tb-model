"""
Record audit — READ-ONLY. Never writes to the sheet.

Answers one question honestly: what is the model's real, defensible track record
when legs are priced ONLY at real entered odds (no -110 fallback, and NOT counting
a price stored for a different line than the one graded), broken out BY LINE so
0.5-line chalk isn't blended with 1.5/2.5 plus-money.

Sections:
  1. Sample overview — graded legs by prop, date range, monthly counts.
  2. Odds coverage — real leaned-side prices; accent-name recoveries; line
     mismatches DROPPED (a 0.5 price is not a 1.5 bet).
  3. Track record per prop: ALL legs (-110 fallback view) vs REAL-PRICED ONLY.
  4. BY LINE, real-priced only — the view that actually matters.
  5. TB by confidence band, real-priced only.
  6. Coherence check.
  7. Real-money bets ledger (ground truth).

Emits the full report to stdout AND (if DISCORD_WEBHOOK_URL is set) posts it to
Discord, chunked. Same env as daily_grade.py: GCP_SERVICE_ACCOUNT_JSON (required),
TRACKER_SHEET_NAME + DISCORD_WEBHOOK_URL (optional). Runs weekly via audit.yml.
"""
import os
import sys
import unicodedata
from collections import defaultdict

import requests

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from daily_grade import _iso, _parse_service_account  # noqa: E402  (same dir)

if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8")

LOG_COLUMNS = ["date", "batter", "batter_id", "pitcher", "venue", "line", "prop",
               "proj", "pred_side", "p_over", "actual", "actual_side", "over_hit", "correct", "graded"]
BET_COLUMNS = ["date", "batter", "batter_id", "pitcher", "venue", "line", "side", "prop",
               "odds", "stake", "actual", "result", "profit", "close_odds", "graded"]
ODDS_COLUMNS = ["date", "prop", "game", "batter", "line", "over", "under"]
GRADED = ("1", "1.0", "True")
CONF_BINS = [0.5, 0.55, 0.60, 0.65, 0.70, 1.01]
CONF_LABELS = ["50-55%", "55-60%", "60-65%", "65-70%", "70%+"]
CLAIMED_TB_ROI = 0.41   # the number to sanity-check against real prices


def _open_sheet():
    sa_raw = os.environ.get("GCP_SERVICE_ACCOUNT_JSON")
    if not sa_raw:
        raise SystemExit("GCP_SERVICE_ACCOUNT_JSON not set.")
    import gspread
    from google.oauth2.service_account import Credentials
    creds = Credentials.from_service_account_info(
        _parse_service_account(sa_raw),
        scopes=["https://www.googleapis.com/auth/spreadsheets",
                "https://www.googleapis.com/auth/drive"])
    gc = gspread.authorize(creds)
    return gc.open(os.environ.get("TRACKER_SHEET_NAME") or "MLB TB Tracker")


def _dec(a):
    try:
        v = float(str(a).replace("+", "").strip())
    except (TypeError, ValueError):
        return None
    if v == 0:
        return None
    return 1 + v / 100.0 if v > 0 else 1 + 100.0 / abs(v)


def _amer(d):
    if not d or d <= 1:
        return None
    return round((d - 1) * 100) if d >= 2 else round(-100 / (d - 1))


def _norm(s):
    s = unicodedata.normalize("NFKD", str(s))
    s = "".join(c for c in s if not unicodedata.combining(c) and c.isprintable())
    return " ".join(s.lower().replace(".", "").split())


def _fline(x):
    try:
        return float(str(x).strip())
    except (TypeError, ValueError):
        return None


def band_of(conf):
    for lo, hi, lbl in zip(CONF_BINS[:-1], CONF_BINS[1:], CONF_LABELS):
        if lo <= conf < hi:
            return lbl
    return CONF_LABELS[-1] if conf >= CONF_BINS[-2] else ""


def summarize(legs):
    n = len(legs)
    if not n:
        return None
    wins = sum(1 for L in legs if L["win"])
    imp = sum(1.0 / L["dec"] for L in legs) / n            # avg breakeven, implied space
    roi = sum((L["dec"] - 1.0) if L["win"] else -1.0 for L in legs) / n
    mev = sum(L["conf"] * (L["dec"] - 1.0) - (1 - L["conf"]) for L in legs) / n
    return {"n": n, "hit": wins / n, "be": imp, "avg_price": _amer(1.0 / imp),
            "roi": roi, "model_ev": mev}


def _px(s):
    return f"{s['avg_price']:+d}" if s and s["avg_price"] is not None else "-"


def _post_discord(text):
    url = os.environ.get("DISCORD_WEBHOOK_URL")
    if not url:
        return
    # chunk on line boundaries, wrap each chunk monospaced so tables align
    chunk, chunks = "", []
    for line in text.splitlines():
        if len(chunk) + len(line) + 1 > 1850:
            chunks.append(chunk); chunk = ""
        chunk += line + "\n"
    if chunk:
        chunks.append(chunk)
    for c in chunks:
        try:
            requests.post(url, json={"content": f"```\n{c}\n```"}, timeout=30).raise_for_status()
        except Exception as e:
            print(f"(discord post failed: {e})")


def main():
    sh = _open_sheet()
    log = sh.sheet1.get_all_records(expected_headers=LOG_COLUMNS)
    try:
        odds_rows = sh.worksheet("odds").get_all_records(expected_headers=ODDS_COLUMNS)
    except Exception:
        odds_rows = []
    try:
        bet_rows = sh.worksheet("bets").get_all_records(expected_headers=BET_COLUMNS)
    except Exception:
        bet_rows = []

    olu, olu_norm = {}, {}
    for r in odds_rows:
        k = (_iso(r.get("date")), str(r.get("prop", "") or "TB").upper(),
             str(r.get("batter", "")).strip())
        rec = {"over": str(r.get("over", "")).strip(), "under": str(r.get("under", "")).strip(),
               "line": str(r.get("line", "")).strip()}
        olu[k] = rec
        olu_norm[(k[0], k[1], _norm(k[2]))] = rec

    by_prop_all = defaultdict(list)
    by_prop_real = defaultdict(list)
    by_line = defaultdict(list)          # (prop, line_str) -> legs, real-priced+matched
    log_by_line = defaultdict(list)      # (prop, line_str) -> win bools, ALL graded legs
    tb_band_real = defaultdict(list)
    monthly = defaultdict(int)
    n_graded = n_priced = n_norm_only = n_linemis = 0
    # join diagnostics
    j_nokey = j_lineblank = j_linematch = j_linemismatch = j_priceblank = 0
    mismatch_pairs = defaultdict(int)     # (log_line -> odds_line) -> count
    dmin, dmax = "9999", "0000"
    FALLBACK_DEC = _dec(-110)

    for rec in log:
        if str(rec.get("graded")) not in GRADED:
            continue
        try:
            p = float(rec.get("p_over")); oh = float(rec.get("over_hit"))
        except (TypeError, ValueError):
            continue
        if not (0 < p < 1):
            continue
        n_graded += 1
        d = _iso(rec.get("date"))
        if d:
            monthly[d[:7]] += 1
            dmin, dmax = min(dmin, d), max(dmax, d)
        prop = str(rec.get("prop") or "TB").upper()
        batter = str(rec.get("batter", "")).strip()
        conf = max(p, 1 - p)
        bet_over = p >= 0.5
        win = bet_over == (oh > 0.5)
        logln = _fline(rec.get("line"))
        log_by_line[(prop, f"{logln:g}" if logln is not None else "?")].append(win)

        o = olu.get((d, prop, batter))
        if o is None:
            o2 = olu_norm.get((d, prop, _norm(batter)))
            if o2 is not None:
                n_norm_only += 1
            o = o2
        dec = _dec(o.get("over") if bet_over else o.get("under")) if o else None

        # --- join diagnostics: why does a leg end up priced or not? --- #
        if o is None:
            j_nokey += 1
        elif dec is None:
            j_priceblank += 1          # matched a row but the leaned side had no price
        else:
            ol = _fline(o.get("line"))
            if not str(o.get("line", "")).strip():
                j_lineblank += 1       # odds row has no line -> we keep it
            elif ol is not None and logln is not None and abs(ol - logln) > 1e-9:
                j_linemismatch += 1
                mismatch_pairs[(f"{logln:g}", f"{ol:g}")] += 1
            else:
                j_linematch += 1

        # Drop a price stored for a different line than the graded leg (a 0.5 price
        # is not this 1.5 bet) — mirrors tracker._line_matches.
        if dec is not None and o.get("line") and logln is not None:
            ol = _fline(o["line"])
            if ol is not None and abs(ol - logln) > 1e-9:
                n_linemis += 1
                dec = None

        if dec is not None:
            n_priced += 1
            leg = {"dec": dec, "win": win, "conf": conf}
            by_prop_real[prop].append(leg); by_prop_real["ALL"].append(leg)
            by_line[(prop, f"{logln:g}" if logln is not None else "?")].append(leg)
            if prop == "TB":
                tb_band_real[band_of(conf)].append(leg)
        legf = {"dec": dec if dec is not None else FALLBACK_DEC, "win": win, "conf": conf}
        by_prop_all[prop].append(legf); by_prop_all["ALL"].append(legf)

    out = []
    def emit(s=""):
        out.append(s)

    emit("=" * 64)
    emit(f"RECORD AUDIT (read-only) — {dmin} -> {dmax}")
    emit("=" * 64)
    emit(f"[1] {n_graded} graded legs" + " · " + ", ".join(f"{m}:{monthly[m]}" for m in sorted(monthly)))
    emit(f"[2] Real leaned-side price: {n_priced}/{n_graded} "
         f"({100*n_priced/n_graded:.0f}%). Accent-recovered: {n_norm_only}. "
         f"Line-mismatch DROPPED: {n_linemis}.")

    # odds store shape
    oprops = defaultdict(int); olines = defaultdict(int); oblank = 0
    for r in odds_rows:
        oprops[str(r.get("prop", "") or "?").upper()] += 1
        lv = str(r.get("line", "")).strip()
        if lv:
            olines[lv] += 1
        else:
            oblank += 1
    emit(f"[2a] ODDS STORE: {len(odds_rows)} rows · props "
         + ", ".join(f"{k}:{v}" for k, v in sorted(oprops.items()))
         + f" · blank-line rows: {oblank}")
    emit("     line values: " + ", ".join(f"{k}:{v}" for k, v in
         sorted(olines.items(), key=lambda kv: (-kv[1]))[:12]))
    emit(f"[2b] JOIN of {n_graded} graded legs: no-odds-row={j_nokey} · "
         f"row-but-no-price={j_priceblank} · line-blank(kept)={j_lineblank} · "
         f"line-match={j_linematch} · line-MISMATCH(dropped)={j_linemismatch}")
    if mismatch_pairs:
        top = sorted(mismatch_pairs.items(), key=lambda kv: -kv[1])[:10]
        emit("     mismatch (logLine->oddsLine): " +
             ", ".join(f"{a}->{b}:{c}" for (a, b), c in top))

    emit("")
    emit("[3] TRACK RECORD — flat 1u, leaned side")
    emit(f"    {'prop':4} {'view':11} {'n':>5} {'hit':>6} {'avgPx':>6} {'ROI':>7}")
    for prop in ("TB", "HRR", "K", "ALL"):
        for lbl, src in (("all(-110fb)", by_prop_all), ("REAL only", by_prop_real)):
            s = summarize(src.get(prop, []))
            if s:
                emit(f"    {prop:4} {lbl:11} {s['n']:>5} {s['hit']*100:>5.1f}% {_px(s):>6} {s['roi']*100:>+6.1f}%")

    emit("")
    emit("[4a] ALL GRADED LEGS BY LOGGED LINE (hit% only — shows where legs sit)")
    emit(f"    {'prop':4} {'line':>5} {'n':>6} {'hit':>6}")
    for prop in ("TB", "HRR", "K"):
        lks = sorted([lk for (pp, lk) in log_by_line if pp == prop],
                     key=lambda x: (x == "?", _fline(x) if x != "?" else 99))
        for lk in lks:
            wl = log_by_line[(prop, lk)]
            emit(f"    {prop:4} {lk:>5} {len(wl):>6} {100*sum(wl)/len(wl):>5.1f}%")

    emit("")
    emit("[4b] REAL-PRICED+MATCHED LEGS BY LINE (price & outcome on the SAME line)")
    emit(f"    {'prop':4} {'line':>5} {'n':>5} {'hit':>6} {'avgPx':>6} {'ROI':>7}")
    for prop in ("TB", "HRR", "K"):
        lks = sorted([lk for (pp, lk) in by_line if pp == prop],
                     key=lambda x: (x == "?", _fline(x) if x != "?" else 9))
        for lk in lks:
            s = summarize(by_line[(prop, lk)])
            if s:
                emit(f"    {prop:4} {lk:>5} {s['n']:>5} {s['hit']*100:>5.1f}% {_px(s):>6} {s['roi']*100:>+6.1f}%")

    emit("")
    emit("[5] TB BY CONFIDENCE BAND — real-priced only")
    for lbl in CONF_LABELS:
        s = summarize(tb_band_real.get(lbl, []))
        emit(f"    {lbl:7} " + ("(none)" if not s else
             f"n={s['n']:<4} hit {s['hit']*100:5.1f}%  avgPx {_px(s):>6}  ROI {s['roi']*100:+6.1f}%"))

    emit("")
    s = summarize(by_prop_real.get("TB", []))
    if s and s["n"] and s["hit"]:
        need = _amer(1.0 + (CLAIMED_TB_ROI + 1.0 - s["hit"]) / s["hit"])
        emit(f"[6] COHERENCE: TB real {s['hit']*100:.1f}% @ {_px(s)} -> {s['roi']*100:+.1f}% ROI. "
             f"A {CLAIMED_TB_ROI*100:.0f}% ROI would need avg {need:+d}.")

    emit("")
    emit("[7] REAL-MONEY BETS LEDGER (ground truth) — overall and BY LINE")
    bp = defaultdict(lambda: {"n": 0, "staked": 0.0, "profit": 0.0, "w": 0, "l": 0, "imp": 0.0, "ni": 0})
    for r in bet_rows:
        if str(r.get("graded")) not in GRADED:
            continue
        res = str(r.get("result", "")).lower()
        if res not in ("win", "loss", "push"):
            continue
        try:
            stake = float(r.get("stake")); profit = float(r.get("profit"))
        except (TypeError, ValueError):
            continue
        prop = str(r.get("prop") or "TB").upper()
        ln = _fline(r.get("line"))
        d = _dec(r.get("odds"))
        keys = [(prop, "ALL"), (prop, f"{ln:g}" if ln is not None else "?"), ("ALL", "ALL")]
        for k in keys:
            b = bp[k]
            b["n"] += 1; b["staked"] += stake; b["profit"] += profit
            b["w"] += res == "win"; b["l"] += res == "loss"
            if d:
                b["imp"] += 1.0 / d; b["ni"] += 1

    def _row(label, b):
        dcd = b["w"] + b["l"]
        roi = b["profit"] / b["staked"] if b["staked"] else 0.0
        avg = _amer(1.0 / (b["imp"] / b["ni"])) if b["ni"] else None
        avgs = f"{avg:+d}" if avg is not None else "-"
        return (f"    {label:12} n={b['n']:<4} {b['w']}-{b['l']}  hit {(100*b['w']/dcd if dcd else 0):4.1f}%  "
                f"avgPx {avgs:>6}  staked ${b['staked']:,.0f}  P/L ${b['profit']:+,.0f}  ROI {roi*100:+.1f}%")

    for prop in ("TB", "HRR", "K"):
        if (prop, "ALL") not in bp:
            continue
        emit(_row(f"{prop} (all)", bp[(prop, "ALL")]))
        lks = sorted([lk for (pp, lk) in bp if pp == prop and lk != "ALL"],
                     key=lambda x: (x == "?", _fline(x) if x != "?" else 99))
        for lk in lks:
            emit(_row(f"  {prop} @ {lk}", bp[(prop, lk)]))
    if ("ALL", "ALL") in bp:
        emit(_row("ALL", bp[("ALL", "ALL")]))

    report = "\n".join(out)
    print(report)
    _post_discord(report)


if __name__ == "__main__":
    main()
# end of scripts/audit_record.py
