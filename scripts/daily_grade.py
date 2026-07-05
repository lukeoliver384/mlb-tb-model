"""
Standalone nightly grader — no Streamlit involved.

Grades the Google Sheet tracker headless so grading no longer depends on someone
opening the app and clicking "Grade results". Designed to run in GitHub Actions
(same pattern as scripts/daily_ping.py): it imports only data.py for the grading
math and talks to Google Sheets directly via gspread, so there is no Streamlit /
session_state dependency.

WHAT IT DOES each run, over the log (sheet1) and the "bets" worksheet:
  * grades every ungraded row whose game date has passed and (for today) whose
    game is Final, using the same MLB-API lookups the app uses
    (data.player_tb_on_date / player_hrr_on_date / player_k_on_date);
  * VOIDS rows with no stat line once they are >= VOID_DAYS past their game date —
    a player who never recorded a line that day (DNP / scratched from a projected
    lineup / postponed game) is permanently ungradeable, so we stop leaving it as
    a forever-"ungraded" zombie (this matches tracker.grade()'s new behaviour and
    how bets were already voided);
  * drops corrupt rows (e.g. a header row that leaked into the data, date == "date");
  * writes the sheet back only if something actually changed, and optionally posts
    a one-line summary to Discord.

Grade states written match tracker.py exactly: graded = "1" for a real grade,
graded = "void" for a void (both are terminal; only "1" counts toward metrics).

ENV (all via GitHub Actions secrets):
  GCP_SERVICE_ACCOUNT_JSON  (required) the service-account key JSON, same account
                            wired into Streamlit secrets as [gcp_service_account]
  TRACKER_SHEET_NAME        (optional) sheet name, default "MLB TB Tracker"
  DISCORD_WEBHOOK_URL       (optional) post a summary line
  GRADE_SEASON              (optional) season, default current year
  GRADE_VOID_DAYS           (optional) days past game date before voting a no-result
                            row void, default 2

Run locally (needs the same env vars):
    GCP_SERVICE_ACCOUNT_JSON="$(cat key.json)" python scripts/daily_grade.py
"""
import datetime as dt
import json
import os
import sys

import requests

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import data as D  # noqa: E402  (grading math only — no streamlit)

if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8")

# Kept in sync with tracker.py by hand (this script is deliberately streamlit-free).
LOG_COLUMNS = ["date", "batter", "batter_id", "pitcher", "venue", "line", "prop",
               "proj", "pred_side", "p_over", "actual", "actual_side", "over_hit", "correct", "graded"]
BET_COLUMNS = ["date", "batter", "batter_id", "pitcher", "venue", "line", "side", "prop",
               "odds", "stake", "actual", "result", "profit", "close_odds", "graded"]
VOID = "void"
TERMINAL = ("1", "1.0", "True", VOID)


def _iso(x) -> str:
    """Normalize a date value (ISO, M/D/YYYY, or datetime string) to YYYY-MM-DD."""
    s = str(x).strip()
    if not s:
        return ""
    for fmt in ("%Y-%m-%d", "%m/%d/%Y", "%m/%d/%y", "%Y-%m-%dT%H:%M:%S", "%Y-%m-%d %H:%M:%S"):
        try:
            return dt.datetime.strptime(s.split("T")[0] if fmt == "%Y-%m-%d" else s, fmt).date().isoformat()
        except ValueError:
            continue
    # last resort: take a leading YYYY-MM-DD if present
    head = s[:10]
    try:
        return dt.date.fromisoformat(head).isoformat()
    except ValueError:
        return ""


def _american_to_decimal_profit(a) -> float:
    v = float(str(a).replace("+", "").strip())
    return v / 100.0 if v > 0 else 100.0 / abs(v)


def _parse_service_account(raw: str) -> dict:
    """Accept the service account as JSON OR as the Streamlit [gcp_service_account]
    TOML block (with or without the section header) — whichever the user pasted
    into the secret. Raises a clear error if it's neither (e.g. only the key)."""
    raw = (raw or "").strip()
    try:
        return json.loads(raw)                       # a proper JSON object
    except Exception:
        pass
    try:
        import tomllib                                # stdlib on 3.11+
        data = tomllib.loads(raw)
        if isinstance(data.get("gcp_service_account"), dict):
            return data["gcp_service_account"]        # full [gcp_service_account] block
        if "private_key" in data and "client_email" in data:
            return data                               # just the fields, no header
    except Exception:
        pass
    raise SystemExit(
        "GCP_SERVICE_ACCOUNT_JSON is not usable. Paste the FULL service-account key "
        "(the whole JSON object starting with '{', or your Streamlit "
        "[gcp_service_account] TOML block) — not just the private_key.")


def _open_sheet():
    sa_raw = os.environ.get("GCP_SERVICE_ACCOUNT_JSON")
    if not sa_raw:
        raise SystemExit("GCP_SERVICE_ACCOUNT_JSON not set — cannot reach the tracker sheet.")
    import gspread
    from google.oauth2.service_account import Credentials
    creds = Credentials.from_service_account_info(
        _parse_service_account(sa_raw),
        scopes=["https://www.googleapis.com/auth/spreadsheets",
                "https://www.googleapis.com/auth/drive"])
    gc = gspread.authorize(creds)
    name = os.environ.get("TRACKER_SHEET_NAME") or "MLB TB Tracker"
    return gc.open(name)


def _fn_for(prop: str):
    prop = (prop or "TB").upper()
    if prop == "HRR":
        return D.player_hrr_on_date
    if prop == "K":
        return D.player_k_on_date
    return D.player_tb_on_date


def _valid_date_row(rec, cols) -> bool:
    """Drop obvious corruption: a leaked header row, or a row with no usable date."""
    raw = str(rec.get("date", "")).strip()
    if raw.lower() == "date":          # header leaked into data
        return False
    return bool(_iso(raw))


def _rows_to_matrix(records, cols):
    out = []
    for rec in records:
        out.append(["" if rec.get(c) is None else rec.get(c) for c in cols])
    return out


def grade_log(sh, season: int, void_days: int) -> dict:
    ws = sh.sheet1
    records = ws.get_all_records(expected_headers=LOG_COLUMNS)
    today = dt.date.today().isoformat()
    today_d = dt.date.fromisoformat(today)
    finals: dict = {}
    graded = voided = dropped = 0
    kept = []
    for rec in records:
        if not _valid_date_row(rec, LOG_COLUMNS):
            dropped += 1
            continue
        kept.append(rec)
        if str(rec.get("graded")) in TERMINAL:
            continue
        d = _iso(rec.get("date"))
        if d > today:
            continue
        if d == today:
            if d not in finals:
                finals[d] = D.final_venues(d)
            if str(rec.get("venue", "")).strip() not in finals[d]:
                continue
        try:
            bid = int(rec.get("batter_id"))
        except (ValueError, TypeError):
            continue
        if not bid:
            continue
        fn = _fn_for(rec.get("prop"))
        try:
            actual = fn(bid, season, d)
        except Exception:
            continue
        if actual is None:
            age = (today_d - dt.date.fromisoformat(d)).days
            if age >= void_days:
                rec["actual_side"] = VOID
                rec["graded"] = VOID
                voided += 1
            continue
        try:
            ln = float(rec.get("line"))
        except (ValueError, TypeError):
            continue
        a_side = "Over" if actual > ln else "Under"
        pred = str(rec.get("pred_side") or "")
        if pred not in ("Over", "Under"):
            try:
                pred = "Over" if float(rec.get("p_over")) > 0.5 else "Under"
            except (ValueError, TypeError):
                pred = ""
        rec["actual"] = actual
        rec["actual_side"] = a_side
        rec["over_hit"] = int(actual > ln)
        rec["correct"] = 1 if pred == a_side else 0
        rec["graded"] = 1
        graded += 1

    if graded or voided or dropped:
        ws.clear()
        ws.append_rows([LOG_COLUMNS] + _rows_to_matrix(kept, LOG_COLUMNS),
                       value_input_option="RAW")
    return {"graded": graded, "voided": voided, "dropped": dropped, "rows": len(kept)}


def grade_bets(sh, season: int) -> dict:
    try:
        ws = sh.worksheet("bets")
    except Exception:
        return {"graded": 0, "voided": 0, "rows": 0, "missing": True}
    records = ws.get_all_records(expected_headers=BET_COLUMNS)
    today = dt.date.today().isoformat()
    finals: dict = {}
    graded = voided = 0
    kept = []
    for rec in records:
        if not _valid_date_row(rec, BET_COLUMNS):
            continue
        kept.append(rec)
        if str(rec.get("graded")) in TERMINAL:
            continue
        d = _iso(rec.get("date"))
        if d > today:
            continue
        if d == today:
            if d not in finals:
                finals[d] = D.final_venues(d)
            if str(rec.get("venue", "")).strip() not in finals[d]:
                continue
        try:
            bid = int(rec.get("batter_id"))
            line = float(rec.get("line")); odds = float(rec.get("odds")); stake = float(rec.get("stake"))
        except (ValueError, TypeError):
            continue
        fn = _fn_for(rec.get("prop"))
        try:
            actual = fn(bid, season, d) if bid else None
        except Exception:
            continue
        if actual is None:
            rec["result"] = VOID; rec["profit"] = 0.0; rec["actual"] = ""; rec["graded"] = 1
            voided += 1
            continue
        side = str(rec.get("side")).lower()
        won = (actual > line) if side == "over" else (actual < line)
        if actual == line:
            profit, res = 0.0, "push"
        elif won:
            profit, res = stake * _american_to_decimal_profit(odds), "win"
        else:
            profit, res = -stake, "loss"
        rec["actual"] = actual; rec["result"] = res
        rec["profit"] = round(profit, 3); rec["graded"] = 1
        graded += 1

    if graded or voided:
        ws.clear()
        ws.append_rows([BET_COLUMNS] + _rows_to_matrix(kept, BET_COLUMNS),
                       value_input_option="RAW")
    return {"graded": graded, "voided": voided, "rows": len(kept)}


def _post_discord(msg: str) -> None:
    url = os.environ.get("DISCORD_WEBHOOK_URL")
    if not url:
        return
    try:
        requests.post(url, json={"content": msg}, timeout=30).raise_for_status()
    except Exception as e:
        print(f"(discord post failed: {e})")


def main():
    season = int(os.environ.get("GRADE_SEASON") or dt.date.today().year)
    void_days = int(os.environ.get("GRADE_VOID_DAYS") or "2")
    sh = _open_sheet()
    lg = grade_log(sh, season, void_days)
    bt = grade_bets(sh, season)
    summary = (f"Tracker grading {dt.date.today().isoformat()} — "
               f"log: {lg['graded']} graded, {lg['voided']} voided"
               + (f", {lg['dropped']} corrupt dropped" if lg.get("dropped") else "")
               + f"; bets: {bt['graded']} graded, {bt['voided']} voided"
               + ("" if not bt.get("missing") else " (no bets sheet)"))
    print(summary)
    # only ping Discord when something actually happened
    if lg["graded"] or lg["voided"] or lg.get("dropped") or bt["graded"] or bt["voided"]:
        _post_discord(summary)


if __name__ == "__main__":
    main()
# end of scripts/daily_grade.py
