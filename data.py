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
    r_allowed: float = 0.0
    k_allowed: float = 0.0
    h_allowed: float = 0.0
    d_allowed: float = 0.0
    t_allowed: float = 0.0
    hr_allowed: float = 0.0
    bf_vs_l: float = 0.0
    h_vs_l: float = 0.0
    d_vs_l: float = 0.0
    t_vs_l: float = 0.0
    hr_vs_l: float = 0.0
    k_vs_l: float = 0.0
    bf_vs_r: float = 0.0
    h_vs_r: float = 0.0
    d_vs_r: float = 0.0
    t_vs_r: float = 0.0
    hr_vs_r: float = 0.0
    k_vs_r: float = 0.0
    bf_home: float = 0.0
    tb_home_allowed: float = 0.0
    bf_away: float = 0.0
    tb_away_allowed: float = 0.0

    @property
    def tb_per_bf(self) -> float:
        return self.tb_allowed / self.bf if self.bf else 0.0

    @property
    def bf_per_start(self) -> float:
        # bf is TOTAL batters faced (incl. relief), so bf/GS inflates for swingmen and is
        # noisy with few starts. Regress toward a league-average start (~23 BF) and cap at a
        # realistic single-start max so no pitcher is modeled "facing the lineup 4x".
        if not self.games_started:
            return 0.0
        raw = self.bf / self.games_started
        blended = (raw * self.games_started + 23.0 * 3.0) / (self.games_started + 3.0)
        return min(blended, 28.0)

    @property
    def h_per_bf(self) -> float:
        return self.h_allowed / self.bf if self.bf else 0.0

    @property
    def r_per_bf(self) -> float:
        return self.r_allowed / self.bf if self.bf else 0.0

    @property
    def k_per_bf(self) -> float:
        return self.k_allowed / self.bf if self.bf else 0.0

    def k_per_bf_vs(self, bat_side: str):
        """Pitcher K/BF vs the batter's side (min 50 BF), else overall (k_per_bf, n=bf)."""
        if bat_side.upper() in ("L", "S") and self.bf_vs_l >= 50 and self.k_vs_l:
            return self.k_vs_l / self.bf_vs_l, self.bf_vs_l
        if bat_side.upper() == "R" and self.bf_vs_r >= 50 and self.k_vs_r:
            return self.k_vs_r / self.bf_vs_r, self.bf_vs_r
        return self.k_per_bf, self.bf

    def event_rates_allowed_vs(self, bat_side: str):
        """Per-BF {1B,2B,3B,HR} allowed vs batter side; falls back to overall."""
        if bat_side.upper() in ("L", "S") and self.bf_vs_l >= 50:
            bf, h, d, t, hr = self.bf_vs_l, self.h_vs_l, self.d_vs_l, self.t_vs_l, self.hr_vs_l
        elif bat_side.upper() == "R" and self.bf_vs_r >= 50:
            bf, h, d, t, hr = self.bf_vs_r, self.h_vs_r, self.d_vs_r, self.t_vs_r, self.hr_vs_r
        else:
            bf, h, d, t, hr = self.bf, self.h_allowed, self.d_allowed, self.t_allowed, self.hr_allowed
        if bf <= 0:
            return None
        singles = h - d - t - hr
        return {"1B": singles/bf, "2B": d/bf, "3B": t/bf, "HR": hr/bf}

    def home_away_factor(self, is_home: bool, k: float = 600.0) -> float:
        """Multiplier vs this pitcher's OWN overall TB/BF allowed for home/away,
        heavily regressed toward 1.0. Small secondary signal."""
        overall = self.tb_per_bf
        if overall <= 0:
            return 1.0
        rate, n = (self.tb_home_allowed / self.bf_home, self.bf_home) if is_home and self.bf_home \
            else (self.tb_away_allowed / self.bf_away, self.bf_away) if (not is_home) and self.bf_away \
            else (overall, 0)
        if n <= 0:
            return 1.0
        reg = (rate * n + overall * k) / (n + k)
        return reg / overall


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
    # per-hand hit components (h, 2b, 3b, hr) for component log5
    h_vs_l: float = 0.0
    d_vs_l: float = 0.0
    t_vs_l: float = 0.0
    hr_vs_l: float = 0.0
    h_vs_r: float = 0.0
    d_vs_r: float = 0.0
    t_vs_r: float = 0.0
    hr_vs_r: float = 0.0
    pa_home: float = 0.0
    tb_home: float = 0.0
    pa_away: float = 0.0
    tb_away: float = 0.0
    runs: float = 0.0
    rbi: float = 0.0
    k: float = 0.0
    k_vs_l: float = 0.0
    k_vs_r: float = 0.0

    @property
    def tb_per_pa(self) -> float:
        return self.tb / self.pa if self.pa else 0.0

    @property
    def hits_per_pa(self) -> float:
        h = self.single + self.double + self.triple + self.hr
        return h / self.pa if self.pa else 0.0

    @property
    def runs_per_pa(self) -> float:
        return self.runs / self.pa if self.pa else 0.0

    @property
    def rbi_per_pa(self) -> float:
        return self.rbi / self.pa if self.pa else 0.0

    @property
    def k_per_pa(self) -> float:
        return self.k / self.pa if self.pa else 0.0

    def k_per_pa_vs(self, throws: str):
        if throws.upper() == "L" and self.pa_vs_l >= 30 and self.k_vs_l:
            return self.k_vs_l / self.pa_vs_l, self.pa_vs_l
        if throws.upper() == "R" and self.pa_vs_r >= 30 and self.k_vs_r:
            return self.k_vs_r / self.pa_vs_r, self.pa_vs_r
        return self.k_per_pa, self.pa

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

    def home_away_factor(self, is_home: bool, k: float = 500.0) -> float:
        """Multiplier vs this batter's OWN overall TB/PA for home or away, heavily
        regressed toward 1.0 (k large). A small, noisy secondary signal."""
        overall = self.tb_per_pa
        if overall <= 0:
            return 1.0
        rate, n = (self.tb_home / self.pa_home, self.pa_home) if is_home and self.pa_home \
            else (self.tb_away / self.pa_away, self.pa_away) if (not is_home) and self.pa_away \
            else (overall, 0)
        if n <= 0:
            return 1.0
        reg = (rate * n + overall * k) / (n + k)
        return reg / overall

    def event_rates_vs(self, throws: str):
        """Per-PA {1B,2B,3B,HR} vs the pitcher's hand; falls back to overall."""
        if throws.upper() == "L" and self.pa_vs_l >= 30:
            pa, h, d, t, hr = self.pa_vs_l, self.h_vs_l, self.d_vs_l, self.t_vs_l, self.hr_vs_l
        elif throws.upper() == "R" and self.pa_vs_r >= 30:
            pa, h, d, t, hr = self.pa_vs_r, self.h_vs_r, self.d_vs_r, self.t_vs_r, self.hr_vs_r
        else:
            pa = self.pa
            h = self.single + self.double + self.triple + self.hr
            d, t, hr = self.double, self.triple, self.hr
        if pa <= 0:
            return None
        singles = h - d - t - hr
        return {"1B": singles/pa, "2B": d/pa, "3B": t/pa, "HR": hr/pa}


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
    game_datetime: str = ""          # UTC ISO, for weather hour matching
    temp_f: "float | None" = None
    wind_mph: "float | None" = None
    wind_dir: "float | None" = None  # meteorological (from), degrees


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
                game_datetime=g.get("gameDate", ""),
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
def _people_stats(pid: int, group: str, season: int, splits: bool, sit_codes: str = "vl,vr"):
    """group: 'hitting' or 'pitching'. sit_codes e.g. 'vl,vr' or 'h,a'."""
    stype = "statSplits" if splits else "season"
    params = dict(stats=stype, group=group, season=season, sportId=1)
    if splits:
        params["sitCodes"] = sit_codes
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
        b.runs = float(s.get("runs", 0))
        b.rbi = float(s.get("rbi", 0))
        b.k = float(s.get("strikeOuts", 0))
    except Exception:
        pass
    try:
        sp = _people_stats(b.mlbam_id, "hitting", season, splits=True, sit_codes="vl,vr,h,a")
        for split in sp["stats"][0]["splits"]:
            code = split.get("split", {}).get("code", "")
            st = split["stat"]
            if code == "vl":
                b.pa_vs_l = float(st.get("plateAppearances", 0)); b.tb_vs_l = float(st.get("totalBases", 0))
                b.h_vs_l = float(st.get("hits", 0)); b.d_vs_l = float(st.get("doubles", 0))
                b.t_vs_l = float(st.get("triples", 0)); b.hr_vs_l = float(st.get("homeRuns", 0))
                b.k_vs_l = float(st.get("strikeOuts", 0))
            elif code == "vr":
                b.pa_vs_r = float(st.get("plateAppearances", 0)); b.tb_vs_r = float(st.get("totalBases", 0))
                b.h_vs_r = float(st.get("hits", 0)); b.d_vs_r = float(st.get("doubles", 0))
                b.t_vs_r = float(st.get("triples", 0)); b.hr_vs_r = float(st.get("homeRuns", 0))
                b.k_vs_r = float(st.get("strikeOuts", 0))
            elif code == "h":
                b.pa_home = float(st.get("plateAppearances", 0)); b.tb_home = float(st.get("totalBases", 0))
            elif code == "a":
                b.pa_away = float(st.get("plateAppearances", 0)); b.tb_away = float(st.get("totalBases", 0))
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
        p.games_started = float(st.get("gamesStarted", 0))
        p.h_allowed = float(st.get("hits", 0)); p.d_allowed = float(st.get("doubles", 0))
        p.t_allowed = float(st.get("triples", 0)); p.hr_allowed = float(st.get("homeRuns", 0))
        p.r_allowed = float(st.get("runs", 0))
        p.k_allowed = float(st.get("strikeOuts", 0))
    except Exception:
        pass
    try:
        sp = _people_stats(p.mlbam_id, "pitching", season, splits=True, sit_codes="vl,vr,h,a")
        for split in sp["stats"][0]["splits"]:
            code = split.get("split", {}).get("code", "")
            st = split["stat"]
            bf = float(st.get("battersFaced", 0)) or 1
            if code == "vl":
                p.tb_per_bf_vs_l = _tb_allowed(st) / bf
                p.bf_vs_l = float(st.get("battersFaced", 0))
                p.h_vs_l = float(st.get("hits", 0)); p.d_vs_l = float(st.get("doubles", 0))
                p.t_vs_l = float(st.get("triples", 0)); p.hr_vs_l = float(st.get("homeRuns", 0))
                p.k_vs_l = float(st.get("strikeOuts", 0))
            elif code == "vr":
                p.tb_per_bf_vs_r = _tb_allowed(st) / bf
                p.bf_vs_r = float(st.get("battersFaced", 0))
                p.h_vs_r = float(st.get("hits", 0)); p.d_vs_r = float(st.get("doubles", 0))
                p.t_vs_r = float(st.get("triples", 0)); p.hr_vs_r = float(st.get("homeRuns", 0))
                p.k_vs_r = float(st.get("strikeOuts", 0))
            elif code == "h":
                p.bf_home = float(st.get("battersFaced", 0)); p.tb_home_allowed = _tb_allowed(st)
            elif code == "a":
                p.bf_away = float(st.get("battersFaced", 0)); p.tb_away_allowed = _tb_allowed(st)
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
def league_event_rates(season: int):
    """Current-season league per-PA rates from team totals (auto-adjusts the baseline
    to the run environment). Returns dict with 1B/2B/3B/HR/H/R/RBI/TB per PA, or None."""
    try:
        d = _get(f"{STATSAPI}/teams/stats", season=season, group="hitting",
                 stats="season", sportId=1)
        H = D2 = D3 = HR = R = RBI = TB = PA = SO = 0.0
        for sp in d["stats"][0]["splits"]:
            s = sp["stat"]
            H += float(s.get("hits", 0)); D2 += float(s.get("doubles", 0))
            D3 += float(s.get("triples", 0)); HR += float(s.get("homeRuns", 0))
            R += float(s.get("runs", 0)); RBI += float(s.get("rbi", 0))
            TB += float(s.get("totalBases", 0)); PA += float(s.get("plateAppearances", 0))
            SO += float(s.get("strikeOuts", 0))
        if PA < 1000:
            return None
        singles = H - D2 - D3 - HR
        return {"1B": singles / PA, "2B": D2 / PA, "3B": D3 / PA, "HR": HR / PA,
                "H": H / PA, "R": R / PA, "RBI": RBI / PA, "TB": TB / PA, "K": SO / PA}
    except Exception:
        return None


def player_k_on_date(pid: int, season: int, date: str):
    """Actual pitcher strikeouts on a date from the pitching game log. None if no start."""
    try:
        d = _get(f"{STATSAPI}/people/{pid}/stats",
                 stats="gameLog", group="pitching", season=season, sportId=1)
        games = [s["stat"] for s in d["stats"][0]["splits"] if s.get("date") == date]
        if not games:
            return None
        return float(sum(float(st.get("strikeOuts", 0)) for st in games))
    except Exception:
        return None


def final_venues(date: str) -> set:
    """Set of venue names whose games are complete on the date (for as-you-go grading)."""
    try:
        data = _get(f"{STATSAPI}/schedule", sportId=1, date=date)
        out = set()
        for d in data.get("dates", []):
            for g in d.get("games", []):
                s = g.get("status", {})
                detailed = str(s.get("detailedState", "")).lower()
                final = (s.get("abstractGameState") == "Final"
                         or s.get("codedGameState") in ("F", "O")
                         or "final" in detailed or detailed == "game over"
                         or "completed" in detailed)
                if final:
                    out.add((g.get("venue", {}).get("name", "") or "").strip())
        return out
    except Exception:
        return set()


def _gamelog_on_date(pid: int, season: int, date: str):
    """Return list of per-game hitting stat dicts for a player on a date (sums doubleheaders)."""
    d = _get(f"{STATSAPI}/people/{pid}/stats",
             stats="gameLog", group="hitting", season=season, sportId=1)
    return [s["stat"] for s in d["stats"][0]["splits"] if s.get("date") == date]


def player_tb_on_date(pid: int, season: int, date: str):
    """Actual total bases on a date (YYYY-MM-DD) from the game log. None if DNP."""
    try:
        games = _gamelog_on_date(pid, season, date)
        if not games:
            return None
        return float(sum(float(st.get("totalBases", 0)) for st in games))
    except Exception:
        return None


def player_hrr_on_date(pid: int, season: int, date: str):
    """Actual Hits+Runs+RBIs on a date from the game log. None if DNP."""
    try:
        games = _gamelog_on_date(pid, season, date)
        if not games:
            return None
        return float(sum(float(st.get("hits", 0)) + float(st.get("runs", 0)) + float(st.get("rbi", 0))
                         for st in games))
    except Exception:
        return None


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


def load_pitcher_whiff(season: int) -> dict:
    """Usage-weighted whiff% (whiffs/swings) per pitcher from Savant pitch-arsenal CSV.
    Returns {player_id: whiff_fraction}. SwStr/whiff stabilizes faster than K%, so it
    sharpens the pitcher K rate — most valuable early in a season."""
    url = ("https://baseballsavant.mlb.com/leaderboard/pitch-arsenal-stats"
           f"?type=pitcher&year={season}&min=50&csv=true")
    out = {}
    try:
        r = requests.get(url, headers=HEADERS, timeout=TIMEOUT)
        r.raise_for_status()
        import io, csv
        reader = csv.DictReader(io.StringIO(r.text))
        agg = {}
        for row in reader:
            try:
                pid = int(row.get("player_id") or row.get("\ufeffplayer_id"))
            except (TypeError, ValueError):
                continue
            try:
                usage = float(row.get("pitch_usage") or 0) / 100.0
                whiff = float(row.get("whiff_percent") or 0) / 100.0
            except (TypeError, ValueError):
                continue
            if usage <= 0 or whiff <= 0:
                continue
            w, u = agg.get(pid, (0.0, 0.0))
            agg[pid] = (w + usage * whiff, u + usage)
        for pid, (w, u) in agg.items():
            if u > 0:
                out[pid] = w / u
    except Exception:
        pass
    return out


def load_savant_arsenal(season: int, kind: str = "batter") -> dict:
    """
    Pull Savant pitch-arsenal-stats. Returns {player_id: {pitch_type: value}}:
      * batter  -> value = est_woba (xwOBA) vs that pitch type
      * pitcher -> value = pitch_usage as a fraction (how often thrown)
    """
    url = ("https://baseballsavant.mlb.com/leaderboard/pitch-arsenal-stats"
           f"?type={kind}&year={season}&min=50&csv=true")
    out = {}
    try:
        r = requests.get(url, headers=HEADERS, timeout=TIMEOUT)
        r.raise_for_status()
        import io, csv
        reader = csv.DictReader(io.StringIO(r.text))
        for row in reader:
            try:
                pid = int(row.get("player_id") or row.get("﻿player_id"))
                pt = (row.get("pitch_type") or "").strip()
            except (TypeError, ValueError):
                continue
            if not pt:
                continue
            if kind == "batter":
                try:
                    v = float(row.get("est_woba") or 0)
                except (TypeError, ValueError):
                    continue
                if v > 0:
                    out.setdefault(pid, {})[pt] = v
            else:
                try:
                    v = float(row.get("pitch_usage") or 0) / 100.0
                except (TypeError, ValueError):
                    continue
                if v > 0:
                    out.setdefault(pid, {})[pt] = v
    except Exception:
        pass
    return out


def league_reliever_tb_per_bf_default() -> float:
    """Bullpen TB/BF default (relievers run a touch better than overall league)."""
    return 0.345


# --------------------------------------------------------------------------- #
# Weather (Open-Meteo, free, no API key)                                       #
# --------------------------------------------------------------------------- #
def get_weather(lat: float, lon: float, iso_dt_utc: str):
    """Return (temp_f, wind_mph, wind_dir_from_deg) at the game hour, or (None,)*3."""
    if not iso_dt_utc:
        return None, None, None
    date = iso_dt_utc[:10]
    try:
        hour = int(iso_dt_utc[11:13])
    except ValueError:
        return None, None, None
    try:
        d = _get("https://api.open-meteo.com/v1/forecast",
                 latitude=lat, longitude=lon,
                 hourly="temperature_2m,wind_speed_10m,wind_direction_10m",
                 temperature_unit="fahrenheit", wind_speed_unit="mph",
                 start_date=date, end_date=date, timezone="UTC")
        h = d["hourly"]
        times = h["time"]
        idx = min(range(len(times)), key=lambda i: abs(int(times[i][11:13]) - hour))
        return (h["temperature_2m"][idx], h["wind_speed_10m"][idx], h["wind_direction_10m"][idx])
    except Exception:
        return None, None, None


# --------------------------------------------------------------------------- #
# Convenience: build a fully-populated slate                                  #
# --------------------------------------------------------------------------- #
def build_slate(date: str, season: int, recent_days: int = 0,
                want_weather: bool = False, park_geo: dict = None,
                max_workers: int = 24) -> list[Matchup]:
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

    # batch handedness for all hitters in one call per chunk (instead of one each)
    try:
        ids = [str(b.mlbam_id) for b in batters if b.mlbam_id]
        hand = {}
        for i in range(0, len(ids), 100):
            chunk = ",".join(ids[i:i + 100])
            people = _get(f"{STATSAPI}/people", personIds=chunk)
            for person in people.get("people", []):
                hand[person.get("id")] = person.get("batSide", {}).get("code", "")
        for b in batters:
            if not b.bats:
                b.bats = hand.get(b.mlbam_id, "")
    except Exception:
        pass

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

    if want_weather and park_geo:
        def _do_weather(mu: Matchup):
            geo = park_geo.get(mu.venue)
            if not geo or geo.get("roof") == "dome":
                return
            mu.temp_f, mu.wind_mph, mu.wind_dir = get_weather(
                geo["lat"], geo["lon"], mu.game_datetime)
        with ThreadPoolExecutor(max_workers=max_workers) as ex:
            list(ex.map(_do_weather, games))

    return games
