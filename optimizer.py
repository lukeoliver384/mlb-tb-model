"""
Automated model optimizer — deterministic engine (no LLM, report/PR only).

WHY THIS EXISTS
---------------
The projection constants in `engine.py` / `scripts/daily_ping.py` (regression
strength, recent-form weight, bullpen rate, times-through-the-order shape) were
hand-tuned once from the source spreadsheet. As the graded log grows, some of
them are almost certainly no longer optimal. This tool re-fits them against real
history WITHOUT look-ahead leakage and WITHOUT ever changing live behaviour on
its own — it only writes a report + a machine-readable best-settings file. A
human (or the `optimizer-reviewer` agent) decides whether to open/merge a PR.

TWO STAGES (this is the whole design)
-------------------------------------
The expensive part of a constant sweep is the NETWORK: `backtest.py` re-fetches
every player's point-in-time stats from StatsAPI for every date. Re-fetching
that once per candidate parameter set would be unusable. So we split it:

  Stage 1 — `build`:  reconstruct every player's as-of stats + actual TB across
            a date range ONCE and pickle it to `.cache/optimizer_dataset_*.pkl`.
            This is the slow, network-bound step. Reuses `backtest.py`'s
            point-in-time reconstruction verbatim (no model logic duplicated).

  Stage 2 — `sweep`:  load the cached dataset and, for each candidate parameter
            combo, re-project + grade in PURE PYTHON (no network) by rebinding
            the module-level constants and re-running `daily_ping.project_batter`
            unchanged. Fast enough to sweep a real grid.

WALK-FORWARD / OUT-OF-SAMPLE (the guardrail that keeps this honest)
-------------------------------------------------------------------
Picking the grid point with the best log-loss over ALL the data overfits the
still-small graded sample. Instead we do expanding-window walk-forward
SELECTION: sort the dates, split into sequential folds, and for each fold k>=2,
select the best combo on folds 1..k-1 (in-sample) and score THAT combo on fold k
(out-of-sample). The pooled OOS score of the selected-each-step combo, compared
to baseline's pooled OOS score, is the honest "would re-tuning have helped?"
number. The recommended settings printed at the end are the full-sample best,
but the DECISION to trust them rests on the OOS delta, not the in-sample fit.

WHAT IS AND ISN'T SWEPT
-----------------------
The backtest only grades TOTAL BASES, so only TB-affecting knobs are swept:
  * REG_TB       — regression-toward-mean strength (PA), `daily_ping.REG_TB`
  * W_RECENT     — recent-form blend weight,          `daily_ping.W_RECENT`
  * BULLPEN_RATE — post-starter PA TB rate,           `daily_ping.BULLPEN_RATE`
  * tto_tb_scale — scales TTO_TB_MULT's deviations from 1.0 (lineup-turn shape)
Deliberately NOT swept here (documented so nobody thinks they were tuned):
  * HRR_DISPERSION / K_DISPERSION — only affect the H+R+RBI and K props, which
    this TB backtest never grades. Tuning them needs H+R+RBI / K backtests.
  * W_STATCAST — only bites with Savant on, which reintroduces look-ahead
    (Savant leaderboards are cumulative). Kept off = clean, so it's a no-op here.
Calibration (Platt A/B) is RE-FIT and reported for information only — never
auto-applied, per the standing preview-only calibration rule.

USAGE
-----
    python optimizer.py build  --start 2026-04-01 --end 2026-06-30
    python optimizer.py sweep  --start 2026-04-01 --end 2026-06-30
    python optimizer.py run    --start 2026-04-01 --end 2026-06-30   # build+sweep
Outputs: optimizer_report.md (human) and optimizer_best.json (machine).
"""
from __future__ import annotations

import argparse
import datetime as dt
import itertools
import json
import math
import os
import pickle
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass

import data as D
import engine as E

# reuse backtest.py's point-in-time reconstruction verbatim — no duplication
import backtest as BT
# daily_ping is on sys.path via backtest's import; grab the same module object
import daily_ping as dp  # noqa: E402  (path set up by `import backtest`)

CACHE_DIR = ".cache"
REPORT_PATH = "optimizer_report.md"
BEST_PATH = "optimizer_best.json"


# --------------------------------------------------------------------------- #
# Stage 1 — build the point-in-time dataset (slow, network, done once)         #
# --------------------------------------------------------------------------- #
@dataclass
class Leg:
    """One graded batter-vs-pitcher projection input, frozen point-in-time.

    Carries the reconstructed Batter/Pitcher objects (as-of game_date-1) plus
    the context needed to re-run `daily_ping.project_batter`, and the actual TB
    the batter recorded. Everything here is knowable at first pitch — no leak.
    """
    date: str
    game: str
    batter: "D.Batter"
    pitcher: "D.Pitcher"
    venue: str
    batter_is_home: bool
    pitcher_is_home: bool
    actual_tb: float


def _cache_path(start: str, end: str) -> str:
    return os.path.join(CACHE_DIR, f"optimizer_dataset_{start}_{end}.pkl")


def build_dataset(start: str, end: str, season: int, season_start: str,
                  recent_days: int, max_workers: int) -> dict:
    """Reconstruct every gradeable leg across [start, end] and cache to disk.

    Returns a dict: {legs: [Leg...], meta: {...}, league: {...}}. The league
    baseline captured here is replayed at sweep time so projections are
    deterministic regardless of when the sweep runs.
    """
    legs: list[Leg] = []
    d = dt.date.fromisoformat(start)
    end_d = dt.date.fromisoformat(end)

    while d <= end_d:
        ds = d.isoformat()
        asof = (d - dt.timedelta(days=1)).isoformat()
        try:
            games = BT._final_games(ds)
        except Exception as e:
            print(f"{ds}: schedule ERROR {e}")
            d += dt.timedelta(days=1)
            continue
        if not games:
            print(f"{ds}:   0 games")
            d += dt.timedelta(days=1)
            continue

        # real lineups from each game's boxscore (point-in-time truth)
        def _load_lineups(mu):
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

        def _do_pitcher(p):
            try:
                if not p.throws:
                    p.throws = D.pitcher_throws(p.mlbam_id)
                BT.fill_pitcher_asof(p, season, asof, season_start)
            except Exception:
                pass

        def _do_batter(b):
            try:
                if not b.bats:
                    b.bats = D.batter_bats(b.mlbam_id)
                BT.fill_batter_asof(b, season, asof, season_start)
                BT.fill_recent_asof(b, season, d, recent_days)
            except Exception:
                pass

        with ThreadPoolExecutor(max_workers=max_workers) as ex:
            list(ex.map(_do_pitcher, pitchers))
            list(ex.map(_do_batter, batters))

        day_legs = 0
        for mu in games:
            if not mu.home_pitcher or not mu.away_pitcher:
                continue
            pairs = [(b, mu.home_pitcher, False, True) for b in mu.away_lineup] + \
                    [(b, mu.away_pitcher, True, False) for b in mu.home_lineup]
            for b, opp, bhome, phome in pairs:
                actual = D.player_tb_on_date(b.mlbam_id, season, ds)
                if actual is None:          # DNP / scratched — drop, don't guess
                    continue
                legs.append(Leg(
                    date=ds, game=f"{mu.away} @ {mu.home}",
                    batter=b, pitcher=opp, venue=mu.venue,
                    batter_is_home=bhome, pitcher_is_home=phome,
                    actual_tb=float(actual),
                ))
                day_legs += 1
        print(f"{ds}: {day_legs:3d} legs   (total {len(legs)})")
        d += dt.timedelta(days=1)

    # capture the league baseline used for this season so the sweep is deterministic
    league = {}
    lr = D.league_event_rates(season)
    if lr:
        league = {"event_rates": {k: lr[k] for k in ("1B", "2B", "3B", "HR") if k in lr}}
        if "TB" in lr:
            league["tb_per_pa"] = lr["TB"]

    ds_obj = {
        "legs": legs,
        "league": league,
        "meta": {"start": start, "end": end, "season": season,
                 "recent_days": recent_days, "n_legs": len(legs)},
    }
    os.makedirs(CACHE_DIR, exist_ok=True)
    with open(_cache_path(start, end), "wb") as f:
        pickle.dump(ds_obj, f)
    print(f"\nCached {len(legs)} legs -> {_cache_path(start, end)}")
    return ds_obj


def load_dataset(start: str, end: str) -> dict:
    with open(_cache_path(start, end), "rb") as f:
        return pickle.load(f)


# --------------------------------------------------------------------------- #
# Stage 2 — cheap re-projection under a candidate parameter set (pure python)  #
# --------------------------------------------------------------------------- #
# The knobs we sweep, and the module attribute each rebinds. tto_tb_scale is
# special-cased (it reshapes engine.TTO_TB_MULT rather than being a scalar attr).
SWEEP_ATTRS = {
    "REG_TB": ("dp", "REG_TB"),
    "W_RECENT": ("dp", "W_RECENT"),
    "BULLPEN_RATE": ("dp", "BULLPEN_RATE"),
}


def _apply_params(params: dict, base_tto: dict) -> None:
    """Rebind live module constants to a candidate combo (mutates dp / engine)."""
    for name, (mod, attr) in SWEEP_ATTRS.items():
        if name in params:
            setattr(dp if mod == "dp" else E, attr, params[name])
    scale = params.get("tto_tb_scale", 1.0)
    # scale each TTO_TB_MULT's deviation from 1.0 (1.0 => unchanged; 0 => flat)
    E.TTO_TB_MULT = {k: 1.0 + (v - 1.0) * scale for k, v in base_tto.items()}


def _project_all(legs: list, params: dict, base_tto: dict) -> list:
    """P(Over) for every leg under `params`. Savant off (clean, no look-ahead)."""
    _apply_params(params, base_tto)
    out = []
    for lg in legs:
        try:
            r = dp.project_batter(lg.batter, lg.pitcher, lg.venue, {}, {},
                                  lg.batter_is_home, lg.pitcher_is_home)
            out.append(r["p_over"] if r else None)
        except Exception:
            out.append(None)
    return out


# --------------------------------------------------------------------------- #
# Metrics                                                                      #
# --------------------------------------------------------------------------- #
def _logloss(ps: list, ys: list, idx=None) -> float | None:
    idx = idx if idx is not None else range(len(ps))
    s, n = 0.0, 0
    for i in idx:
        p, y = ps[i], ys[i]
        if p is None:
            continue
        q = min(max(p, 1e-6), 1 - 1e-6)
        s += -(y * math.log(q) + (1 - y) * math.log(1 - q))
        n += 1
    return s / n if n else None


def _brier(ps: list, ys: list, idx=None) -> float | None:
    idx = idx if idx is not None else range(len(ps))
    s, n = 0.0, 0
    for i in idx:
        if ps[i] is None:
            continue
        s += (ps[i] - ys[i]) ** 2
        n += 1
    return s / n if n else None


def _accuracy(ps: list, ys: list, idx=None) -> float | None:
    idx = idx if idx is not None else range(len(ps))
    c, n = 0, 0
    for i in idx:
        if ps[i] is None:
            continue
        c += int((ps[i] >= 0.5) == (ys[i] == 1))
        n += 1
    return c / n if n else None


# --------------------------------------------------------------------------- #
# The sweep                                                                    #
# --------------------------------------------------------------------------- #
DEFAULT_GRID = {
    "REG_TB": [130, 175, 220],
    "W_RECENT": [0.20, 0.35, 0.50, 0.65],
    "BULLPEN_RATE": [0.320, 0.345, 0.370],
    "tto_tb_scale": [0.5, 1.0, 1.5],
}
# The current live values — the baseline every candidate is measured against.
BASELINE = {"REG_TB": 175, "W_RECENT": 0.35, "BULLPEN_RATE": 0.345, "tto_tb_scale": 1.0}


def _grid_combos(grid: dict) -> list[dict]:
    keys = list(grid)
    return [dict(zip(keys, vals)) for vals in itertools.product(*[grid[k] for k in keys])]


def _fold_bounds(dates_sorted: list[str], n_folds: int) -> list[tuple[int, int]]:
    """Contiguous [lo, hi) index ranges over the sorted unique-date leg order."""
    n = len(dates_sorted)
    step = max(1, n // n_folds)
    bounds, lo = [], 0
    for k in range(n_folds):
        hi = n if k == n_folds - 1 else min(n, lo + step)
        if lo < hi:
            bounds.append((lo, hi))
        lo = hi
    return bounds


def sweep(ds_obj: dict, grid: dict, n_folds: int, line: float) -> dict:
    """Project every leg under every combo once, then do walk-forward selection.

    Returns a report dict with: baseline metrics, full-sample best combo, and the
    honest out-of-sample delta of walk-forward re-tuning vs baseline.
    """
    legs = ds_obj["legs"]
    # legs are appended in date order already; keep that order for folds
    ys = [1 if lg.actual_tb > line else 0 for lg in legs]

    # replay the captured league baseline so projections are deterministic
    league = ds_obj.get("league") or {}
    if league.get("event_rates"):
        E.LEAGUE_EVENT_RATES.update(league["event_rates"])
    if league.get("tb_per_pa"):
        E.LEAGUE_TB_PER_PA = league["tb_per_pa"]
    base_tto = dict(E.TTO_TB_MULT)  # snapshot before we start rescaling it

    combos = _grid_combos(grid)
    if BASELINE not in combos:
        combos = [BASELINE] + combos
    print(f"Projecting {len(legs)} legs x {len(combos)} combos "
          f"({len(legs)*len(combos):,} projections)...")

    # p_over for every combo over every leg (the one expensive pure-python pass)
    combo_preds: list[tuple[dict, list]] = []
    for i, combo in enumerate(combos, 1):
        preds = _project_all(legs, combo, base_tto)
        combo_preds.append((combo, preds))
        if i % 10 == 0 or i == len(combos):
            print(f"  {i}/{len(combos)} combos projected")

    # restore live constants (don't leave the module mutated for anything after us)
    _apply_params(BASELINE, base_tto)

    def combo_key(c):
        return tuple(sorted(c.items()))

    baseline_preds = next(p for c, p in combo_preds if combo_key(c) == combo_key(BASELINE))

    # ---- full-sample metrics per combo (in-sample; for the recommendation) ----
    scored = []
    for combo, preds in combo_preds:
        scored.append({
            "params": combo,
            "logloss": _logloss(preds, ys),
            "brier": _brier(preds, ys),
            "accuracy": _accuracy(preds, ys),
        })
    scored_valid = [s for s in scored if s["logloss"] is not None]
    scored_valid.sort(key=lambda s: s["logloss"])
    full_best = scored_valid[0] if scored_valid else None
    baseline_row = next(s for s in scored if combo_key(s["params"]) == combo_key(BASELINE))

    # ---- walk-forward out-of-sample selection (the honest generalization test) ----
    bounds = _fold_bounds(list(range(len(legs))), n_folds)
    wf_selected_pred = [None] * len(legs)   # OOS preds of the each-step-selected combo
    wf_baseline_pred = [None] * len(legs)   # OOS preds of the fixed baseline
    wf_picks = []
    for k in range(1, len(bounds)):
        train_hi = bounds[k][0]             # everything before this fold = in-sample
        test_lo, test_hi = bounds[k]
        train_idx = list(range(0, train_hi))
        test_idx = list(range(test_lo, test_hi))
        # select best combo on the in-sample window
        best_c, best_ll = None, None
        for combo, preds in combo_preds:
            ll = _logloss(preds, ys, train_idx)
            if ll is not None and (best_ll is None or ll < best_ll):
                best_c, best_ll = combo, ll
        sel_preds = next(p for c, p in combo_preds if combo_key(c) == combo_key(best_c))
        for i in test_idx:
            wf_selected_pred[i] = sel_preds[i]
            wf_baseline_pred[i] = baseline_preds[i]
        wf_picks.append({"fold": k, "train_n": len(train_idx), "test_n": len(test_idx),
                         "selected": best_c})

    oos_selected_ll = _logloss(wf_selected_pred, ys)
    oos_baseline_ll = _logloss(wf_baseline_pred, ys)
    oos_delta = (oos_baseline_ll - oos_selected_ll
                 if (oos_selected_ll is not None and oos_baseline_ll is not None) else None)

    # ---- calibration re-fit (preview only, never applied) ----
    platt = _platt_from_preds(baseline_preds, ys)

    return {
        "meta": ds_obj["meta"],
        "line": line,
        "n_legs_graded": sum(1 for p in baseline_preds if p is not None),
        "baseline": baseline_row,
        "full_best": full_best,
        "top5": scored_valid[:5],
        "walk_forward": {
            "n_folds": len(bounds),
            "oos_baseline_logloss": oos_baseline_ll,
            "oos_selected_logloss": oos_selected_ll,
            "oos_delta": oos_delta,
            "picks": wf_picks,
        },
        "calibration": platt,
    }


def _platt_from_preds(preds: list, ys: list) -> dict:
    """Re-fit tracker's Platt scaling from raw preds (preview only)."""
    try:
        import pandas as pd
        import tracker as T
        rows = [{"p_over": p, "over_hit": y, "graded": 1}
                for p, y in zip(preds, ys) if p is not None]
        return T.fit_platt_scaling(pd.DataFrame(rows))
    except Exception as e:
        return {"error": str(e)}


# --------------------------------------------------------------------------- #
# Reporting                                                                    #
# --------------------------------------------------------------------------- #
def _fmt_params(p: dict) -> str:
    return ", ".join(f"{k}={p[k]}" for k in ("REG_TB", "W_RECENT", "BULLPEN_RATE", "tto_tb_scale") if k in p)


def write_report(res: dict) -> None:
    m = res["meta"]
    wf = res["walk_forward"]
    base, best = res["baseline"], res["full_best"]
    lines = []
    lines.append("# MLB TB Model — Optimizer Report")
    lines.append("")
    lines.append(f"- **Window:** {m['start']} → {m['end']}  (season {m['season']})")
    lines.append(f"- **Graded TB legs:** {res['n_legs_graded']}  (line {res['line']})")
    lines.append(f"- **Generated:** {dt.datetime.now().isoformat(timespec='seconds')}")
    lines.append("")
    lines.append("> Report only. Nothing here changes live model behaviour. "
                 "Calibration is re-fit for information and is **not** applied "
                 "(preview-only calibration rule). Only TB-affecting constants "
                 "are swept — see the engine docstring for what is deliberately "
                 "left out.")
    lines.append("")

    lines.append("## Verdict — did walk-forward re-tuning beat baseline out-of-sample?")
    lines.append("")
    if wf["oos_delta"] is None:
        lines.append("Not enough folds/data to compute an out-of-sample delta. "
                     "Widen the date range and re-run before trusting any change.")
    else:
        better = wf["oos_delta"] > 0
        lines.append(f"- OOS log-loss, fixed baseline: **{wf['oos_baseline_logloss']:.4f}**")
        lines.append(f"- OOS log-loss, walk-forward re-tuned: **{wf['oos_selected_logloss']:.4f}**")
        lines.append(f"- **OOS delta: {wf['oos_delta']:+.4f}** "
                     f"({'re-tuning generalizes — worth a PR' if better else 'NO out-of-sample gain — do not change constants'})")
        lines.append("")
        lines.append("_This is the number that matters. A better in-sample fit "
                     "(below) that does not show up here is overfitting._")
    lines.append("")

    lines.append("## Full-sample fit (in-sample — for direction, not decisions)")
    lines.append("")
    lines.append("| | Params | log-loss | Brier | Dir. acc |")
    lines.append("|---|---|---|---|---|")
    lines.append(f"| Baseline (live) | {_fmt_params(base['params'])} | "
                 f"{base['logloss']:.4f} | {base['brier']:.4f} | {base['accuracy']*100:.1f}% |")
    if best:
        lines.append(f"| Best in-sample | {_fmt_params(best['params'])} | "
                     f"{best['logloss']:.4f} | {best['brier']:.4f} | {best['accuracy']*100:.1f}% |")
    lines.append("")
    lines.append("Top 5 by in-sample log-loss:")
    lines.append("")
    lines.append("| Rank | Params | log-loss | Brier |")
    lines.append("|---|---|---|---|")
    for i, s in enumerate(res["top5"], 1):
        lines.append(f"| {i} | {_fmt_params(s['params'])} | {s['logloss']:.4f} | {s['brier']:.4f} |")
    lines.append("")

    lines.append("## Walk-forward fold picks")
    lines.append("")
    lines.append("| Fold | Train legs | Test legs | Selected on train |")
    lines.append("|---|---|---|---|")
    for p in wf["picks"]:
        lines.append(f"| {p['fold']} | {p['train_n']} | {p['test_n']} | {_fmt_params(p['selected'])} |")
    lines.append("")

    cal = res["calibration"]
    lines.append("## Calibration re-fit (preview only — NOT applied)")
    lines.append("")
    if cal.get("logloss") is not None:
        improv = cal["logloss_uncalibrated"] - cal["logloss"]
        lines.append(f"- Platt A={cal['A']}, B={cal['B']}  (n={cal['n']})")
        lines.append(f"- log-loss raw {cal['logloss_uncalibrated']} → calibrated {cal['logloss']} "
                     f"({improv:+.4f})")
        lines.append("- A<1 ⇒ model overconfident (compress); B≠0 ⇒ systematic Over/Under skew.")
    elif "error" in cal:
        lines.append(f"- (skipped: {cal['error']})")
    else:
        lines.append(f"- Need ≥30 graded legs to fit (have {cal.get('n', 0)}).")
    lines.append("")

    lines.append("## Recommendation")
    lines.append("")
    if best and wf["oos_delta"] and wf["oos_delta"] > 0 and _fmt_params(best["params"]) != _fmt_params(base["params"]):
        lines.append(f"Re-tuning showed a positive out-of-sample delta. Consider a PR moving "
                     f"the live constants toward: **{_fmt_params(best['params'])}**. "
                     f"Change `daily_ping.REG_TB`, `daily_ping.W_RECENT`, "
                     f"`daily_ping.BULLPEN_RATE`, and scale `engine.TTO_TB_MULT` accordingly. "
                     f"Keep the change small and re-validate next week.")
    else:
        lines.append("**Hold — do not change constants.** Either there was no "
                     "out-of-sample improvement, or the best combo equals the "
                     "current live values. The current settings stand.")
    lines.append("")

    with open(REPORT_PATH, "w", encoding="utf-8") as f:
        f.write("\n".join(lines))

    with open(BEST_PATH, "w", encoding="utf-8") as f:
        json.dump({
            "meta": res["meta"],
            "baseline": base["params"],
            "recommended": best["params"] if best else None,
            "oos_delta": wf["oos_delta"],
            "should_pr": bool(best and wf["oos_delta"] and wf["oos_delta"] > 0
                              and _fmt_params(best["params"]) != _fmt_params(base["params"])),
            "calibration_preview": res["calibration"],
        }, f, indent=2)
    print(f"\nWrote {REPORT_PATH} and {BEST_PATH}")


# --------------------------------------------------------------------------- #
# CLI                                                                          #
# --------------------------------------------------------------------------- #
def main():
    ap = argparse.ArgumentParser(description="Deterministic model optimizer (report/PR only).")
    ap.add_argument("cmd", choices=["build", "sweep", "run"],
                    help="build dataset / sweep cached dataset / both")
    ap.add_argument("--start", required=True, help="YYYY-MM-DD")
    ap.add_argument("--end", required=True, help="YYYY-MM-DD")
    ap.add_argument("--season", type=int, default=None)
    ap.add_argument("--line", type=float, default=1.5)
    ap.add_argument("--recent-days", type=int, default=21)
    ap.add_argument("--folds", type=int, default=5)
    ap.add_argument("--max-workers", type=int, default=16)
    args = ap.parse_args()

    season = args.season or dt.date.fromisoformat(args.start).year
    season_start = f"{season}-03-01"

    if args.cmd in ("build", "run"):
        # set the live league baseline for the reconstruction pass too
        lr = D.league_event_rates(season)
        if lr:
            E.LEAGUE_EVENT_RATES.update({k: lr[k] for k in ("1B", "2B", "3B", "HR") if k in lr})
            if "TB" in lr:
                E.LEAGUE_TB_PER_PA = lr["TB"]
        build_dataset(args.start, args.end, season, season_start,
                      args.recent_days, args.max_workers)

    if args.cmd in ("sweep", "run"):
        ds_obj = load_dataset(args.start, args.end)
        if not ds_obj["legs"]:
            print("No legs in dataset — nothing to sweep.")
            return
        res = sweep(ds_obj, DEFAULT_GRID, args.folds, args.line)
        write_report(res)


if __name__ == "__main__":
    main()
# end of optimizer.py
