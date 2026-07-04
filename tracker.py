"""
Accuracy tracker.

Logs each day's projections and auto-grades them against actual total bases from
the MLB API, so you can see projection error and calibration over time.

Storage is pluggable:
  * Google Sheet  — persistent + viewable, and the only tier that survives a
    Streamlit Cloud reboot (its filesystem is ephemeral). Add a service-account
    key to Streamlit secrets as [gcp_service_account] and (optionally)
    tracker_sheet_name.
  * SQLite        — local fallback (tracker.db, via stdlib sqlite3 + pandas'
    to_sql/read_sql — no new dependency). Used automatically when no Google
    Sheet is configured, e.g. running locally. One-time-migrates any existing
    tracker_*.csv files on first use so earlier local runs aren't lost.
"""

from __future__ import annotations

import datetime as dt
import os
import sqlite3

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
# SQLite backend (local fallback when no Google Sheet is configured)          #
# --------------------------------------------------------------------------- #
DB_PATH = "tracker.db"
_CSV_MIGRATE_MAP = {"log": (LOG_CSV, LOG_COLUMNS), "bets": (BET_CSV, BET_COLUMNS),
                     "odds": (ODDS_CSV, ODDS_COLUMNS)}


def _sqlite_conn():
    return sqlite3.connect(DB_PATH)


def _sqlite_table_exists(conn, table):
    cur = conn.execute("SELECT name FROM sqlite_master WHERE type='table' AND name=?", (table,))
    return cur.fetchone() is not None


def _sqlite_read(table, columns):
    """Read a table, migrating a legacy CSV of the same name in on first use."""
    try:
        with _sqlite_conn() as conn:
            if not _sqlite_table_exists(conn, table):
                csv_path, _ = _CSV_MIGRATE_MAP.get(table, (None, None))
                if csv_path and os.path.exists(csv_path):
                    try:
                        pd.read_csv(csv_path).reindex(columns=columns).to_sql(table, conn, index=False)
                    except Exception:
                        return None
                else:
                    return None
            df = pd.read_sql(f"SELECT * FROM {table}", conn)
        return df.reindex(columns=columns)
    except Exception:
        return None


def _sqlite_write(table, df):
    try:
        with _sqlite_conn() as conn:
            df.to_sql(table, conn, if_exists="replace", index=False)
        return True
    except Exception:
        return False


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
    return "Google Sheet" if _gsheet() else "SQLite (local — not persistent on Streamlit Cloud)"


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
    if df is None:
        df = _sqlite_read("log", LOG_COLUMNS)
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
    if _sqlite_write("log", df):
        return "SQLite"
    return "session-only"


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


def bulk_log(new_df) -> int:
    """One-shot projection logger: write many (date, prop) groups in a SINGLE sheet
    rewrite (avoids per-day rate-limit stalls). new_df needs at least date, batter,
    batter_id, pitcher, venue, line, prop, proj, pred_side, p_over."""
    if new_df is None or len(new_df) == 0:
        return 0
    new_df = new_df.copy()
    for c in ("actual", "actual_side", "over_hit", "correct"):
        if c not in new_df.columns:
            new_df[c] = None
    if "graded" not in new_df.columns:
        new_df["graded"] = 0
    new_df = new_df.reindex(columns=LOG_COLUMNS)
    log = read_log()
    if log is not None and not log.empty:
        pairs = set(zip(new_df["date"].astype(str), new_df["prop"].astype(str)))
        keep = ~log.apply(lambda r: (str(r["date"]), str(r["prop"])) in pairs, axis=1)
        log = log[keep]
    out = new_df if (log is None or log.empty) else pd.concat([log, new_df], ignore_index=True)
    write_log(out)
    return int(len(new_df))


def grade_diagnostic(season: int) -> dict:
    """Walk every UNGRADED row and tally why it would/wouldn't grade right now.
    Helps diagnose '0 graded' issues. Returns a dict of reason -> count (per prop too)."""
    from collections import Counter
    log = read_log()
    if log is None or log.empty:
        return {"(log empty)": 0}
    today = dt.date.today().isoformat()
    c = Counter()
    cp = Counter()
    finals = {}
    for _, row in log.iterrows():
        if str(row.get("graded")) in ("1", "1.0", "True"):
            continue
        prop = str(row.get("prop") or "TB").upper() or "(blank)"
        d = _iso(row["date"])
        if d > today:
            c["future_date"] += 1; continue
        if d == today:
            if d not in finals:
                try:
                    finals[d] = D.final_venues(d)
                except Exception:
                    finals[d] = set()
            if str(row.get("venue", "")).strip() not in finals[d]:
                c["today_not_final"] += 1; continue
        try:
            bid = int(row["batter_id"])
        except (ValueError, TypeError):
            bid = 0
        if not bid:
            c["no_batter_id"] += 1; cp[f"{prop}:no_batter_id"] += 1; continue
        fn2 = D.player_hrr_on_date if prop == "HRR" else (D.player_k_on_date if prop == "K" else D.player_tb_on_date)
        try:
            actual = fn2(bid, season, d)
        except Exception:
            c["fetch_error"] += 1; cp[f"{prop}:fetch_error"] += 1; continue
        if actual is None:
            c["no_result_found"] += 1; cp[f"{prop}:no_result_found"] += 1; continue
        try:
            float(row["line"])
        except (ValueError, TypeError):
            c["bad_or_blank_line"] += 1; cp[f"{prop}:bad_line"] += 1; continue
        c["GRADEABLE_now"] += 1; cp[f"{prop}:gradeable"] += 1
    out = dict(c)
    out["_by_prop"] = dict(cp)
    # date distribution of ALL ungraded rows (normalized) + today for reference
    ud = Counter()
    for _, row in log.iterrows():
        if str(row.get("graded")) in ("1", "1.0", "True"):
            continue
        ud[_iso(row["date"])] += 1
    out["_ungraded_by_date"] = dict(sorted(ud.items()))
    # raw date values for the first several ungraded rows: raw repr -> _iso -> ==today?
    samples = []
    for _, row in log.iterrows():
        if str(row.get("graded")) in ("1", "1.0", "True"):
            continue
        raw = row.get("date")
        samples.append({"raw": repr(raw), "type": type(raw).__name__,
                        "iso": _iso(raw), "is_today": _iso(raw) == today,
                        "prop": str(row.get("prop")), "bid": repr(row.get("batter_id"))})
        if len(samples) >= 8:
            break
    out["_date_samples"] = samples
    # today-grading check: which venues are Final vs which today rows are waiting
    try:
        out["_today_final_venues"] = sorted(v for v in D.final_venues(today) if v)
    except Exception as e:
        out["_today_final_venues"] = f"error: {e}"
    tv = set()
    for _, row in log.iterrows():
        if str(row.get("graded")) in ("1", "1.0", "True"):
            continue
        if _iso(row["date"]) == today:
            tv.add(str(row.get("venue", "")).strip())
    out["_today_ungraded_venues"] = sorted(tv)
    out["_today"] = today
    out["_total_rows"] = int(len(log))
    return out


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
        try:
            actual = fn(bid, season, d)
            if actual is None:
                continue
            ln = float(row["line"])           # blank/None line -> skip this row, not the whole run
        except (ValueError, TypeError):
            continue
        except Exception:
            continue
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


def calibration_by_probability(log: pd.DataFrame, temp: float = 1.0) -> pd.DataFrame:
    """Reliability diagram by RAW model probability P(over). If temp>1, also shows the
    'calibrated' column = each row's P(over) compressed by that temperature, and gap_cal
    = actual over rate minus the calibrated prediction. Lets you preview a temperature
    against your whole history (the live cumulative table only reflects it on new rows)."""
    import math
    g = log[log["graded"].astype(str).isin(["1", "1.0", "True"])].copy()
    if g.empty:
        return pd.DataFrame()
    g["p"] = pd.to_numeric(g["p_over"], errors="coerce")
    g["oh"] = pd.to_numeric(g["over_hit"], errors="coerce")
    g = g.dropna(subset=["p", "oh"])
    if g.empty:
        return pd.DataFrame()

    def _cal(p):
        if temp == 1.0 or not (0 < p < 1):
            return p
        return 1.0 / (1.0 + math.exp(-(math.log(p / (1 - p)) / temp)))
    g["pc"] = g["p"].apply(_cal)
    bins = [0.0, 0.1, 0.2, 0.3, 0.4, 0.5, 0.6, 0.7, 0.8, 0.9, 1.01]
    labels = ["0-10%", "10-20%", "20-30%", "30-40%", "40-50%",
              "50-60%", "60-70%", "70-80%", "80-90%", "90-100%"]
    g["bucket"] = pd.cut(g["p"], bins=bins, labels=labels, right=False)
    out = g.groupby("bucket", observed=True).agg(
        n=("oh", "size"),
        predicted=("p", "mean"),
        calibrated=("pc", "mean"),
        over_rate=("oh", "mean")).reset_index()
    out["gap"] = out["over_rate"] - out["predicted"]
    out["gap_cal"] = out["over_rate"] - out["calibrated"]
    return out


# Shared confidence-bucket definition. The projections board, the edges table,
# and the calibration report all key off these SAME edges so a pick and its
# calibration bucket can never disagree. Buckets are on the leaned-side
# confidence, max(p, 1-p), and are left-inclusive: 0.50<=x<0.55, ... 0.70<=x.
CONF_BINS = [0.5, 0.55, 0.60, 0.65, 0.70, 1.01]
CONF_LABELS = ["50-55%", "55-60%", "60-65%", "65-70%", "70%+"]


def confidence_band(p) -> str:
    """Bucket label for a pick's model probability, using the leaned-side
    confidence max(p, 1-p). Returns '' if p isn't a usable number."""
    try:
        p = float(p)
    except (TypeError, ValueError):
        return ""
    if pd.isna(p):
        return ""
    conf = max(p, 1 - p)
    for lo, hi, label in zip(CONF_BINS[:-1], CONF_BINS[1:], CONF_LABELS):
        if lo <= conf < hi:
            return label
    return CONF_LABELS[-1] if conf >= CONF_BINS[-2] else ""


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
    g["bucket"] = pd.cut(g["conf"], bins=CONF_BINS, labels=CONF_LABELS, right=False)
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
    df = _sqlite_read("bets", BET_COLUMNS)
    if df is not None:
        return df
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
    if _sqlite_write("bets", df):
        return "SQLite"
    return "session-only"


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
    if rows is None:
        df = _sqlite_read("odds", ODDS_COLUMNS)
        if df is not None:
            rows = df.to_dict("records")
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
    if _sqlite_write("odds", pd.DataFrame(rows, columns=ODDS_COLUMNS)):
        return "SQLite"
    return "session-only"


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
              real_only=False, stake_mode="flat", kelly_mult=0.25, max_frac=0.10,
              temp=1.0, temp_map=None, mode="lean"):
    """Paper bankroll over graded projections.
      mode="lean"  -> bet the model's leaned side (P(over) vs 50%), +EV-filtered.
      mode="value" -> bet whichever side is +EV at the entered price (over OR under),
                      regardless of the lean. Requires entered odds (uses odds_lookup).
    Returns (summary, curve)."""
    import pandas as pd
    import math as _m
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

    def _cal(pp, t):
        if not t or t == 1.0 or not (0 < pp < 1):
            return pp
        return 1.0 / (1.0 + _m.exp(-(_m.log(pp / (1 - pp)) / t)))

    i = wins = n_real = 0
    profit = staked = be_sum = 0.0
    bk = float(start_units)
    curve = []
    for _, r in g.iterrows():
        p = float(r["p"]); oh = float(r["oh"])
        prop_u = str(r.get("prop", "")).upper()
        rec = odds_lookup.get((_iso(r.get("date")), prop_u, str(r.get("batter", "")).strip())) if odds_lookup else None

        if mode == "value":
            do = _american_to_decimal(rec.get("over")) if rec else None
            du = _american_to_decimal(rec.get("under")) if rec else None
            ev_o = (p * (do - 1) - (1 - p)) if do else None
            ev_u = ((1 - p) * (du - 1) - p) if du else None
            pick = None
            if ev_o is not None and (ev_u is None or ev_o >= ev_u) and ev_o > 0:
                pick = (True, do, p)
            elif ev_u is not None and ev_u > 0:
                pick = (False, du, 1 - p)
            if pick is None:
                continue
            bet_over, dec, pbet = pick
            used_real = True
            win = bet_over == (oh > 0.5)
        else:
            pbet = max(p, 1 - p)
            bet_over = p >= 0.5
            dec = None
            if rec:
                dec = _american_to_decimal(rec.get("over") if bet_over else rec.get("under"))
            used_real = dec is not None
            if dec is None:
                if real_only:
                    continue
                dec = assumed
            if only_plus_ev and pbet < 1.0 / dec:
                continue
            win = bet_over == (oh > 0.5)

        be = 1.0 / dec
        if stake_mode == "kelly":
            _t = (temp_map.get(prop_u, temp) if temp_map else temp)
            pk = _cal(pbet, _t)
            b = dec - 1.0
            f = ((b * pk - (1 - pk)) / b) if b > 0 else 0.0
            f = min(max(0.0, f) * kelly_mult, max_frac)
            stake = f * start_units
        else:
            stake = 0.01 * start_units
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
             "roi": (profit / staked) if staked else 0.0, "growth": (bk / start_units - 1.0),
             "profit": profit, "final": bk, "n_real": n_real, "odds": odds, "stake_mode": stake_mode},
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
    while T <= 2.0001:
        ll = logloss(T)
        if ll < best_ll:
            best_ll, best_T = ll, T
        T += 0.05
    # regularize toward 1.0 by sample size (full weight ~200 resolved legs)
    w = min(1.0, n / 400.0)
    T_eff = 1.0 + (best_T - 1.0) * w
    return round(T_eff, 3), n


def _sigmoid(z):
    import math
    if z >= 0:
        return 1.0 / (1.0 + math.exp(-z))
    ez = math.exp(z)
    return ez / (1.0 + ez)


def fit_platt_scaling(log: pd.DataFrame, min_n: int = 30, ridge: float = 1.0):
    """
    Fit 2-parameter Platt scaling from graded projections: calibrated_p =
    sigmoid(A * logit(raw_p) + B). This generalizes calibration_temperature()'s
    single 'temperature' (equivalent to fixing A = 1/T, B = 0) with an added
    bias term B, which corrects a systematic Over/Under skew that pure
    temperature scaling can't (e.g. the model running hot on Overs specifically,
    not just overconfident on both sides equally).

    Fit via Newton-Raphson on the 2-parameter logistic log-loss (converges in
    a handful of iterations; no numpy/sklearn needed for 1 feature). A small
    ridge penalty keeps it stable on thin samples. Regularizes the fitted
    (A, B) toward the identity (A=1, B=0 = no change) by sample size, same
    pattern as calibration_temperature, so early data barely moves it.

    Returns a dict: A, B (the effective, regularized values to apply), n
    (resolved legs used), logloss (with the fit), logloss_uncalibrated (raw
    model, for comparison — lower logloss = better).
    """
    import math
    identity = {"A": 1.0, "B": 0.0, "n": 0, "logloss": None, "logloss_uncalibrated": None}
    if log is None or log.empty:
        return identity
    g = log[log["graded"].astype(str).isin(["1", "1.0", "True"])].copy()
    g["p"] = pd.to_numeric(g["p_over"], errors="coerce")
    g["y"] = pd.to_numeric(g["over_hit"], errors="coerce")
    g = g.dropna(subset=["p", "y"])
    n = len(g)
    if n < min_n:
        return {**identity, "n": n}

    xs = [math.log(min(max(float(p), 1e-4), 1 - 1e-4) / (1 - min(max(float(p), 1e-4), 1 - 1e-4)))
          for p in g["p"]]
    ys = [float(y) for y in g["y"]]

    def logloss(A, B):
        s = 0.0
        for x, y in zip(xs, ys):
            q = min(max(_sigmoid(A * x + B), 1e-6), 1 - 1e-6)
            s += -(y * math.log(q) + (1 - y) * math.log(1 - q))
        return s / n

    ll_uncal = logloss(1.0, 0.0)

    A, B = 1.0, 0.0
    for _ in range(25):
        gA = gB = 0.0
        hAA = hAB = hBB = 0.0
        for x, y in zip(xs, ys):
            q = _sigmoid(A * x + B)
            err = q - y
            w = q * (1 - q)
            gA += err * x
            gB += err
            hAA += w * x * x
            hAB += w * x
            hBB += w
        gA, gB = gA / n, gB / n
        # ridge on A only (toward 1.0), so a thin/degenerate sample can't blow up
        gA += ridge * (A - 1.0) / n
        hAA += ridge / n
        hAA, hAB, hBB = hAA / n, hAB / n, hBB / n
        det = hAA * hBB - hAB * hAB
        if abs(det) < 1e-12:
            break
        dA = (hBB * gA - hAB * gB) / det
        dB = (hAA * gB - hAB * gA) / det
        A -= dA
        B -= dB
        if abs(dA) < 1e-8 and abs(dB) < 1e-8:
            break

    ll_fit = logloss(A, B)
    # regularize toward identity by sample size (full weight ~200 resolved legs),
    # same convention as calibration_temperature
    w = min(1.0, n / 400.0)
    A_eff = 1.0 + (A - 1.0) * w
    B_eff = 0.0 + (B - 0.0) * w
    return {"A": round(A_eff, 4), "B": round(B_eff, 4), "n": n,
            "logloss": round(ll_fit, 4), "logloss_uncalibrated": round(ll_uncal, 4)}


def apply_platt(p, A, B):
    if p is None or not (0 < p < 1):
        return p
    import math
    x = math.log(p / (1 - p))
    return _sigmoid(A * x + B)


def calibration_by_probability_platt(log: pd.DataFrame, A: float = 1.0, B: float = 0.0) -> pd.DataFrame:
    """Same reliability-diagram shape as calibration_by_probability(), but with the
    'calibrated' column run through Platt scaling (apply_platt) instead of a single
    temperature. A preview tool — doesn't touch anything live."""
    g = log[log["graded"].astype(str).isin(["1", "1.0", "True"])].copy()
    if g.empty:
        return pd.DataFrame()
    g["p"] = pd.to_numeric(g["p_over"], errors="coerce")
    g["oh"] = pd.to_numeric(g["over_hit"], errors="coerce")
    g = g.dropna(subset=["p", "oh"])
    if g.empty:
        return pd.DataFrame()
    g["pc"] = g["p"].apply(lambda p: apply_platt(p, A, B))
    bins = [0.0, 0.1, 0.2, 0.3, 0.4, 0.5, 0.6, 0.7, 0.8, 0.9, 1.01]
    labels = ["0-10%", "10-20%", "20-30%", "30-40%", "40-50%",
              "50-60%", "60-70%", "70-80%", "80-90%", "90-100%"]
    g["bucket"] = pd.cut(g["p"], bins=bins, labels=labels, right=False)
    out = g.groupby("bucket", observed=True).agg(
        n=("oh", "size"),
        predicted=("p", "mean"),
        calibrated=("pc", "mean"),
        over_rate=("oh", "mean")).reset_index()
    out["gap"] = out["over_rate"] - out["predicted"]
    out["gap_cal"] = out["over_rate"] - out["calibrated"]
    return out


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
            return default
    except Exception:
        pass
    df = _sqlite_read("settings", SETTINGS_COLUMNS)
    if df is not None:
        hit = df[df["key"].astype(str) == str(key)]
        if not hit.empty:
            val = hit.iloc[-1]["value"]
            st.session_state[skey] = val
            return val
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
            return
        except Exception:
            pass
    df = _sqlite_read("settings", SETTINGS_COLUMNS)
    d = {} if df is None else dict(zip(df["key"].astype(str), df["value"]))
    d[str(key)] = value
    _sqlite_write("settings", pd.DataFrame(list(d.items()), columns=SETTINGS_COLUMNS))
