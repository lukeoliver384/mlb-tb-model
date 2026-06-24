"""
Accuracy tracker.

Logs each day's projections and auto-grades them against actual total bases from
the MLB API, so you can see projection error and calibration over time.

Storage is pluggable:
  * Google Sheet  — persistent + viewable. Add a service-account key to Streamlit
    secrets as [gcp_service_account] and (optionally) tracker_sheet_name.
  * Fallback      — session + a local CSV (works immediately, but the hosted app
    won't persist it across restarts). Add the Google Sheet to make it permanent.
"""

from __future__ import annotations

import datetime as dt
import os

import pandas as pd
import streamlit as st

import data as D
import engine as E

LOG_COLUMNS = ["date", "batter", "batter_id", "pitcher", "venue", "line", "prop",
               "proj", "pred_side", "p_over", "actual", "actual_side", "over_hit", "correct", "graded"]
LOG_CSV = "tracker_log.csv"


def _iso(x):
    """Normalize any date representation (incl. sheet-coerced M/D/YYYY) to YYYY-MM-DD."""
    try:
        return pd.to_datetime(x).date().isoformat()
    except Exception:
        return str(x)

BET_COLUMNS = ["date", "batter", "batter_id", "pitcher", "venue", "line", "side", "prop",
               "odds", "stake", "actual", "result", "profit", "close_odds", "graded"]
BET_CSV = "tracker_bets.csv"

ODDS_COLUMNS = ["date", "prop", "game", "batter", "line", "over", "under"]
ODDS_CSV = "tracker_odds.csv"


# --------------------------------------------------------------------------- #
# Storage backend                                                             #
# --------------------------------------------------------------------------- #
def _gsheet():
    """Return a gspread worksheet if credentials are configured, else None."""
    try:
        if "gcp_service_account" not in st.secrets:
            return None
        import gspread
        from google.oauth2.service_account import Credentials
        creds = Credentials.from_service_account_info(
            dict(st.secrets["gcp_service_account"]),
            scopes=["https://www.googleapis.com/auth/spreadsheets",
                    "https://www.googleapis.com/auth/drive"])
        gc = gspread.authorize(creds)
        name = st.secrets.get("tracker_sheet_name", "MLB TB Tracker")
        try:
            sh = gc.open(name)
        except Exception:
            sh = gc.create(name)
        ws = sh.sheet1
        if not ws.row_values(1):
            ws.append_row(LOG_COLUMNS)
        return ws
    except Exception:
        return None


def backend_name() -> str:
    return "Google Sheet" if _gsheet() else "local (session/CSV — not persistent)"


def read_log() -> pd.DataFrame:
    df = None
    ws = _gsheet()
    if ws:
        try:
            recs = ws.get_all_records()
            df = pd.DataFrame(recs)
        except Exception:
            df = None
    if df is None and "tracker_log" in st.session_state:
        df = st.session_state["tracker_log"].copy()
    if df is None and os.path.exists(LOG_CSV):
        try:
            df = pd.read_csv(LOG_CSV)
        except Exception:
            df = None
    if df is None or df.empty:
        return pd.DataFrame(columns=LOG_COLUMNS)
    return df.reindex(columns=LOG_COLUMNS)


def write_log(df: pd.DataFrame) -> str:
    df = df[LOG_COLUMNS]
    ws = _gsheet()
    if ws:
        try:
            ws.clear()
            body = [LOG_COLUMNS] + df.astype(object).where(pd.notna(df), "").values.tolist()
            ws.update(body)
            return "Google Sheet"
        except Exception:
            pass
    st.session_state["tracker_log"] = df.copy()
    try:
        df.to_csv(LOG_CSV, index=False)
    except Exception:
        pass
    return "local"


# --------------------------------------------------------------------------- #
# Logging + grading                                                           #
# --------------------------------------------------------------------------- #
def log_projections(proj_df: pd.DataFrame, date_str: str,
                    prop: str = "TB", proj_col: str = "Proj TB") -> int:
    """Append today's projections for this prop (replacing same date+prop rows)."""
    new = pd.DataFrame({
        "date": date_str,
        "batter": proj_df["Batter"],
        "batter_id": proj_df.get("_bid", 0),
        "pitcher": proj_df["vs Pitcher"],
        "venue": proj_df["Venue"],
        "line": proj_df["Line"],
        "prop": prop,
        "proj": proj_df[proj_col],
        "pred_side": ["Over" if float(po) > 0.5 else "Under" for po in proj_df["P(Over)"]],
        "p_over": proj_df["P(Over)"],
        "actual": None, "actual_side": None, "over_hit": None, "correct": None, "graded": 0,
    })
    log = read_log()
    if not log.empty:
        log = log[~((log["date"] == date_str) & (log["prop"] == prop))]
    out = new if log.empty else pd.concat([log, new], ignore_index=True)
    write_log(out)
    return len(new)


def grade(season: int) -> int:
    """Fill actual result for ungraded rows whose game date has passed (prop-aware)."""
    log = read_log()
    if log.empty:
        return 0
    log = log.astype(object)
    today = dt.date.today().isoformat()
    finals = {}
    n = 0
    for i, row in log.iterrows():
        if str(row.get("graded")) in ("1", "1.0", "True"):
            continue
        d = _iso(row["date"])
        if d > today:
            continue
        if d == today:  # only grade games already Final
            if d not in finals:
                finals[d] = D.final_venues(d)
            if str(row.get("venue", "")).strip() not in finals[d]:
                continue
        try:
            bid = int(row["batter_id"])
        except (ValueError, TypeError):
            continue
        if not bid:
            continue
        prop = str(row.get("prop") or "TB").upper()
        fn = D.player_hrr_on_date if prop == "HRR" else (D.player_k_on_date if prop == "K" else D.player_tb_on_date)
        actual = fn(bid, season, d)
        if actual is None:
            continue
        ln = float(row["line"])
        a_side = "Over" if actual > ln else "Under"
        pred = str(row.get("pred_side") or "")
        if pred not in ("Over", "Under"):
            try:
                pred = "Over" if float(row.get("p_over")) > 0.5 else "Under"
            except (ValueError, TypeError):
                pred = ""
        log.at[i, "actual"] = actual
        log.at[i, "actual_side"] = a_side
        log.at[i, "over_hit"] = int(actual > ln)
        log.at[i, "correct"] = 1 if pred == a_side else 0
        log.at[i, "graded"] = 1
        n += 1
    if n:
        write_log(log)
    return n


# --------------------------------------------------------------------------- #
# Accuracy metrics                                                            #
# --------------------------------------------------------------------------- #
def metrics(log: pd.DataFrame) -> dict:
    g = log[log["graded"].astype(str).isin(["1", "1.0", "True"])].copy()
    if g.empty:
        return {}
    g["proj"] = pd.to_numeric(g["proj"], errors="coerce")
    g["actual"] = pd.to_numeric(g["actual"], errors="coerce")
    g["p_over"] = pd.to_numeric(g["p_over"], errors="coerce")
    g["over_hit"] = pd.to_numeric(g["over_hit"], errors="coerce")
    g["line"] = pd.to_numeric(g["line"], errors="coerce")
    g = g.dropna(subset=["proj", "actual"])
    err = (g["proj"] - g["actual"])
    gg = g.dropna(subset=["line", "over_hit"])
    pred_over = gg["proj"] > gg["line"]
    actual_over = gg["over_hit"] > 0.5
    pred_acc = float((pred_over == actual_over).mean()) if len(gg) else None
    return {
        "n": len(g),
        "mae": err.abs().mean(),
        "bias": err.mean(),                       # + = over-projecting
        "pred_acc": pred_acc,                     # projection's over/under call vs actual
        "pred_n": int(len(gg)),
    }


def prediction_breakdown(log: pd.DataFrame) -> pd.DataFrame:
    """Directional accuracy: did the projection's over/under call (proj vs line)
    match the actual result? Broken out by what the model predicted."""
    g = log[log["graded"].astype(str).isin(["1", "1.0", "True"])].copy()
    if g.empty:
        return pd.DataFrame()
    g["proj"] = pd.to_numeric(g["proj"], errors="coerce")
    g["line"] = pd.to_numeric(g["line"], errors="coerce")
    g["over_hit"] = pd.to_numeric(g["over_hit"], errors="coerce")
    g = g.dropna(subset=["proj", "line", "over_hit"])
    if g.empty:
        return pd.DataFrame()
    g["pred"] = g.apply(lambda r: "Over" if r["proj"] > r["line"] else "Under", axis=1)
    g["correct"] = ((g["pred"] == "Over") == (g["over_hit"] > 0.5))
    rows = []
    for side in ("Over", "Under"):
        sub = g[g["pred"] == side]
        if len(sub):
            rows.append({"Prediction": side, "n": int(len(sub)),
                         "Correct": round(sub["correct"].mean() * 100)})
    rows.append({"Prediction": "All", "n": int(len(g)),
                 "Correct": round(g["correct"].mean() * 100)})
    return pd.DataFrame(rows)


def calibration_by_confidence(log: pd.DataFrame) -> pd.DataFrame:
    """Bucket graded picks by the model's confidence on its leaned side, and show
    the actual hit rate of that side. Tells you whether a '60%' really hits ~60%."""
    g = log[log["graded"].astype(str).isin(["1", "1.0", "True"])].copy()
    if g.empty:
        return pd.DataFrame()
    g["p"] = pd.to_numeric(g["p_over"], errors="coerce")
    g["oh"] = pd.to_numeric(g["over_hit"], errors="coerce")
    g = g.dropna(subset=["p", "oh"])
    if g.empty:
        return pd.DataFrame()
    g["conf"] = g["p"].apply(lambda p: max(p, 1 - p))           # confidence on leaned side
    g["hit"] = (g["p"] >= 0.5) == (g["oh"] > 0.5)               # did the leaned side win
    bins = [0.5, 0.55, 0.60, 0.65, 0.70, 1.01]
    labels = ["50-55%", "55-60%", "60-65%", "65-70%", "70%+"]
    g["bucket"] = pd.cut(g["conf"], bins=bins, labels=labels, right=False)
    out = g.groupby("bucket", observed=True).agg(
        n=("hit", "size"),
        confidence=("conf", "mean"),
        hit_rate=("hit", "mean")).reset_index()
    out["gap"] = out["hit_rate"] - out["confidence"]
    return out


# --------------------------------------------------------------------------- #
# Bet log (ROI / P&L)                                                         #
# --------------------------------------------------------------------------- #
def _bet_ws():
    ws = _gsheet()
    if not ws:
        return None
    try:
        sh = ws.spreadsheet
        try:
            bws = sh.worksheet("bets")
        except Exception:
            bws = sh.add_worksheet(title="bets", rows=2000, cols=len(BET_COLUMNS))
        if not bws.row_values(1):
            bws.append_row(BET_COLUMNS)
        return bws
    except Exception:
        return None


def read_bets() -> pd.DataFrame:
    ws = _bet_ws()
    if ws:
        try:
            recs = ws.get_all_records()
            return pd.DataFrame(recs).reindex(columns=BET_COLUMNS) if recs else pd.DataFrame(columns=BET_COLUMNS)
        except Exception:
            pass
    if "bet_log" in st.session_state:
        return st.session_state["bet_log"].copy().reindex(columns=BET_COLUMNS)
    if os.path.exists(BET_CSV):
        try:
            return pd.read_csv(BET_CSV).reindex(columns=BET_COLUMNS)
        except Exception:
            pass
    return pd.DataFrame(columns=BET_COLUMNS)


def write_bets(df: pd.DataFrame) -> str:
    df = df[BET_COLUMNS]
    ws = _bet_ws()
    if ws:
        try:
            ws.clear()
            ws.update([BET_COLUMNS] + df.astype(object).where(pd.notna(df), "").values.tolist())
            return "Google Sheet"
        except Exception:
            pass
    st.session_state["bet_log"] = df.copy()
    try:
        df.to_csv(BET_CSV, index=False)
    except Exception:
        pass
    return "local"


def log_bets(bets_df: pd.DataFrame) -> int:
    """Append bets (date,batter,batter_id,pitcher,line,side,odds,stake). Dedups exact repeats."""
    new = bets_df.copy()
    for c in ("actual", "result", "profit"):
        new[c] = None
    new["graded"] = 0
    existing = read_bets()
    out = new if existing.empty else pd.concat([existing, new], ignore_index=True)
    out = out.drop_duplicates(subset=["date", "batter", "side", "odds", "stake"], keep="last")
    out = out.reindex(columns=BET_COLUMNS)
    write_bets(out)
    return len(new)


def grade_bets(season: int) -> int:
    bets = read_bets()
    if bets.empty:
        return 0
    bets = bets.astype(object)
    today = dt.date.today().isoformat()
    finals = {}
    n = 0
    for i, row in bets.iterrows():
        if str(row.get("graded")) in ("1", "1.0", "True"):
            continue
        d = _iso(row["date"])
        if d > today:
            continue
        if d == today:
            if d not in finals:
                finals[d] = D.final_venues(d)
            if str(row.get("venue", "")).strip() not in finals[d]:
                continue
        try:
            bid = int(row["batter_id"])
            line = float(row["line"]); odds = float(row["odds"]); stake = float(row["stake"])
        except (ValueError, TypeError):
            continue
        prop = str(row.get("prop") or "TB").upper()
        fn = D.player_hrr_on_date if prop == "HRR" else (D.player_k_on_date if prop == "K" else D.player_tb_on_date)
        actual = fn(bid, season, d) if bid else None
        if actual is None:
            bets.at[i, "result"] = "void"; bets.at[i, "profit"] = 0.0
            bets.at[i, "actual"] = ""; bets.at[i, "graded"] = 1; n += 1
            continue
        side = str(row["side"]).lower()
        won = (actual > line) if side == "over" else (actual < line)
        push = (actual == line)
        if push:
            profit, res = 0.0, "push"
        elif won:
            profit, res = stake * E.american_to_decimal_profit(odds), "win"
        else:
            profit, res = -stake, "loss"
        bets.at[i, "actual"] = actual
        bets.at[i, "result"] = res
        bets.at[i, "profit"] = round(profit, 3)
        bets.at[i, "graded"] = 1
        n += 1
    if n:
        write_bets(bets)
    return n


def _american_to_decimal(a):
    try:
        v = float(str(a).replace("+", "").strip())
    except (TypeError, ValueError):
        return None
    if v == 0:
        return None
    return 1 + v / 100.0 if v > 0 else 1 + 100.0 / abs(v)


def clv_metrics(bets: pd.DataFrame) -> dict:
    """CLV per bet from the price you took vs the closing price of that side:
    CLV% = entry_decimal / closing_decimal - 1 (positive = you beat the close).
    Only bets with a closing price entered are counted."""
    if bets is None or bets.empty:
        return {"n": 0, "avg_clv": 0.0, "pos_rate": 0.0}
    clvs = []
    for _, r in bets.iterrows():
        ed = _american_to_decimal(r.get("odds"))
        cd = _american_to_decimal(r.get("close_odds"))
        if ed and cd:
            clvs.append(ed / cd - 1.0)
    if not clvs:
        return {"n": 0, "avg_clv": 0.0, "pos_rate": 0.0}
    return {"n": len(clvs),
            "avg_clv": sum(clvs) / len(clvs),
            "pos_rate": sum(1 for x in clvs if x > 0) / len(clvs)}


def bet_metrics(bets: pd.DataFrame) -> dict:
    g = bets[bets["graded"].astype(str).isin(["1", "1.0", "True"])].copy()
    if g.empty:
        return {}
    g["stake"] = pd.to_numeric(g["stake"], errors="coerce")
    g["profit"] = pd.to_numeric(g["profit"], errors="coerce")
    g = g.dropna(subset=["stake", "profit"])
    wins = (g["result"] == "win").sum()
    losses = (g["result"] == "loss").sum()
    staked = g["stake"].sum()
    profit = g["profit"].sum()
    return {
        "n": len(g),
        "record": f"{int(wins)}-{int(losses)}" + (f"-{int((g['result']=='push').sum())}" if (g['result']=='push').any() else ""),
        "win_rate": wins / (wins + losses) if (wins + losses) else 0.0,
        "units_staked": staked,
        "units_profit": profit,
        "roi": profit / staked if staked else 0.0,
    }


def bankroll_curve(bets: pd.DataFrame, start: float = 100.0) -> pd.DataFrame:
    """
    Running bankroll from graded bets as a UNIT ledger: bankroll = start + the
    running sum of profit (units), using the actual stake you logged per bet.
    profit is stake*payout on a win, -stake on a loss (computed at grade time).
    Returns columns: n, date, bankroll.
    """
    g = bets[bets["graded"].astype(str).isin(["1", "1.0", "True"])].copy()
    if g.empty:
        return pd.DataFrame(columns=["n", "date", "bankroll"])
    g["profit"] = pd.to_numeric(g["profit"], errors="coerce").fillna(0.0)
    g = g.sort_values("date").reset_index(drop=True)
    rows, bk = [], float(start)
    for i, r in g.iterrows():
        bk += float(r["profit"])
        rows.append({"n": i + 1, "date": r["date"], "bankroll": round(bk, 3)})
    return pd.DataFrame(rows)


def bankroll_stats(curve: pd.DataFrame, start: float = 100.0) -> dict:
    if curve.empty:
        return {}
    bk = curve["bankroll"]
    peak = bk.cummax()
    dd = ((bk - peak) / peak)
    cur = bk.iloc[-1]
    return {
        "current": cur,
        "growth_pct": (cur / start - 1) * 100,
        "peak": bk.max(),
        "max_drawdown_pct": dd.min() * 100,  # most negative
    }


# --------------------------------------------------------------------------- #
# Odds persistence (survives tab refresh / restart)                           #
# --------------------------------------------------------------------------- #
def _odds_ws():
    ws = _gsheet()
    if not ws:
        return None
    try:
        sh = ws.spreadsheet
        try:
            ows = sh.worksheet("odds")
        except Exception:
            ows = sh.add_worksheet(title="odds", rows=4000, cols=len(ODDS_COLUMNS))
        if not ows.row_values(1):
            ows.append_row(ODDS_COLUMNS)
        return ows
    except Exception:
        return None


def read_odds() -> dict:
    """Return {(date, prop, game, batter): {"over":..,"under":..}} from storage."""
    rows = None
    ws = _odds_ws()
    if ws:
        try:
            rows = ws.get_all_records()
        except Exception:
            rows = None
    if rows is None and "odds_rows" in st.session_state:
        rows = st.session_state["odds_rows"]
    if rows is None and os.path.exists(ODDS_CSV):
        try:
            rows = pd.read_csv(ODDS_CSV).to_dict("records")
        except Exception:
            rows = None
    out = {}
    for r in (rows or []):
        k = (str(r.get("date", "")), str(r.get("prop", "")),
             str(r.get("game", "")), str(r.get("batter", "")))
        rec = {}
        if str(r.get("line", "")).strip():
            rec["line"] = str(r["line"]).strip()
        if str(r.get("over", "")).strip():
            rec["over"] = str(r["over"]).strip()
        if str(r.get("under", "")).strip():
            rec["under"] = str(r["under"]).strip()
        if rec:
            out[k] = rec
    return out


def write_odds(store: dict) -> str:
    rows = [[d, prop, game, batter, rec.get("line", ""), rec.get("over", ""), rec.get("under", "")]
            for (d, prop, game, batter), rec in store.items()]
    ws = _odds_ws()
    if ws:
        try:
            ws.clear()
            ws.update([ODDS_COLUMNS] + rows)
            return "Google Sheet"
        except Exception:
            pass
    st.session_state["odds_rows"] = [dict(zip(ODDS_COLUMNS, r)) for r in rows]
    try:
        pd.DataFrame(rows, columns=ODDS_COLUMNS).to_csv(ODDS_CSV, index=False)
    except Exception:
        pass
    return "local"


def reset_grades():
    """Clear all grade results so they re-grade with the current logic/data source."""
    log = read_log()
    if not log.empty:
        log = log.astype(object)
        log["graded"] = 0; log["actual"] = None; log["over_hit"] = None
        write_log(log)
    bets = read_bets()
    if not bets.empty:
        bets = bets.astype(object)
        bets["graded"] = 0; bets["actual"] = None; bets["result"] = None; bets["profit"] = None
        write_bets(bets)


def avg_realized_odds(bets):
    """Average price you actually got, computed in implied-probability space (robust to
    longshots), returned as American odds. None if no priced bets."""
    if bets is None or bets.empty:
        return None
    decs = [_american_to_decimal(o) for o in bets.get("odds", [])]
    decs = [d for d in decs if d]
    if not decs:
        return None
    avg_p = sum(1.0 / d for d in decs) / len(decs)
    dec = 1.0 / avg_p
    amer = round((dec - 1) * 100) if dec >= 2 else round(-100.0 / (dec - 1))
    return int(amer)


def paper_sim(log, odds=-110, only_plus_ev=True, start_units=100.0, odds_lookup=None,
              real_only=False, stake_mode="flat", kelly_mult=0.25, max_frac=0.10):
    """Hypothetical paper bankroll betting the model's lean on graded projections.

      odds_lookup : {(iso_date, PROP, batter): {over, under}} -> use your REAL entered
                    price per pick for its leaned side; else fall back to `odds`.
      real_only   : with odds_lookup, skip picks you never priced (don't use fallback).
      stake_mode  : "flat" = 1 unit each (best for measuring edge); "kelly" = compounding
                    fractional-Kelly stakes (realistic bankroll growth).

    Returns (summary, curve). summary: n, wins, hit_rate, breakeven(avg), roi
    (profit/total staked), growth (final/start-1), profit, final, n_real.
    """
    import pandas as pd
    empty = pd.DataFrame(columns=["n", "bankroll"])
    if log is None or log.empty:
        return {"n": 0}, empty
    g = log[log["graded"].astype(str).isin(["1", "1.0", "True"])].copy()
    g["p"] = pd.to_numeric(g["p_over"], errors="coerce")
    g["oh"] = pd.to_numeric(g["over_hit"], errors="coerce")
    g = g.dropna(subset=["p", "oh"])
    if g.empty:
        return {"n": 0}, empty
    assumed = _american_to_decimal(odds)
    if not assumed:
        return {"n": 0}, empty
    i = wins = n_real = 0
    profit = staked = be_sum = 0.0
    bk = float(start_units)
    curve = []
    for _, r in g.iterrows():
        p = float(r["p"]); oh = float(r["oh"])
        conf = max(p, 1 - p); lean_over = p >= 0.5
        dec = None
        if odds_lookup:
            key = (_iso(r.get("date")), str(r.get("prop", "")).upper(), str(r.get("batter", "")).strip())
            rec = odds_lookup.get(key)
            if rec:
                dec = _american_to_decimal(rec.get("over") if lean_over else rec.get("under"))
        used_real = dec is not None
        if dec is None:
            if real_only:
                continue
            dec = assumed
        be = 1.0 / dec
        if only_plus_ev and conf < be:
            continue
        win = (lean_over) == (oh > 0.5)
        if stake_mode == "kelly":
            b = dec - 1.0
            f = ((b * conf - (1 - conf)) / b) if b > 0 else 0.0
            f = min(max(0.0, f) * kelly_mult, max_frac)
            stake = f * bk
        else:
            stake = 1.0
        step = stake * (dec - 1.0) if win else -stake
        i += 1
        wins += 1 if win else 0
        n_real += 1 if used_real else 0
        be_sum += be
        staked += stake
        profit += step
        bk += step
        curve.append({"n": i, "bankroll": round(bk, 2)})
    if i == 0:
        return {"n": 0, "breakeven": 1.0 / assumed}, empty
    return ({"n": i, "wins": wins, "hit_rate": wins / i, "breakeven": be_sum / i,
             "roi": (profit / staked) if staked else 0.0,
             "growth": (bk / start_units - 1.0), "profit": profit, "final": bk,
             "n_real": n_real, "odds": odds, "stake_mode": stake_mode},
            pd.DataFrame(curve))


def calibration_temperature(log: pd.DataFrame):
    """
    Fit a probability 'temperature' from graded projections to correct over/under-
    confidence. T>1 = model overconfident (compress toward 50%); T<1 = underconfident.
    Heavily regularized toward T=1 (no change) until enough resolved legs accumulate,
    so early/small samples barely move it. Returns (T_effective, n_resolved).
    """
    import math
    if log is None or log.empty:
        return 1.0, 0
    g = log[log["graded"].astype(str).isin(["1", "1.0", "True"])].copy()
    g["p"] = pd.to_numeric(g["p_over"], errors="coerce")
    g["y"] = pd.to_numeric(g["over_hit"], errors="coerce")
    g = g.dropna(subset=["p", "y"])
    n = len(g)
    if n < 30:
        return 1.0, n
    ps = [min(max(float(p), 1e-4), 1 - 1e-4) for p in g["p"]]
    ys = [float(y) for y in g["y"]]

    def logloss(T):
        s = 0.0
        for p, y in zip(ps, ys):
            lp = math.log(p / (1 - p)) / T
            q = 1.0 / (1.0 + math.exp(-lp))
            q = min(max(q, 1e-6), 1 - 1e-6)
            s += -(y * math.log(q) + (1 - y) * math.log(1 - q))
        return s / len(ps)

    best_T, best_ll = 1.0, logloss(1.0)
    T = 0.5
    while T <= 3.0001:
        ll = logloss(T)
        if ll < best_ll:
            best_ll, best_T = ll, T
        T += 0.05
    # regularize toward 1.0 by sample size (full weight ~200 resolved legs)
    w = min(1.0, n / 200.0)
    T_eff = 1.0 + (best_T - 1.0) * w
    return round(T_eff, 3), n


# --------------------------------------------------------------------------- #
# Persistent settings (e.g. starting bankroll)                                #
# --------------------------------------------------------------------------- #
SETTINGS_COLUMNS = ["key", "value"]


def _settings_ws():
    ws = _gsheet()
    if not ws:
        return None
    try:
        sh = ws.spreadsheet
        try:
            sws = sh.worksheet("settings")
        except Exception:
            sws = sh.add_worksheet(title="settings", rows=50, cols=2)
        if not sws.row_values(1):
            sws.append_row(SETTINGS_COLUMNS)
        return sws
    except Exception:
        return None


def get_setting(key, default=None):
    skey = f"_set_{key}"
    if skey in st.session_state:
        return st.session_state[skey]
    ws = _settings_ws()
    try:
        if ws:
            for r in ws.get_all_records():
                if str(r.get("key")) == key:
                    st.session_state[skey] = r.get("value")
                    return r.get("value")
    except Exception:
        pass
    return default


def set_setting(key, value):
    st.session_state[f"_set_{key}"] = value
    ws = _settings_ws()
    if ws:
        try:
            recs = ws.get_all_records()
            d = {str(r.get("key")): r.get("value") for r in recs}
            d[str(key)] = value
            ws.clear()
            ws.update([SETTINGS_COLUMNS] + [[k, v] for k, v in d.items()])
        except Exception:
            pass
