"""
Data layer.

Backbone: MLB StatsAPI (free, stable) for the daily slate, probable pitchers,
confirmed lineups, and handedness — plus season batting/pitching stats with
vs-LHP / vs-RHP splits, from which TB/PA and TB-allowed/BF are computed.

Optional enhancement: Fangraphs rates (richer / expected stats) loaded either
via pybaseball or from a CSV you export from Fangraphs. If Fangraphs data is
unavailable (cloud IPs get blocked), the app degrades gracefully to MLB API.

All network calls are wrapped so the UI can show a clear message instead of
crashing when a feed is down.
"""

from __future__ import annotations

import datetime as dt
from dataclasses import dataclass, field
from functools import lru_cache
from concurrent.futures import ThreadPoolExecutor

import requests

STATSAPI = "https://statsapi.mlb.com/api/v1"
TIMEOUT = 15
HEADERS = {"User-Agent": "mlb-tb-model/1.0"}


# --------------------------------------------------------------------------- #
# Data classes                                                                #
# --------------------------------------------------------------------------- #
@dataclass
class Pitcher:
    mlbam_id: int
    name: str
    throws: str = ""           # "L" or "R"
    tb_allowed: float = 0.0
    bf: float = 0.0
    tb_per_bf_vs_l: float = 0.0
    tb_per_bf_vs_r: float = 0.0
    games_started: float = 0.0

    @property
    def tb_per_bf(self) -> float:
        return self.tb_allowed / self.bf if self.bf else 0.0

    @property
    def bf_per_start(self) -> float:
        return self.bf / self.games_started if self.games_started else 0.0


@dataclass
class Batter:
    mlbam_id: int
    name: str
    bats: str = ""             # "L", "R", "S"
    order: int = 0             # batting-order slot 1-9
    pa: float = 0.0
    tb: float = 0.0
    pa_vs_l: float = 0.0
    tb_vs_l: float = 0.0
    pa_vs_r: float = 0.0
    tb_vs_r: float = 0.0
    single: float = 0.0
    double: float = 0.0
    triple: float = 0.0
    hr: float = 0.0
    recent_pa: float = 0.0
    recent_tb: float = 0.0

    @property
    def tb_per_pa(self) -> float:
        return self.tb / self.pa if self.pa else 0.0

    def tb_per_pa_vs(self, throws: str) -> tuple[float, float]:
        """Return (rate, sample_pa) split vs the pitcher's handedness."""
        if throws.upper() == "L" and self.pa_vs_l:
            return self.tb_vs_l / self.pa_vs_l, self.pa_vs_l
        if throws.upper() == "R" and self.pa_vs_r:
            return self.tb_vs_r / self.pa_vs_r, self.pa_vs_r
        return self.tb_per_pa, self.pa

    def hit_shares(self):
        hits = self.single + self.double + self.triple + self.hr
        if hits <= 0:
            return None
        return (self.single / hits, self.double / hits,
                self.triple / hits, self.hr / hits)


@dataclass
class Matchup:
    game_pk: int
    home: str
    away: str
    venue: str
    home_pitcher: "Pitcher | None" = None
    away_pitcher: "Pitcher | None" = None
    home_lineup: list = field(default_factory=list)
    away_lineup: list = field(default_factory=list)


# --------------------------------------------------------------------------- #
# Schedule + lineups (MLB StatsAPI)                                           #
# --------------------------------------------------------------------------- #
def _get(url, **params):
    r = requests.get(url, params=params, headers=HEADERS, timeout=TIMEOUT)
    r.raise_for_status()
    return r.json()


def get_schedule(date: str) -> list[Matchup]:
    """date as 'YYYY-MM-DD'. Returns games with probable pitchers."""
    data = _get(f"{STATSAPI}/schedule", sportId=1, date=date,
                hydrate="probablePitcher,lineups,venue")
    out = []
    for d in data.get("dates", []):
        for g in d.get("games", []):
            teams = g["teams"]
            mu = Matchup(
                game_pk=g["gamePk"],
                home=teams["home"]["team"]["name"],
                away=teams["away"]["team"]["name"],
                venue=g.get("venue", {}).get("name", ""),
            )
            for side, attr in (("home", "home_pitcher"), ("away", "away_pitcher")):
                pp = teams[side].get("probablePitcher")
                if pp:
                    setattr(mu, attr, Pitcher(mlbam_id=pp["id"], name=pp["fullName"]))
            out.append(mu)
    return out


def get_lineup(game_pk: int, side: str) -> list[Batter]:
    """Confirmed batting order for a game side ('home'/'away'). Empty if not posted."""
    box = _get(f"{STATSAPI.replace('/v1','')}/v1/game/{game_pk}/boxscore")
    team = box["teams"][side]
    batters_ids = team.get("battingOrder", [])
    out = []
    for i, pid in enumerate(batters_ids[:9], start=1):
        p = team["players"].get(f"ID{pid}", {})
        person = p.get("person", {})
        out.append(Batter(
            mlbam_id=pid,
            name=person.get("fullName", str(pid)),
            bats=p.get("batSide", {}).get("code", ""),
            order=i,
        ))
    return out


# --------------------------------------------------------------------------- #
# Season stats + L/R splits (MLB StatsAPI)                                     #
# --------------------------------------------------------------------------- #
def _people_stats(pid: int, group: str, season: int, splits: bool):
    """group: 'hitting' or 'pitching'."""
    stype = "statSplits" if splits else "season"
    params = dict(stats=stype, group=group, season=season, sportId=1)
    if splits:
        params["sitCodes"] = "vl,vr"
    return _get(f"{STATSAPI}/people/{pid}/stats", **params)


def fill_batter_stats(b: Batter, season: int) -> Batter:
    try:
        season_data = _people_stats(b.mlbam_id, "hitting", season, splits=False)
        s = season_data["stats"][0]["splits"][0]["stat"]
        b.pa = float(s.get("plateAppearances", 0))
        b.tb = float(s.get("totalBases", 0))
        b.single = float(s.get("hits", 0)) - float(s.get("doubles", 0)) \
            - float(s.get("triples", 0)) - float(s.get("homeRuns", 0))
        b.double = float(s.get("doubles", 0))
        b.triple = float(s.get("triples", 0))
        b.hr = float(s.get("homeRuns", 0))
    except Exception:
        pass
    try:
        sp = _people_stats(b.mlbam_id, "hitting", season, splits=True)
        for split in sp["stats"][0]["splits"]:
            code = split.get("split", {}).get("code", "")
            st = split["stat"]
            if code == "vl":
                b.pa_vs_l = float(st.get("plateAppearances", 0))
                b.tb_vs_l = float(st.get("totalBases", 0))
            elif code == "vr":
                b.pa_vs_r = float(st.get("plateAppearances", 0))
                b.tb_vs_r = float(st.get("totalBases", 0))
    except Exception:
        pass
    if not b.bats:
        b.bats = batter_bats(b.mlbam_id)
    return b


def fill_pitcher_stats(p: Pitcher, season: int) -> Pitcher:
    def _tb_allowed(st):
        h = float(st.get("hits", 0)); d = float(st.get("doubles", 0))
        t = float(st.get("triples", 0)); hr = float(st.get("homeRuns", 0))
        singles = h - d - t - hr
        return singles + 2 * d + 3 * t + 4 * hr
    try:
        sd = _people_stats(p.mlbam_id, "pitching", season, splits=False)
        st = sd["stats"][0]["splits"][0]["stat"]
        p.throws = sd["stats"][0]["splits"][0].get("player", {}).get("pitchHand", "") or p.throws
        p.tb_allowed = _tb_allowed(st)
        p.bf = float(st.get("battersFaced", 0))
        p.games_started = float(st.get("gamesStarted", 0))
    except Exception:
        pass
    try:
        sp = _people_stats(p.mlbam_id, "pitching", season, splits=True)
        for split in sp["stats"][0]["splits"]:
            code = split.get("split", {}).get("code", "")
            st = split["stat"]
            bf = float(st.get("battersFaced", 0)) or 1
            if code == "vl":
                p.tb_per_bf_vs_l = _tb_allowed(st) / bf
            elif code == "vr":
                p.tb_per_bf_vs_r = _tb_allowed(st) / bf
    except Exception:
        pass
    return p


def pitcher_throws(pid: int) -> str:
    try:
        person = _get(f"{STATSAPI}/people/{pid}")
        return person["people"][0].get("pitchHand", {}).get("code", "")
    except Exception:
        return ""


def batter_bats(pid: int) -> str:
    """Batter handedness ("L"/"R"/"S") from the player profile endpoint."""
    try:
        person = _get(f"{STATSAPI}/people/{pid}")
        return person["people"][0].get("batSide", {}).get("code", "")
    except Exception:
        return ""


# --------------------------------------------------------------------------- #
# Fangraphs (optional enhancement)                                            #
# --------------------------------------------------------------------------- #
def load_fangraphs_csv(path_or_buffer):
    """
    Load a Fangraphs leaderboard CSV export. Returns a dict keyed by lowercased
    player name -> {'tb_per_pa':..., 'pa':...}. Expects columns including 'Name',
    'PA', and either 'TB' or enough to derive it (1B/2B/3B/HR or SLG*AB).
    """
    import pandas as pd
    df = pd.read_csv(path_or_buffer)
    df.columns = [c.strip() for c in df.columns]
    out = {}
    for _, row in df.iterrows():
        name = str(row.get("Name", "")).strip().lower()
        pa = float(row.get("PA", 0) or 0)
        if not name or pa <= 0:
            continue
        if "TB" in df.columns:
            tb = float(row.get("TB", 0) or 0)
        else:
            singles = float(row.get("1B", 0) or 0)
            tb = (singles + 2 * float(row.get("2B", 0) or 0)
                  + 3 * float(row.get("3B", 0) or 0)
                  + 4 * float(row.get("HR", 0) or 0))
        out[name] = {"tb_per_pa": tb / pa if pa else 0, "pa": pa}
    return out


# --------------------------------------------------------------------------- #
# Recent form (MLB StatsAPI byDateRange)                                       #
# --------------------------------------------------------------------------- #
def fill_recent_form(b: Batter, season: int, days: int = 21) -> Batter:
    """Last-N-days TB/PA via byDateRange. Stored on b.recent_pa / b.recent_tb."""
    end = dt.date.today()
    start = end - dt.timedelta(days=days)
    try:
        data = _get(f"{STATSAPI}/people/{b.mlbam_id}/stats",
                    stats="byDateRange", group="hitting", season=season, sportId=1,
                    startDate=start.isoformat(), endDate=end.isoformat())
        st = data["stats"][0]["splits"][0]["stat"]
        b.recent_pa = float(st.get("plateAppearances", 0))
        b.recent_tb = float(st.get("totalBases", 0))
    except Exception:
        pass
    return b


# --------------------------------------------------------------------------- #
# Statcast expected stats (Baseball Savant)                                    #
# --------------------------------------------------------------------------- #
def load_savant_expected(season: int, kind: str = "batter") -> dict:
    """
    Pull Savant expected_statistics leaderboard. Returns
        {player_id: {"slg": float, "est_slg": float, "luck": est_slg/slg}}
    `luck` < 1 means the player has out-hit his contact quality (regress down);
    > 1 means unlucky (regress up). Applied to actual TB/PA in the app.
    """
    url = ("https://baseballsavant.mlb.com/leaderboard/expected_statistics"
           f"?type={kind}&year={season}&position=&team=&min=q&csv=true")
    out = {}
    try:
        r = requests.get(url, headers=HEADERS, timeout=TIMEOUT)
        r.raise_for_status()
        import io, csv
        reader = csv.DictReader(io.StringIO(r.text))
        for row in reader:
            try:
                pid = int(row.get("player_id") or row.get("\ufeffplayer_id"))
                slg = float(row.get("slg") or 0)
                est = float(row.get("est_slg") or 0)
            except (TypeError, ValueError):
                continue
            if slg > 0 and est > 0:
                out[pid] = {"slg": slg, "est_slg": est, "luck": est / slg}
    except Exception:
        pass
    return out


def league_reliever_tb_per_bf_default() -> float:
    """Bullpen TB/BF default (relievers run a touch better than overall league)."""
    return 0.345


# --------------------------------------------------------------------------- #
# Convenience: build a fully-populated slate                                  #
# --------------------------------------------------------------------------- #
def build_slate(date: str, season: int, recent_days: int = 0,
                max_workers: int = 16) -> list[Matchup]:
    """
    Build the full slate, fetching all player stats in parallel.

    A full slate is hundreds of API calls; doing them concurrently turns a
    multi-minute sequential load into ~20-30s. Each call is already wrapped in
    try/except, so one slow or missing player can't stall the whole slate.
    """
    games = get_schedule(date)

    # 1) lineups (one boxscore call per side), in parallel
    def _load_lineups(mu: Matchup):
        try:
            mu.home_lineup = get_lineup(mu.game_pk, "home")
            mu.away_lineup = get_lineup(mu.game_pk, "away")
        except Exception:
            mu.home_lineup, mu.away_lineup = [], []

    with ThreadPoolExecutor(max_workers=max_workers) as ex:
        list(ex.map(_load_lineups, games))

    # 2) gather every player, then fetch stats concurrently
    pitchers = [getattr(mu, a) for mu in games
                for a in ("home_pitcher", "away_pitcher") if getattr(mu, a)]
    batters = [b for mu in games for b in (mu.home_lineup + mu.away_lineup)]

    def _do_pitcher(p: Pitcher):
        try:
            if not p.throws:
                p.throws = pitcher_throws(p.mlbam_id)
            fill_pitcher_stats(p, season)
        except Exception:
            pass

    def _do_batter(b: Batter):
        try:
            fill_batter_stats(b, season)
            if recent_days:
                fill_recent_form(b, season, recent_days)
        except Exception:
            pass

    with ThreadPoolExecutor(max_workers=max_workers) as ex:
        list(ex.map(_do_pitcher, pitchers))
        list(ex.map(_do_batter, batters))

    return games
