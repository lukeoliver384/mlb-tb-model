"""
Roster-triggered auto-logger — no Streamlit involved.

Closes the loop so grading has something to grade: it logs the day's projections
to the tracker Google Sheet automatically, the moment each game's OFFICIAL lineup
is posted ("rosters submitted") and while the game is still pre-game — so the
logged projection is point-in-time clean (season-to-date stats don't yet include
today's game). Then scripts/daily_grade.py grades those rows after the games end.

Runs headless in GitHub Actions (like scripts/daily_ping.py) and writes to the
sheet directly via gspread (reusing scripts/daily_grade.py's sheet helpers).

PER-GAME GATING (why it can run many times a day safely):
  * a side's batters (TB + H+R+RBI) and the opposing starter (K) are logged only
    once that side's lineup is the CONFIRMED official order (Batter.expected is
    False), not the projected-placeholder fallback;
  * only while the game's abstractGameState is "Preview" (not started) — never
    after first pitch, which would leak the game into its own projection;
  * idempotent: a (date, player, prop) already in the log is skipped, so repeated
    runs never double-log or overwrite an earlier clean projection.

PROJECTION CONFIG — deliberately the model's fixed DEFAULTS (same philosophy as
daily_ping.py's Discord picks), NOT the app's mutable sidebar settings. A stable
config over time keeps the calibration history from being polluted by day-to-day
slider changes. Shared numeric defaults are imported from daily_ping so the auto-
logged TB projection matches the Discord pick exactly.

LINES: TB and H+R+RBI use the standard 1.5. Strikeouts have no standard line, so
— exactly like the app's "Log all props" — a pitcher's K row is logged only if a K
line exists for him in the odds sheet.

ENV: same as scripts/daily_grade.py (GCP_SERVICE_ACCOUNT_JSON required,
TRACKER_SHEET_NAME optional, DISCORD_WEBHOOK_URL optional, GRADE_SEASON optional).

Run locally:
    GCP_SERVICE_ACCOUNT_JSON="$(cat key.json)" python scripts/daily_log.py [--date YYYY-MM-DD]
"""
import argparse
import datetime as dt
import math
import os
import sys

import requests

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import data as D          # noqa: E402
import engine as E        # noqa: E402
import park_factors as PF  # noqa: E402
import daily_ping as dp   # noqa: E402  (shared numeric defaults + slate build)
import daily_grade as G   # noqa: E402  (sheet helpers: _open_sheet, _iso, LOG_COLUMNS, _rows_to_matrix)

if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8")

# ---- fixed default projection config (see module docstring) ----
REG_HRR = 175
REG_KMOD = int(getattr(E, "REG_KMODEL", 70))
LINEUP_CTX = 0.5
TEMP = {"TB": 1.5, "HRR": 2.0, "K": 1.4}   # per-prop temperature (app defaults)
LINE_TB = LINE_HRR = 1.5


def _calibrate(p, prop):
    """Temperature-scale a probability, matching the app's default per-prop temps."""
    t = TEMP.get(prop, 1.0)
    if not t or t == 1.0 or not (0 < p < 1):
        return p
    return 1.0 / (1.0 + math.exp(-(math.log(p / (1 - p)) / t)))


def _hand(b, opp):
    side = b.bats
    if side == "S":
        side = "L" if opp.throws == "R" else "R"
    return side


def project_tb(b, opp, venue, savant_bat, savant_pit, bhome, phome):
    total_pa = PF.expected_pa(b.order)
    this_share = (E.pa_vs_starter(b.order, opp.bf_per_start, total_pa) / total_pa
                  if opp.bf_per_start > 0 else 1.0)
    side = _hand(b, opp)
    pmult = PF.park_mult_hand(venue, side)
    park_ev = PF.park_event_hand(venue, side, 1.0)

    b_rate, b_n = b.tb_per_pa_vs(opp.throws)
    if not b_rate:
        b_rate, b_n = b.tb_per_pa, b.pa
    p_rate = (opp.tb_per_bf_vs_l if b.bats in ("L", "S") else opp.tb_per_bf_vs_r) or opp.tb_per_bf
    b_rate0, p_rate0 = b_rate, p_rate

    if b.recent_pa > 0:
        recent_rate = b.recent_tb / b.recent_pa
        eff_w = dp.W_RECENT * b.recent_pa / (b.recent_pa + 50.0)
        b_rate = eff_w * recent_rate + (1 - eff_w) * b_rate
    if b.mlbam_id in savant_bat:
        b_rate = E.blend(b_rate, b_rate * savant_bat[b.mlbam_id]["luck"], dp.W_STATCAST)
    if opp.mlbam_id in savant_pit:
        p_rate = E.blend(p_rate, p_rate * savant_pit[opp.mlbam_id]["luck"], dp.W_STATCAST)
    b_rate *= b.home_away_factor(bhome)
    p_rate *= opp.home_away_factor(phome)
    if not b_rate or not p_rate:
        return None

    shares = b.hit_shares()
    ht = E.HitTypeShares(*shares) if shares else E.HitTypeShares()
    ber = per = None
    raw_b = b.event_rates_vs(opp.throws)
    raw_p = opp.event_rates_allowed_vs(side)
    if raw_b and raw_p:
        b_adj = b_rate / b_rate0 if b_rate0 else 1.0
        p_adj = p_rate / p_rate0 if p_rate0 else 1.0
        ber = {ev: E.regress(raw_b[ev], b_n, E.LEAGUE_EVENT_RATES[ev], dp.REG_TB) * b_adj for ev in raw_b}
        per = {ev: E.regress(raw_p[ev], max(opp.bf, 1), E.LEAGUE_EVENT_RATES[ev], dp.REG_TB) * p_adj for ev in raw_p}

    inp = E.ProjectionInput(
        batter_tb_per_pa=b_rate, batter_pa_sample=b_n,
        pitcher_tb_per_pa_allowed=p_rate, pitcher_bf_sample=max(opp.bf, 1),
        line=LINE_TB, side="Over", expected_pa=total_pa, park_mult=pmult,
        shares=ht, sp_share=this_share, bullpen_rate=dp.BULLPEN_RATE,
        reg_k=dp.REG_TB, batter_event_rates=ber, pitcher_event_rates=per,
        park_event_mult=park_ev)
    r = E.project(inp)
    p = _calibrate(r.p_cover, "TB")
    return {"prop": "TB", "line": LINE_TB, "proj": round(r.lam, 3), "p_over": round(p, 4),
            "batter_id": b.mlbam_id, "batter": b.name, "pitcher": opp.name, "venue": venue}


def project_hrr(b, opp, venue, bhome, phome):
    total_pa = PF.expected_pa(b.order)
    this_share = (E.pa_vs_starter(b.order, opp.bf_per_start, total_pa) / total_pa
                  if opp.bf_per_start > 0 else 1.0)
    side = _hand(b, opp)
    pmult = PF.park_mult_hand(venue, side)

    er = b.event_rates_vs(opp.throws)
    h_pa = sum(er.values()) if er else b.hits_per_pa
    pr = opp.event_rates_allowed_vs(side)
    p_h_bf = sum(pr.values()) if pr else opp.h_per_bf
    if not h_pa:
        return None
    ha = b.home_away_factor(bhome)
    park_runs = pmult
    r_ctx, rbi_ctx = PF.lineup_run_context(b.order, LINEUP_CTX)
    tto = 1.0
    if opp.bf_per_start > 0:
        sppa = E.pa_vs_starter(b.order, opp.bf_per_start, total_pa)
        if sppa > 0:
            tto = E.tto_weighted(b.order, opp.bf_per_start, total_pa, E.TTO_TB_MULT) / sppa
    lam, p_cover = E.project_hrr(
        h_pa * ha, b.pa, p_h_bf, max(opp.bf, 1),
        b.runs_per_pa * ha, b.rbi_per_pa * ha, opp.r_per_bf,
        line=LINE_HRR, side="Over", expected_pa=total_pa,
        park_hits=pmult, park_runs=park_runs, reg_k=REG_HRR, sp_share=this_share,
        r_ctx=r_ctx, rbi_ctx=rbi_ctx, tto=tto)
    p = _calibrate(p_cover, "HRR")
    return {"prop": "HRR", "line": LINE_HRR, "proj": round(lam, 3), "p_over": round(p, 4),
            "batter_id": b.mlbam_id, "batter": b.name, "pitcher": opp.name, "venue": venue}


def project_k(pit, opp_lineup):
    """PA-weighted projected Ks for a starter vs the confirmed opposing lineup."""
    if not pit or not opp_lineup:
        return None
    total_k = 0.0
    for b in opp_lineup:
        k_pa, k_n = b.k_per_pa_vs(pit.throws)
        bk = E.regress(k_pa, k_n, E.LEAGUE_K_PA, REG_KMOD)
        p_k_bf, p_k_n = pit.k_per_bf_vs(b.bats)
        pk = E.regress(p_k_bf, p_k_n, E.LEAGUE_K_PA, REG_KMOD)
        km = E.log5_rate(bk, pk, E.LEAGUE_K_PA)
        tp = PF.expected_pa(b.order)
        eff_pa = (E.tto_weighted(b.order, pit.bf_per_start, tp, E.TTO_K_MULT)
                  if pit.bf_per_start > 0 else tp)
        total_k += km * eff_pa
    return total_k


# --------------------------------------------------------------------------- #
# Game gating                                                                 #
# --------------------------------------------------------------------------- #
def _game_states(date_str: str) -> dict:
    """{game_pk: abstractGameState} — 'Preview' = not started."""
    try:
        d = requests.get(f"{D.STATSAPI}/schedule", params=dict(sportId=1, date=date_str),
                         headers=D.HEADERS, timeout=D.TIMEOUT).json()
    except Exception:
        return {}
    out = {}
    for day in d.get("dates", []):
        for g in day.get("games", []):
            out[g["gamePk"]] = (g.get("status", {}) or {}).get("abstractGameState", "")
    return out


def _confirmed(lineup) -> bool:
    return bool(lineup) and not getattr(lineup[0], "expected", False)


def _k_line_lookup(sh, date_iso: str) -> dict:
    """{pitcher_name: line} from the odds sheet's K rows for this date."""
    out = {}
    try:
        ows = sh.worksheet("odds")
        for r in ows.get_all_records():
            if str(r.get("prop", "")).upper() != "K":
                continue
            if G._iso(r.get("date")) != date_iso:
                continue
            ln = str(r.get("line", "")).strip()
            if ln:
                out[str(r.get("batter", "")).strip()] = ln
    except Exception:
        pass
    return out


def _existing_keys(ws) -> set:
    """{(iso_date, str(batter_id), PROP)} already in the log — for idempotency."""
    keys = set()
    try:
        for r in ws.get_all_records(expected_headers=G.LOG_COLUMNS):
            keys.add((G._iso(r.get("date")), str(r.get("batter_id")).strip(),
                      str(r.get("prop", "")).upper()))
    except Exception:
        pass
    return keys


def _row(date_str, rec):
    """Build a fresh (ungraded) LOG_COLUMNS row dict from a projection rec."""
    pred = "Over" if rec["p_over"] >= 0.5 else "Under"
    return {"date": date_str, "batter": rec["batter"], "batter_id": rec["batter_id"],
            "pitcher": rec["pitcher"], "venue": rec["venue"], "line": rec["line"],
            "prop": rec["prop"], "proj": rec["proj"], "pred_side": pred,
            "p_over": rec["p_over"], "actual": "", "actual_side": "", "over_hit": "",
            "correct": "", "graded": 0}


def run(date_str: str, season: int) -> dict:
    states = _game_states(date_str)
    geo = dict(PF.PARK_GEO)
    for alias, real in PF.PARK_ALIASES.items():
        if real in PF.PARK_GEO:
            geo[alias] = PF.PARK_GEO[real]
    slate = D.build_slate(date_str, season, recent_days=dp.RECENT_DAYS, want_weather=False, park_geo=geo)

    lr = D.league_event_rates(season)
    if lr:
        E.LEAGUE_EVENT_RATES.update({k: lr[k] for k in ("1B", "2B", "3B", "HR") if k in lr})
        if "TB" in lr:
            E.LEAGUE_TB_PER_PA = lr["TB"]
    savant_bat = D.load_savant_expected(season, "batter")
    savant_pit = D.load_savant_expected(season, "pitcher")

    sh = G._open_sheet()
    ws = sh.sheet1
    have = _existing_keys(ws)
    date_iso = G._iso(date_str)
    k_lines = _k_line_lookup(sh, date_iso)

    new_recs = []
    skipped_started = skipped_unconfirmed = 0

    def _add(rec):
        if not rec:
            return
        key = (date_iso, str(rec["batter_id"]).strip(), rec["prop"].upper())
        if key in have:
            return
        have.add(key)
        new_recs.append(rec)

    for mu in slate:
        if states.get(mu.game_pk, "") != "Preview":   # started/final — too late, would leak
            skipped_started += 1
            continue
        # away side confirmed -> away batters (vs home SP) + home SP's Ks (vs away lineup)
        if _confirmed(mu.away_lineup) and mu.home_pitcher:
            for b in mu.away_lineup:
                _add(project_tb(b, mu.home_pitcher, mu.venue, savant_bat, savant_pit, False, True))
                _add(project_hrr(b, mu.home_pitcher, mu.venue, False, True))
            lam = project_k(mu.home_pitcher, mu.away_lineup)
            ln = k_lines.get(mu.home_pitcher.name.strip())
            if lam is not None and ln:
                p = _calibrate(E.p_cover_negbin(lam, float(ln), "Over", E.K_DISPERSION), "K")
                _add({"prop": "K", "line": float(ln), "proj": round(lam, 3), "p_over": round(p, 4),
                      "batter_id": mu.home_pitcher.mlbam_id, "batter": mu.home_pitcher.name,
                      "pitcher": f"vs {mu.away}", "venue": mu.venue})
        elif mu.home_pitcher:
            skipped_unconfirmed += 1
        # home side confirmed -> home batters (vs away SP) + away SP's Ks (vs home lineup)
        if _confirmed(mu.home_lineup) and mu.away_pitcher:
            for b in mu.home_lineup:
                _add(project_tb(b, mu.away_pitcher, mu.venue, savant_bat, savant_pit, True, False))
                _add(project_hrr(b, mu.away_pitcher, mu.venue, True, False))
            lam = project_k(mu.away_pitcher, mu.home_lineup)
            ln = k_lines.get(mu.away_pitcher.name.strip())
            if lam is not None and ln:
                p = _calibrate(E.p_cover_negbin(lam, float(ln), "Over", E.K_DISPERSION), "K")
                _add({"prop": "K", "line": float(ln), "proj": round(lam, 3), "p_over": round(p, 4),
                      "batter_id": mu.away_pitcher.mlbam_id, "batter": mu.away_pitcher.name,
                      "pitcher": f"vs {mu.home}", "venue": mu.venue})
        elif mu.away_pitcher:
            skipped_unconfirmed += 1

    if new_recs:
        # append only the new rows (never rewrite existing clean projections)
        rows = [_row(date_str, r) for r in new_recs]
        ws.append_rows(G._rows_to_matrix(rows, G.LOG_COLUMNS), value_input_option="RAW")

    by_prop = {}
    for r in new_recs:
        by_prop[r["prop"]] = by_prop.get(r["prop"], 0) + 1
    return {"logged": len(new_recs), "by_prop": by_prop,
            "games": len(slate), "skipped_started": skipped_started,
            "skipped_unconfirmed": skipped_unconfirmed}


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--date", default=D.today_local().isoformat())
    ap.add_argument("--season", type=int,
                    default=int(os.environ.get("GRADE_SEASON") or D.today_local().year))
    args = ap.parse_args()
    res = run(args.date, args.season)
    summary = (f"Auto-log {args.date} — logged {res['logged']} rows {res['by_prop']} "
               f"across {res['games']} games (skipped {res['skipped_started']} started, "
               f"{res['skipped_unconfirmed']} awaiting lineups)")
    print(summary)
    if res["logged"]:
        url = os.environ.get("DISCORD_WEBHOOK_URL")
        if url:
            try:
                requests.post(url, json={"content": summary}, timeout=30).raise_for_status()
            except Exception as e:
                print(f"(discord post failed: {e})")


if __name__ == "__main__":
    main()
# end of scripts/daily_log.py
