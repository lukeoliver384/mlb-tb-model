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

    @property
    def tb_per_bf(self) -> float:
        return self.tb_allowed / self.bf if self.bf else 0.0


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
# Convenience: build a fully-populated slate                                  #
# --------------------------------------------------------------------------- #
def build_slate(date: str, season: int) -> list[Matchup]:
    games = get_schedule(date)
    for mu in games:
        for side, attr in (("home", "home_pitcher"), ("away", "away_pitcher")):
            p = getattr(mu, attr)
            if p:
                if not p.throws:
                    p.throws = pitcher_throws(p.mlbam_id)
                fill_pitcher_stats(p, season)
        mu.home_lineup = get_lineup(mu.game_pk, "home")
        mu.away_lineup = get_lineup(mu.game_pk, "away")
        for b in mu.home_lineup + mu.away_lineup:
            fill_batter_stats(b, season)
    return games
