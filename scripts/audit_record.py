"""
Record audit — READ-ONLY. Never writes to the sheet.

Answers one question honestly: what is the model's real, defensible track record
when legs are priced ONLY at real entered odds (no -110 fallback), and does the
headline ROI cohere with the actual average price taken?

Sections printed:
  1. Sample overview — graded legs by prop, date range, monthly counts.
  2. Odds coverage — how many graded legs have a real leaned-side price; how many
     more an accent-insensitive name match would recover; line mismatches (odds
     row line != logged line, i.e. the price belongs to a different line).
  3. Track record two ways per prop: ALL legs (-110 fallback, what the app's
     default view shows) vs REAL-PRICED ONLY (the honest number). Flat 1u stakes.
  4. TB by confidence band, real-priced only.
  5. Coherence check — the avg price required for a claimed ROI at the observed
     hit rate vs the actual avg price.
  6. Real-money bets ledger (the "bets" worksheet) — the ground truth.

Same env as daily_grade.py: GCP_SERVICE_ACCOUNT_JSON (required),
TRACKER_SHEET_NAME (optional). Run in GitHub Actions via audit.yml.
"""
import json
import os
import sys
import unicodedata
from collections import defaultdict

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
CLAIMED = {"TB": 0.41, "ALL": None}   # claimed ROI to coherence-check (TB: 41%)


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
    """Accent-stripped, lowercased, squeezed name for fuzzy coverage check."""
    s = unicodedata.normalize("NFKD", str(s))
    s = "".join(c for c in s if not unicodedata.combining(c) and c.isprintable())
    return " ".join(s.lower().replace(".", "").split())


def band_of(conf):
    for lo, hi, lbl in zip(CONF_BINS[:-1], CONF_BINS[1:], CONF_LABELS):
        if lo <= conf < hi:
            return lbl
    return CONF_LABELS[-1] if conf >= CONF_BINS[-2] else ""


def fmt_pct(x):
    return "-" if x is None else f"{x*100:.1f}%"


def summarize(legs):
    """legs: list of dicts with dec, win, conf. Flat 1u metrics."""
    n = len(legs)
    if not n:
        return None
    wins = sum(1 for L in legs if L["win"])
    imp = sum(1.0 / L["dec"] for L in legs) / n            # avg breakeven (implied space)
    roi = sum((L["dec"] - 1.0) if L["win"] else -1.0 for L in legs) / n
    mev = sum(L["conf"] * (L["dec"] - 1.0) - (1 - L["conf"]) for L in legs) / n
    return {"n": n, "hit": wins / n, "be": imp, "avg_price": _amer(1.0 / imp),
            "roi": roi, "model_ev": mev}


def line_for(rec, legs_line):
    try:
        return float(str(rec.get("line", "")).strip())
    except (TypeError, ValueError):
        return None


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

    # ---- odds lookups: exact key and accent-normalized key ---- #
    olu, olu_norm = {}, {}
    for r in odds_rows:
        k = (_iso(r.get("date")), str(r.get("prop", "") or "TB").upper(),
             str(r.get("batter", "")).strip())
        rec = {"over": str(r.get("over", "")).strip(), "under": str(r.get("under", "")).strip(),
               "line": str(r.get("line", "")).strip()}
        olu[k] = rec
        olu_norm[(k[0], k[1], _norm(k[2]))] = rec

    # ---- walk graded legs ---- #
    by_prop_all = defaultdict(list)      # -110 fallback where unpriced
    by_prop_real = defaultdict(list)     # real leaned-side price only
    tb_band_real = defaultdict(list)
    monthly = defaultdict(int)
    n_graded = n_priced = n_norm_only = n_linemis = 0
    dmin, dmax = "9999", "0000"
    FALLBACK_DEC = _dec(-110)

    for rec in log:
        if str(rec.get("graded")) not in GRADED:
            continue
        try:
            p = float(rec.get("p_over"))
            oh = float(rec.get("over_hit"))
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

        okey = (d, prop, batter)
        o = olu.get(okey)
        if o is None:
            o2 = olu_norm.get((d, prop, _norm(batter)))
            if o2 is not None:
                n_norm_only += 1     # a real price exists but exact-name match missed it
            o = o2
        dec = _dec(o.get("over") if bet_over else o.get("under")) if o else None

        if dec is not None:
            n_priced += 1
            log_line = line_for(rec, None)
            if o.get("line") and log_line is not None:
                try:
                    if abs(float(o["line"]) - log_line) > 1e-9:
                        n_linemis += 1
                except ValueError:
                    pass
            leg = {"dec": dec, "win": win, "conf": conf}
            by_prop_real[prop].append(leg)
            by_prop_real["ALL"].append(leg)
            if prop == "TB":
                tb_band_real[band_of(conf)].append(leg)
        legf = {"dec": dec if dec is not None else FALLBACK_DEC, "win": win, "conf": conf}
        by_prop_all[prop].append(legf)
        by_prop_all["ALL"].append(legf)

    # ---- 1. sample ---- #
    print("=" * 72)
    print("RECORD AUDIT (read-only)")
    print("=" * 72)
    print(f"\n[1] SAMPLE: {n_graded} graded legs, {dmin} -> {dmax}")
    for m in sorted(monthly):
        print(f"    {m}: {monthly[m]}")

    # ---- 2. coverage ---- #
    print(f"\n[2] ODDS COVERAGE: {n_priced}/{n_graded} graded legs have a real "
          f"leaned-side price ({fmt_pct(n_priced/n_graded if n_graded else None)})")
    print(f"    recovered only by accent-insensitive name match: {n_norm_only}")
    print(f"    matched legs where the odds line != logged line: {n_linemis} "
          f"(price belongs to a different line -> contaminates ROI)")

    # ---- 3. two-way track record ---- #
    print("\n[3] TRACK RECORD - flat 1u per leg on the model's leaned side")
    hdr = f"    {'prop':6} {'view':11} {'n':>5} {'hit%':>6} {'BE%':>6} {'avgPx':>6} {'ROI':>7} {'modelEV':>8}"
    print(hdr); print("    " + "-" * (len(hdr) - 4))
    for prop in ("TB", "HRR", "K", "ALL"):
        for lbl, src in (("all(-110fb)", by_prop_all), ("REAL only", by_prop_real)):
            s = summarize(src.get(prop, []))
            if not s:
                continue
            px = f"{s['avg_price']:+d}" if s["avg_price"] is not None else "-"
            print(f"    {prop:6} {lbl:11} {s['n']:>5} {s['hit']*100:>5.1f}% {s['be']*100:>5.1f}% "
                  f"{px:>6} {s['roi']*100:>+6.1f}% {s['model_ev']*100:>+7.1f}%")

    # ---- 4. TB by band, real only ---- #
    print("\n[4] TB BY CONFIDENCE BAND - real-priced legs only")
    for lbl in CONF_LABELS:
        s = summarize(tb_band_real.get(lbl, []))
        if not s:
            print(f"    {lbl:7} (no real-priced legs)")
            continue
        px = f"{s['avg_price']:+d}" if s["avg_price"] is not None else "-"
        print(f"    {lbl:7} n={s['n']:<4} hit {s['hit']*100:5.1f}%  BE {s['be']*100:5.1f}%  "
              f"avgPx {px:>6}  ROI {s['roi']*100:+6.1f}%  modelEV {s['model_ev']*100:+6.1f}%")

    # ---- 5. coherence ---- #
    print("\n[5] COHERENCE CHECK")
    s = summarize(by_prop_real.get("TB", []))
    if s and s["n"]:
        claimed = CLAIMED["TB"]
        need_dec = 1.0 + (claimed + 1.0 - s["hit"]) / s["hit"] if s["hit"] else None
        print(f"    TB real-priced: hit {fmt_pct(s['hit'])} at avg price "
              f"{_amer(1.0/s['be']):+d} -> realized flat ROI {s['roi']*100:+.1f}%")
        if need_dec:
            print(f"    for the claimed {claimed*100:.0f}% ROI at that hit rate, avg price "
                  f"must be {_amer(need_dec):+d}. If [3] REAL-only ROI is far below the "
                  f"claimed number, the claim was fallback-price inflated.")
    else:
        print("    (no real-priced TB legs to check)")

    # ---- 6. real bets ledger ---- #
    print("\n[6] REAL-MONEY BETS LEDGER (ground truth)")
    bp = defaultdict(lambda: {"n": 0, "staked": 0.0, "profit": 0.0, "w": 0, "l": 0, "p": 0})
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
        for k in (prop, "ALL"):
            b = bp[k]
            b["n"] += 1; b["staked"] += stake; b["profit"] += profit
            b["w"] += res == "win"; b["l"] += res == "loss"; b["p"] += res == "push"
    if not bp:
        print("    (no graded bets)")
    for k in ("TB", "HRR", "K", "ALL"):
        if k not in bp:
            continue
        b = bp[k]
        dcd = b["w"] + b["l"]
        roi = b["profit"] / b["staked"] if b["staked"] else 0.0
        print(f"    {k:4} n={b['n']:<4} W-L-P {b['w']}-{b['l']}-{b['p']}  "
              f"hit {fmt_pct(b['w']/dcd if dcd else None)}  staked ${b['staked']:,.0f}  "
              f"P/L ${b['profit']:+,.0f}  ROI {roi*100:+.1f}%")
    print("\nDone. Read [3] REAL-only vs all(-110fb): the gap is the fallback bias.")
    print("[6] is what your money actually did; if it is far below [3], the")
    print("difference is timing/price attainment, not model accuracy.")


if __name__ == "__main__":
    main()
# end of scripts/audit_record.py
