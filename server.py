"""
FastAPI backend — serves the projection slate, odds board, and (soon) the
tracker views as JSON, so a React/Next.js frontend can render a professional UI
and the slate can be PRECOMPUTED + cached instead of recomputed on every load.

It imports your existing modules verbatim (engine/data/park_factors, the
daily_ping projector, odds_scrape) — no model logic is reimplemented.

RUN LOCALLY
-----------
    pip install -r requirements-api.txt
    uvicorn server:app --reload --port 8000
    # then open http://127.0.0.1:8000/docs  (interactive API explorer)

ENDPOINTS
---------
    GET /api/health
    GET /api/slate?date=YYYY-MM-DD[&season=&refresh=1]   full projection board
    GET /api/odds?date=YYYY-MM-DD[&books=bovada,...]      multi-book odds board
    GET /api/edges?date=...                                board + odds + edge, ranked

Slate results are cached to .cache/slate_<date>.json; pass refresh=1 to rebuild.
This is the precompute layer — a scheduled job can hit /api/slate?refresh=1 each
morning so users always get an instant read.
"""
from __future__ import annotations

import datetime as dt
import json
import os
import sys

from fastapi import FastAPI, Query
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse

import data as D
import engine as E
import park_factors as PF

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), "scripts"))
import daily_ping as dp  # noqa: E402
import odds_scrape as OS  # noqa: E402

CACHE_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), ".cache")
os.makedirs(CACHE_DIR, exist_ok=True)

app = FastAPI(title="MLB Prop Model API", version="0.1")
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],          # tighten to your frontend domain before launch
    allow_methods=["*"],
    allow_headers=["*"],
)


# --------------------------------------------------------------------------- #
# Projection board (reuses daily_ping.project_batter — production logic)       #
# --------------------------------------------------------------------------- #
def build_board(date_str: str, season: int) -> dict:
    geo = dict(PF.PARK_GEO)
    for alias, real in PF.PARK_ALIASES.items():
        if real in PF.PARK_GEO:
            geo[alias] = PF.PARK_GEO[real]
    slate = D.build_slate(date_str, season, recent_days=dp.RECENT_DAYS,
                          want_weather=False, park_geo=geo)

    lr = D.league_event_rates(season)
    if lr:
        E.LEAGUE_EVENT_RATES.update({k: lr[k] for k in ("1B", "2B", "3B", "HR") if k in lr})
        if "TB" in lr:
            E.LEAGUE_TB_PER_PA = lr["TB"]
    sav_b = D.load_savant_expected(season, "batter")
    sav_p = D.load_savant_expected(season, "pitcher")

    rows, games = [], []
    for mu in slate:
        games.append({"game": f"{mu.away} @ {mu.home}", "away": mu.away,
                      "home": mu.home, "venue": mu.venue})
        if not mu.home_pitcher or not mu.away_pitcher:
            continue
        for b in mu.away_lineup:
            r = dp.project_batter(b, mu.home_pitcher, mu.venue, sav_b, sav_p, False, True)
            if r:
                rows.append({**r, "game": f"{mu.away} @ {mu.home}", "batter_id": b.mlbam_id})
        for b in mu.home_lineup:
            r = dp.project_batter(b, mu.away_pitcher, mu.venue, sav_b, sav_p, True, False)
            if r:
                rows.append({**r, "game": f"{mu.away} @ {mu.home}", "batter_id": b.mlbam_id})

    rows.sort(key=lambda r: r["p_over"], reverse=True)
    return {"date": date_str, "season": season, "n_games": len(games),
            "games": games, "rows": rows, "generated": dt.datetime.utcnow().isoformat()}


def _cache_path(date_str: str) -> str:
    return os.path.join(CACHE_DIR, f"slate_{date_str}.json")


def get_slate(date_str: str, season: int, refresh: bool) -> dict:
    path = _cache_path(date_str)
    if not refresh and os.path.exists(path):
        try:
            with open(path, encoding="utf-8") as f:
                return {**json.load(f), "cached": True}
        except Exception:
            pass
    board = build_board(date_str, season)
    try:
        with open(path, "w", encoding="utf-8") as f:
            json.dump(board, f)
    except Exception:
        pass
    return {**board, "cached": False}


# --------------------------------------------------------------------------- #
# Routes                                                                      #
# --------------------------------------------------------------------------- #
WEBAPP = os.path.join(os.path.dirname(os.path.abspath(__file__)), "webapp", "index.html")


@app.get("/")
def home():
    return FileResponse(WEBAPP)          # the board UI (same origin as /api, so no CORS)


@app.get("/api/health")
def health():
    return {"ok": True, "time": dt.datetime.utcnow().isoformat()}


@app.get("/api/slate")
def slate(date: str = Query(default=None), season: int = Query(default=None),
          refresh: bool = Query(default=False)):
    date = date or dt.date.today().isoformat()
    season = season or dt.date.fromisoformat(date).year
    return get_slate(date, season, refresh)


@app.get("/api/odds")
def odds(date: str = Query(default=None), books: str = Query(default="bovada")):
    date = date or dt.date.today().isoformat()
    names = [b.strip() for b in books.split(",") if b.strip() in OS.SOURCES]
    props_by_book = {}
    for b in names:
        try:
            props_by_book[b] = OS.SOURCES[b].fetch_tb_props(date)
        except Exception as e:
            props_by_book[b] = []
            print(f"[odds:{b}] {e}")
    slate_pairs = OS._slate_batters(date)
    matched = OS.match_to_slate(props_by_book, slate_pairs)
    board = []
    for (game, batter), row in matched.items():
        book, over = OS.best_over(row)
        board.append({"game": game, "batter": batter, "books": row,
                      "best_over_book": book, "best_over": over,
                      "fair_p_over": OS.consensus_fair(row)})
    return {"date": date, "books": names, "n": len(board), "board": board}


@app.get("/api/edges")
def edges(date: str = Query(default=None), season: int = Query(default=None),
          books: str = Query(default="bovada")):
    """Projection board joined to odds, with edge = model P(Over) − de-vigged fair P(Over)."""
    date = date or dt.date.today().isoformat()
    season = season or dt.date.fromisoformat(date).year
    board = get_slate(date, season, refresh=False)
    odds_board = odds(date, books)
    fair = {(r["game"], r["batter"]): r for r in odds_board["board"]}
    out = []
    for r in board["rows"]:
        o = fair.get((r["game"], r["batter"]))
        if not o:
            continue
        edge = r["p_over"] - (o["fair_p_over"] or r["p_over"])
        out.append({**r, "best_over": o["best_over"], "best_over_book": o["best_over_book"],
                    "fair_p_over": o["fair_p_over"], "edge": round(edge, 4)})
    out.sort(key=lambda r: r["edge"], reverse=True)
    return {"date": date, "n": len(out), "edges": out}
