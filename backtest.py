"""
Historical backtest — point-in-time, no look-ahead.

WHY THIS EXISTS
---------------
The app pulls *season-to-date* batting/pitching stats and *last-N-days-from-today*
recent form. Backtesting a past game with those would leak the future into the
projection (you'd be grading the model on data that only existed AFTER the game).
This script rebuilds every player's stats **as of the day before each game** via
the MLB StatsAPI `byDateRange` endpoint, so each projection only sees what was
knowable at first pitch. That's the difference between a backtest you can trust
with real money and one that flatters itself.

WHAT IT DOES
------------
1. For each date in the range, pulls that day's FINAL games and their ACTUAL
   batting orders (from the boxscore — real lineups, not projected).
2. Rebuilds each batter/pitcher's stats as-of (date - 1) and recent form as-of.
3. Runs your production projection (`daily_ping.project_batter`) unchanged.
4. Grades P(Over the line) against the actual total bases the batter recorded.
5. Reports MAE, directional accuracy, Brier score, log-loss, a reliability
   diagram, and fits your Platt calibration to show how much it would help.

DELIBERATE SIMPLIFICATIONS (all documented so you can trust the number)
-----------------------------------------------------------------------
* L/R and home/away SPLITS are not reconstructed point-in-time (the public
  `byDateRange` endpoint doesn't expose sitCodes cleanly). The Batter/Pitcher
  split methods already fall back to the overall rate when a split sample is 0,
  so the backtest runs on fully-adjusted-except-splits rates. This makes the
  backtest a hair CONSERVATIVE vs the live app, never optimistic — good.
* Statcast "luck" blending is OFF by default (`--savant` to enable). The Savant
  leaderboard is current-season cumulative, so turning it on reintroduces mild
  look-ahead. Off = clean.
* League baseline event rates use season totals (a league-wide constant, not a
  player edge) — negligible leakage, matches production behavior.

USAGE
-----
    python backtest.py --start 2026-04-01 --end 2026-06-30
    python backtest.py --start 2026-06-01 --end 2026-06-30 --line 1.5 --recent-days 21
    python backtest.py --start 2026-06-01 --end 2026-06-30 --write-log   # feed the app's tracker

Output: prints a summary and writes backtest_results.csv. With --write-log it
also appends graded rows into your tracker log so the Calibration tab lights up.
"""
from __future__ import annotations

import argparse
import datetime as dt
import os
import sys
from concurrent.futures import ThreadPoolExecutor

import requests
import pandas as pd

import data as D
import engine as E
import park_factors as PF

# daily_ping lives in scripts/ — reuse its production projection code verbatim
sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), "scripts"))
import daily_ping as dp  # noqa: E402

STATSAPI = D.STATSAPI
HEADERS = D.HEADERS
TIMEOUT = D.TIMEOUT


def _get(url, **params):
    r = requests.get(url, params=params, headers=HEADERS, timeout=TIMEOUT)
    r.raise_for_status()
    return r.json()


# --------------------------------------------------------------------------- #
# Point-in-time stat reconstruction (byDateRange)                             #
# --------------------------------------------------------------------------- #
def _sum_range_stat(pid: int, group: str, season: int, start: str, end: str) -> dict:
    """Cumulative stat totals for a player over [start, end] (inclusive).
    Sums across any returned splits (handles trades / multiple stint rows)."""
    try:
        d = _get(f"{STATSAPI}/people/{pid}/stats", stats="byDateRange", group=group,
                 season=season, sportId=1, startDate=start, endDate=end)
        splits = d["stats"][0]["splits"]
    except Exception:
        return {}
    acc: dict = {}
    for sp in splits:
        for k, v in (sp.get("stat") or {}).items():
            try:
                acc[k] = acc.get(k, 0.0) + float(v)
            except (TypeError, ValueError):
                continue
    return acc


def fill_batter_asof(b: D.Batter, season: int, end: str, season_start: str) -> D.Batter:
    s = _sum_range_stat(b.mlbam_id, "hitting", season, season_start, end)
    if s:
        b.pa = s.get("plateAppearances", 0.0)
        b.tb = s.get("totalBases", 0.0)
        h, d2, t, hr = (s.get("hits", 0.0), s.get("doubles", 0.0),
                        s.get("triples", 0.0), s.get("homeRuns", 0.0))
        b.single = h - d2 - t - hr
        b.double, b.triple, b.hr = d2, t, hr
        b.runs = s.get("runs", 0.0)
        b.rbi = s.get("rbi", 0.0)
        b.k = s.get("strikeOuts", 0.0)
    return b


def fill_pitcher_asof(p: D.Pitcher, season: int, end: str, season_start: str) -> D.Pitcher:
    s = _sum_range_stat(p.mlbam_id, "pitching", season, season_start, end)
    if s:
        h, d2, t, hr = (s.get("hits", 0.0), s.get("doubles", 0.0),
                        s.get("triples", 0.0), s.get("homeRuns", 0.0))
        singles = h - d2 - t - hr
        p.tb_allowed = singles + 2 * d2 + 3 * t + 4 * hr
        p.bf = s.get("battersFaced", 0.0)
        p.games_started = s.get("gamesStarted", 0.0)
        p.h_allowed, p.d_allowed, p.t_allowed, p.hr_allowed = h, d2, t, hr
        p.r_allowed = s.get("runs", 0.0)
        p.k_allowed = s.get("strikeOuts", 0.0)
    return p


def fill_recent_asof(b: D.Batter, season: int, game_date: dt.date, days: int) -> D.Batter:
    if days <= 0:
        return b
    end = game_date - dt.timedelta(days=1)
    start = end - dt.timedelta(days=days)
    s = _sum_range_stat(b.mlbam_id, "hitting", season, start.isoformat(), end.isoformat())
    if s:
        b.recent_pa = s.get("plateAppearances", 0.0)
        b.recent_tb = s.get("totalBases", 0.0)
    return b


# --------------------------------------------------------------------------- #
# One day                                                                     #
# --------------------------------------------------------------------------- #
def _final_games(date_str: str) -> list[D.Matchup]:
    """Schedule for a date, restricted to games that reached Final."""
    games = D.get_schedule(date_str)
    if not games:
        return []
    try:
        data = _get(f"{STATSAPI}/schedule", sportId=1, date=date_str)
        final_pks = set()
        for d in data.get("dates", []):
            for g in d.get("games", []):
                st = g.get("status", {})
                if (st.get("abstractGameState") == "Final"
                        or st.get("codedGameState") in ("F", "O")):
                    final_pks.add(g["gamePk"])
        games = [g for g in games if g.game_pk in final_pks]
    except Exception:
        pass
    return games


def project_day(date_str: str, season: int, season_start: str, line: float,
                recent_days: int, use_savant: bool, max_workers: int) -> list[dict]:
    game_date = dt.date.fromisoformat(date_str)
    asof = (game_date - dt.timedelta(days=1)).isoformat()
    games = _final_games(date_str)
    if not games:
        return []

    # real lineups from each game's boxscore
    def _load_lineups(mu: D.Matchup):
        try:
            mu.home_lineup = D.get_lineup(mu.game_pk, "home")
            mu.away_lineup = D.get_lineup(mu.game_pk, "away")
        except Exception:
            mu.home_lineup, mu.away_lineup = [], []
    with ThreadPoolExecutor(max_workers=max_workers) as ex:
        list(ex.map(_load_lineups, games))

    pitchers = [getattr(mu, a) for mu in games
                for a in ("home_pitcher", "away_pitcher") if getattr(mu, a)]
    batters = [b for mu in games for b in (mu.home_lineup + mu.away_lineup)]

    def _do_pitcher(p: D.Pitcher):
        try:
            if not p.throws:
                p.throws = D.pitcher_throws(p.mlbam_id)
            fill_pitcher_asof(p, season, asof, season_start)
        except Exception:
            pass

    def _do_batter(b: D.Batter):
        try:
            if not b.bats:
                b.bats = D.batter_bats(b.mlbam_id)
            fill_batter_asof(b, season, asof, season_start)
            fill_recent_asof(b, season, game_date, recent_days)
        except Exception:
            pass

    with ThreadPoolExecutor(max_workers=max_workers) as ex:
        list(ex.map(_do_pitcher, pitchers))
        list(ex.map(_do_batter, batters))

    savant_bat = D.load_savant_expected(season, "batter") if use_savant else {}
    savant_pit = D.load_savant_expected(season, "pitcher") if use_savant else {}

    rows = []
    for mu in games:
        if not mu.home_pitcher or not mu.away_pitcher:
            continue
        for b in mu.away_lineup:
            r = dp.project_batter(b, mu.home_pitcher, mu.venue, savant_bat, savant_pit, False, True)
            if r:
                rows.append({**r, "game": f"{mu.away} @ {mu.home}", "id": b.mlbam_id})
        for b in mu.home_lineup:
            r = dp.project_batter(b, mu.away_pitcher, mu.venue, savant_bat, savant_pit, True, False)
            if r:
                rows.append({**r, "game": f"{mu.away} @ {mu.home}", "id": b.mlbam_id})

    # grade against actual total bases
    out = []
    for r in rows:
        actual = D.player_tb_on_date(r["id"], season, date_str)
        if actual is None:
            continue                      # DNP / scratched — drop, don't guess
        p_over = r["p_over"]
        out.append({
            "date": date_str,
            "batter": r["batter"],
            "batter_id": r["id"],
            "pitcher": r["pitcher"],
            "game": r["game"],
            "prop": "TB",
            "line": line,
            "proj": None,                 # daily_ping returns p_over, not lambda; kept for schema
            "p_over": round(float(p_over), 4),
            "actual": float(actual),
            "over_hit": int(actual > line),
            "pred_side": "Over" if p_over >= 0.5 else "Under",
            "correct": int((p_over >= 0.5) == (actual > line)),
            "conf": r.get("conf"),
            "graded": 1,
        })
    return out


# --------------------------------------------------------------------------- #
# Metrics                                                                      #
# --------------------------------------------------------------------------- #
def summarize(df: pd.DataFrame) -> None:
    import math
    n = len(df)
    print(f"\n{'='*60}\nBACKTEST SUMMARY  —  {n} graded legs\n{'='*60}")
    if n == 0:
        print("No graded legs. Widen the date range or check the season.")
        return

    p = df["p_over"].astype(float)
    y = df["over_hit"].astype(float)

    # directional accuracy vs a coinflip
    acc = float((df["pred_side"] == df.apply(
        lambda r: "Over" if r["over_hit"] else "Under", axis=1)).mean())
    # Brier + log-loss (probabilistic quality)
    brier = float(((p - y) ** 2).mean())
    eps = 1e-6
    logloss = float((-(y * (p.clip(eps, 1 - eps)).apply(math.log)
                       + (1 - y) * (1 - p.clip(eps, 1 - eps)).apply(math.log))).mean())
    base_rate = float(y.mean())

    print(f"Over rate (actual):      {base_rate*100:5.1f}%   (line {df['line'].iloc[0]})")
    print(f"Directional accuracy:    {acc*100:5.1f}%")
    print(f"Brier score:             {brier:.4f}   (lower better; 0.25 = coinflip)")
    print(f"Log-loss (raw):          {logloss:.4f}   (lower better)")

    # reliability diagram (reuse the tracker's binning)
    try:
        import tracker as T
        rel = T.calibration_by_probability(df)
        if rel is not None and not rel.empty:
            print("\nReliability (predicted P(Over) vs actual Over rate):")
            print(rel.to_string(index=False))
    except Exception as e:
        print(f"(reliability table skipped: {e})")

    # Platt calibration — how much would it help?
    try:
        import tracker as T
        fit = T.fit_platt_scaling(df)
        if fit.get("logloss") is not None:
            improv = fit["logloss_uncalibrated"] - fit["logloss"]
            print(f"\nPlatt calibration fit:   A={fit['A']}  B={fit['B']}  (n={fit['n']})")
            print(f"  log-loss raw {fit['logloss_uncalibrated']}  ->  calibrated {fit['logloss']}"
                  f"   ({improv:+.4f}, {'better' if improv>0 else 'no gain'})")
            print("  A<1 = model overconfident (compress).  B!=0 = systematic Over/Under skew.")
        else:
            print(f"\nPlatt: need >=30 legs to fit (have {fit.get('n', 0)}).")
    except Exception as e:
        print(f"(Platt fit skipped: {e})")


# --------------------------------------------------------------------------- #
# CLI                                                                         #
# --------------------------------------------------------------------------- #
def main():
    ap = argparse.ArgumentParser(description="Point-in-time historical backtest for the TB model.")
    ap.add_argument("--start", required=True, help="YYYY-MM-DD (first game date)")
    ap.add_argument("--end", required=True, help="YYYY-MM-DD (last game date, inclusive)")
    ap.add_argument("--season", type=int, default=None, help="stats season (defaults to start year)")
    ap.add_argument("--line", type=float, default=1.5, help="TB line to grade against (default 1.5)")
    ap.add_argument("--recent-days", type=int, default=21, help="recent-form window (default 21)")
    ap.add_argument("--savant", action="store_true", help="enable Statcast blend (adds mild look-ahead)")
    ap.add_argument("--max-workers", type=int, default=16)
    ap.add_argument("--out", default="backtest_results.csv")
    ap.add_argument("--write-log", action="store_true",
                    help="append graded rows into the tracker log (feeds the Calibration tab)")
    args = ap.parse_args()

    start = dt.date.fromisoformat(args.start)
    end = dt.date.fromisoformat(args.end)
    season = args.season or start.year
    season_start = f"{season}-03-01"

    # league baseline (constant, matches production build)
    lr = D.league_event_rates(season)
    if lr:
        E.LEAGUE_EVENT_RATES.update({k: lr[k] for k in ("1B", "2B", "3B", "HR") if k in lr})
        if "TB" in lr:
            E.LEAGUE_TB_PER_PA = lr["TB"]

    all_rows = []
    d = start
    while d <= end:
        ds = d.isoformat()
        try:
            day_rows = project_day(ds, season, season_start, args.line,
                                    args.recent_days, args.savant, args.max_workers)
            all_rows.extend(day_rows)
            print(f"{ds}: {len(day_rows):3d} graded legs   (running total {len(all_rows)})")
        except Exception as e:
            print(f"{ds}: ERROR {e}")
        d += dt.timedelta(days=1)

    df = pd.DataFrame(all_rows)
    df.to_csv(args.out, index=False)
    print(f"\nWrote {len(df)} rows -> {args.out}")
    summarize(df)

    if args.write_log and not df.empty:
        try:
            import tracker as T
            log = T.read_log()
            cols = list(log.columns) if not log.empty else [
                "date", "batter", "batter_id", "pitcher", "game", "prop", "line",
                "proj", "pred_side", "p_over", "actual", "actual_side", "over_hit",
                "correct", "graded"]
            add = df.copy()
            add["actual_side"] = add.apply(lambda r: "Over" if r["over_hit"] else "Under", axis=1)
            for c in cols:
                if c not in add.columns:
                    add[c] = None
            merged = pd.concat([log, add[cols]], ignore_index=True) if not log.empty else add[cols]
            merged = merged.drop_duplicates(subset=["date", "batter", "prop", "line"], keep="last")
            where = T.write_log(merged)
            print(f"Appended {len(add)} graded rows to tracker log ({where}).")
        except Exception as e:
            print(f"(write-log skipped: {e})")


if __name__ == "__main__":
    main()
# end of backtest.py
