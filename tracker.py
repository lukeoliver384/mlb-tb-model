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

LOG_COLUMNS = ["date", "batter", "batter_id", "pitcher", "venue", "line",
               "proj_tb", "p_over", "actual_tb", "over_hit", "graded"]
LOG_CSV = "tracker_log.csv"

BET_COLUMNS = ["date", "batter", "batter_id", "pitcher", "line", "side",
               "odds", "stake", "actual_tb", "result", "profit", "graded"]
BET_CSV = "tracker_bets.csv"


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
    ws = _gsheet()
    if ws:
        try:
            recs = ws.get_all_records()
            df = pd.DataFrame(recs)
            return df if not df.empty else pd.DataFrame(columns=LOG_COLUMNS)
        except Exception:
            pass
    if "tracker_log" in st.session_state:
        return st.session_state["tracker_log"].copy()
    if os.path.exists(LOG_CSV):
        try:
            return pd.read_csv(LOG_CSV)
        except Exception:
            pass
    return pd.DataFrame(columns=LOG_COLUMNS)


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
def log_projections(proj_df: pd.DataFrame, date_str: str) -> int:
    """Append today's projections (replacing any existing rows for this date)."""
    new = pd.DataFrame({
        "date": date_str,
        "batter": proj_df["Batter"],
        "batter_id": proj_df.get("_bid", 0),
        "pitcher": proj_df["vs Pitcher"],
        "venue": proj_df["Venue"],
        "line": proj_df["Line"],
        "proj_tb": proj_df["Proj TB"],
        "p_over": proj_df["P(Over)"],
        "actual_tb": None, "over_hit": None, "graded": 0,
    })
    log = read_log()
    if log.empty:
        out = new
    else:
        log = log[log["date"] != date_str]
        out = new if log.empty else pd.concat([log, new], ignore_index=True)
    write_log(out)
    return len(new)


def grade(season: int) -> int:
    """Fill actual TB for ungraded rows whose game date has passed. Returns # graded."""
    log = read_log()
    if log.empty:
        return 0
    today = dt.date.today().isoformat()
    n = 0
    for i, row in log.iterrows():
        if str(row.get("graded")) in ("1", "1.0", "True"):
            continue
        if str(row["date"]) >= today:
            continue
        try:
            bid = int(row["batter_id"])
        except (ValueError, TypeError):
            continue
        if not bid:
            continue
        actual = D.player_tb_on_date(bid, season, str(row["date"]))
        if actual is None:
            continue
        log.at[i, "actual_tb"] = actual
        log.at[i, "over_hit"] = int(actual > float(row["line"]))
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
    g["proj_tb"] = pd.to_numeric(g["proj_tb"], errors="coerce")
    g["actual_tb"] = pd.to_numeric(g["actual_tb"], errors="coerce")
    g["p_over"] = pd.to_numeric(g["p_over"], errors="coerce")
    g["over_hit"] = pd.to_numeric(g["over_hit"], errors="coerce")
    g = g.dropna(subset=["proj_tb", "actual_tb"])
    err = (g["proj_tb"] - g["actual_tb"])
    return {
        "n": len(g),
        "mae": err.abs().mean(),
        "bias": err.mean(),                       # + = over-projecting
        "over_rate_pred": g["p_over"].mean(),
        "over_rate_actual": g["over_hit"].mean(),
    }


def calibration(log: pd.DataFrame) -> pd.DataFrame:
    g = log[log["graded"].astype(str).isin(["1", "1.0", "True"])].copy()
    if g.empty:
        return pd.DataFrame()
    g["p_over"] = pd.to_numeric(g["p_over"], errors="coerce")
    g["over_hit"] = pd.to_numeric(g["over_hit"], errors="coerce")
    g = g.dropna(subset=["p_over", "over_hit"])
    bins = [0, .4, .45, .5, .55, .6, .65, 1.01]
    labels = ["<40%", "40-45%", "45-50%", "50-55%", "55-60%", "60-65%", "65%+"]
    g["bucket"] = pd.cut(g["p_over"], bins=bins, labels=labels, right=False)
    out = g.groupby("bucket", observed=True).agg(
        n=("over_hit", "size"),
        predicted=("p_over", "mean"),
        actual=("over_hit", "mean")).reset_index()
    out["gap"] = out["actual"] - out["predicted"]
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
            return pd.DataFrame(recs) if recs else pd.DataFrame(columns=BET_COLUMNS)
        except Exception:
            pass
    if "bet_log" in st.session_state:
        return st.session_state["bet_log"].copy()
    if os.path.exists(BET_CSV):
        try:
            return pd.read_csv(BET_CSV)
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
    for c in ("actual_tb", "result", "profit"):
        new[c] = None
    new["graded"] = 0
    existing = read_bets()
    out = new if existing.empty else pd.concat([existing, new], ignore_index=True)
    out = out.drop_duplicates(subset=["date", "batter", "side", "odds", "stake"], keep="last")
    write_bets(out)
    return len(new)


def grade_bets(season: int) -> int:
    bets = read_bets()
    if bets.empty:
        return 0
    today = dt.date.today().isoformat()
    n = 0
    for i, row in bets.iterrows():
        if str(row.get("graded")) in ("1", "1.0", "True"):
            continue
        if str(row["date"]) >= today:
            continue
        try:
            bid = int(row["batter_id"])
            line = float(row["line"]); odds = float(row["odds"]); stake = float(row["stake"])
        except (ValueError, TypeError):
            continue
        actual = D.player_tb_on_date(bid, season, str(row["date"])) if bid else None
        if actual is None:
            bets.at[i, "result"] = "void"; bets.at[i, "profit"] = 0.0
            bets.at[i, "actual_tb"] = ""; bets.at[i, "graded"] = 1; n += 1
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
        bets.at[i, "actual_tb"] = actual
        bets.at[i, "result"] = res
        bets.at[i, "profit"] = round(profit, 3)
        bets.at[i, "graded"] = 1
        n += 1
    if n:
        write_bets(bets)
    return n


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
