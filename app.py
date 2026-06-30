"""
MLB Total Bases — daily slate model.

Pulls today's confirmed lineups + probable pitchers, runs your Log5 + regression
projection on every batter vs the opposing starter, and ranks edges against the
odds you paste in. Built to run as a website (Streamlit Community Cloud) — no
download required.
"""

import datetime as dt
import math
import json

import pandas as pd
import streamlit as st

import engine as E
import park_factors as PF
import data as D
import tracker as T

st.set_page_config(page_title="MLB TB Model", page_icon="⚾", layout="wide")

# --- Deployment version guard: catch stale/partial uploads with a clear message ---
import inspect as _inspect
_stale = []
for _attr in ("LEAGUE_K_PA",):
    if not hasattr(E, _attr):
        _stale.append(f"engine.{_attr}")
try:
    if "sp_share" not in _inspect.signature(E.project_hrr).parameters:
        _stale.append("engine.project_hrr(sp_share)")
except Exception:
    _stale.append("engine.project_hrr")
if _stale:
    st.error(
        "⚠️ The server is running an out-of-date **engine.py** (missing: "
        + ", ".join(_stale)
        + "). Re-upload engine.py (and the full file set) to GitHub, then reboot from "
        "Manage app → Reboot."
    )
    st.stop()
# --- end guard ---

st.markdown("""<style>
footer {visibility: hidden;}
.block-container {max-width: 1320px;}
h1, h2, h3 {font-weight: 600; letter-spacing: -0.01em;}
[data-testid="stMetric"] {background: #161B22; border: 1px solid #262C36;
    border-radius: 12px; padding: 14px 18px;}
[data-testid="stMetricValue"] {font-weight: 600; color: #F2F4F6;}
[data-testid="stMetricLabel"] {color: #9AA1AC;}
section[data-testid="stSidebar"] h2 {font-size: 0.8rem; text-transform: uppercase;
    letter-spacing: 0.06em; color: #8A9099; font-weight: 600;}
[data-testid="stDataFrame"] {border: 1px solid #262C36; border-radius: 12px;}
div.stButton > button {border-radius: 8px; font-weight: 600;}
</style>""", unsafe_allow_html=True)

st.markdown(
    '<div style="display:flex;align-items:center;gap:12px;margin-bottom:1.1rem;">'
    '<span style="font-size:1.9rem;line-height:1;">⚾</span>'
    '<div>'
    '<div style="font-size:1.45rem;font-weight:600;letter-spacing:-0.02em;line-height:1.1;">MLB Total Bases Model</div>'
    '<div style="color:#9AA1AC;font-size:0.88rem;">Daily slate projections · log5 + Statcast · park &amp; weather adjusted</div>'
    '</div></div>',
    unsafe_allow_html=True)

# --------------------------------------------------------------------------- #
# Sidebar: settings                                                           #
# --------------------------------------------------------------------------- #
# Load saved sidebar settings (one blob) so controls persist across reloads/restarts
_ui_saved_raw = T.get_setting("ui_settings", "") or ""
try:
    _ui = json.loads(_ui_saved_raw) if _ui_saved_raw else {}
except Exception:
    _ui = {}

def _seed(k, default):
    if k not in st.session_state:
        st.session_state[k] = _ui.get(k, default)

_seed("ui_prop", "Total Bases")
_seed("ui_line", 1.5); _seed("ui_tossup", 0.03); _seed("ui_minedge", 0.05)
_seed("ui_kelly", 0.25); _seed("ui_maxstake", 5.0); _seed("ui_shrink", 0.0); _seed("ui_confstake", True)
_seed("ui_regtb", int(E.REG_K_PA)); _seed("ui_reghrr", int(E.REG_K_PA)); _seed("ui_regkmod", int(getattr(E, "REG_KMODEL", 70)))
_seed("ui_splits", True); _seed("ui_homeaway", True); _seed("ui_components", True)
_seed("ui_method", "Exact distribution (recommended)"); _seed("ui_calib", False)
_seed("ui_hrrdisp", 1.35); _seed("ui_kdisp", 1.4); _seed("ui_lineupctx", 0.5); _seed("ui_tbtemp", 1.5); _seed("ui_hrrtemp", 2.0); _seed("ui_ktemp", 1.4); _seed("ui_autocal", False)
_seed("ui_park", True); _seed("ui_parkstr", 1.0); _seed("ui_weather", False); _seed("ui_weatherstr", 1.0)
_seed("ui_autobp", True); _seed("ui_spshare", 1.0); _seed("ui_bprate", 0.345)
_seed("ui_statcast", True); _seed("ui_wstatcast", 0.5); _seed("ui_arsenal", False)
_seed("ui_recent", True); _seed("ui_recentdays", 21); _seed("ui_wrecent", 0.35)
_seed("ui_kwhiff", True); _seed("ui_wkwhiff", 0.5)

with st.sidebar:
    st.header("Slate")
    date = st.date_input("Date", dt.date.today())
    season = st.number_input("Stats season", 2015, 2030, dt.date.today().year)
    prop = st.radio("Prop", ["Total Bases", "Hits + Runs + RBIs", "Pitcher Strikeouts"], key="ui_prop",
                    help="Pitcher Ks: projected from each batter in the day's lineup (vs-hand K%), PA-weighted.")
    STAT = "TB" if prop.startswith("Total") else ("HRR" if prop.startswith("Hits") else "K")
    proj_col = {"TB": "Proj TB", "HRR": "Proj HRR", "K": "Proj Ks"}[STAT]

    with st.expander("Lines & staking", expanded=False):
        default_line = st.number_input("Default TB line", 0.5, 5.5, step=0.5, key="ui_line")
        tossup_band = st.slider("Toss-up band (± from 50%)", 0.0, 0.10, step=0.01, key="ui_tossup",
                                help="Within this of 50% = 'No clear lean'.")
        min_edge = st.slider("Flag VALUE at edge ≥", 0.0, 0.20, step=0.01, key="ui_minedge")
        kelly_mult = st.slider("Kelly fraction", 0.1, 1.0, step=0.05, key="ui_kelly",
                               help="0.25 = quarter Kelly.")
        conf_stake = st.checkbox("Scale stakes by confidence", key="ui_confstake",
                                 help="Shrink the Kelly stake for thinner-sample picks: 5★ full, 4★ 0.9, 3★ 0.7, 2★ 0.5, 1★ 0.25. Tuned so 4★ (the realistic top) stays near full size.")
        max_stake = st.number_input("Max stake (% bankroll)", 0.5, 25.0, step=0.5, key="ui_maxstake")
        conf_shrink = st.slider("Shrink toward market (optional)", 0.0, 0.6, step=0.05, key="ui_shrink")
        try:
            _bk0 = float(T.get_setting("start_bankroll", 1000.0) or 1000.0)
        except (ValueError, TypeError):
            _bk0 = 1000.0
        if "start_bk" not in st.session_state:
            st.session_state["start_bk"] = _bk0
        start_bk = st.number_input("Bankroll ($)", 1.0, 1e9, step=50.0, key="start_bk",
                                   help="Current balance; used for sizing + ledger. Remembered.")
        if float(start_bk) != _bk0:
            try:
                T.set_setting("start_bankroll", float(start_bk))
            except Exception:
                pass

    with st.expander("Model & matchup", expanded=False):
        st.caption("Regression-to-league sample size, per metric (each stat stabilizes at a different point):")
        reg_tb = st.number_input("Regression — Total Bases (PA)", 0, 600, step=5, key="ui_regtb",
                                 help="TB/SLG stabilizes slowly — ~175 PA.")
        reg_hrr = st.number_input("Regression — H+R+RBI (PA)", 0, 600, step=5, key="ui_reghrr",
                                  help="H+R+RBI rates; ~175 PA, same ballpark as TB. Calibration is handled by the per-prop temperature.")
        reg_kmod = st.number_input("Regression — Strikeouts (BF)", 0, 400, step=5, key="ui_regkmod",
                                   help="K rate stabilizes fast (~70 BF). Kept separate so low-K pitchers aren't washed to league.")
        use_splits = st.checkbox("Use L/R handedness splits", key="ui_splits")
        use_homeaway = st.checkbox("Home/away splits (regressed)", key="ui_homeaway")
        use_components = st.checkbox("Per-event log5 (advanced)", key="ui_components")
        use_calibration = st.checkbox("Apply confidence compression", key="ui_calib",
                                      help="Master on/off. Pulls probabilities toward 50% to fix overconfidence. Off = raw model probabilities.")
        auto_cal = st.checkbox("Auto-fit temperature from graded data", key="ui_autocal", disabled=not use_calibration,
                               help="Fit the temperature from results instead of setting it by hand.")
        st.caption("Per-prop compression temperature (1.0 = none; higher pulls toward 50%):")
        tb_temp = st.slider("TB temperature", 1.0, 5.0, step=0.05, key="ui_tbtemp",
                            disabled=(not use_calibration) or auto_cal)
        hrr_temp = st.slider("H+R+RBI temperature", 1.0, 5.0, step=0.05, key="ui_hrrtemp",
                             disabled=(not use_calibration) or auto_cal)
        k_temp = st.slider("Ks temperature", 1.0, 5.0, step=0.05, key="ui_ktemp",
                           disabled=(not use_calibration) or auto_cal)
        TEMPS = {"TB": tb_temp, "HRR": hrr_temp, "K": k_temp}
        hrr_disp = st.slider("H+R+RBI variance (lower = more confident)", 1.0, 2.0, step=0.05, key="ui_hrrdisp",
                             help="Overdispersion for H+R+RBI. 1.0 = Poisson (most confident); higher spreads probabilities toward 50%. Tune via the confidence-vs-actual tracker.")
        k_disp = st.slider("Strikeouts variance (lower = more confident)", 1.0, 2.0, step=0.05, key="ui_kdisp",
                           help="Overdispersion for pitcher Ks. 1.0 = Poisson; higher accounts for workload swings.")
        lineup_ctx = st.slider("H+R+RBI lineup-spot context", 0.0, 1.0, step=0.05, key="ui_lineupctx",
                               help="Re-rates a hitter's R/RBI for today's batting slot (RBI up in the middle, runs up at the top). Damped because season rates already reflect a player's usual spot. 0 = off.")

    with st.expander("Park & weather", expanded=False):
        use_park = st.checkbox("Apply park factors", key="ui_park")
        park_strength = st.slider("Park factor strength", 0.0, 1.5, step=0.05, key="ui_parkstr", disabled=not use_park)
        use_weather = st.checkbox("Apply weather (Open-Meteo)", key="ui_weather",
                                  help="Per-game temp + wind out/in on HR/XBH. Domes auto-neutral.")
        weather_strength = st.slider("Weather strength", 0.0, 1.5, step=0.05, key="ui_weatherstr", disabled=not use_weather)

    with st.expander("Bullpen split", expanded=False):
        auto_bullpen = st.checkbox("Auto starter/bullpen split", key="ui_autobp")
        sp_share_manual = st.slider("Manual share of PAs vs starter", 0.40, 1.00, step=0.05, key="ui_spshare", disabled=auto_bullpen)
        bullpen_rate = st.number_input("Bullpen TB/BF (later PAs)", 0.28, 0.42, step=0.005, format="%.3f", key="ui_bprate")

    with st.expander("Statcast & recent form", expanded=False):
        use_statcast = st.checkbox("Blend Statcast expected (xSLG)", key="ui_statcast")
        w_statcast = st.slider("Weight on expected vs actual", 0.0, 1.0, step=0.05, key="ui_wstatcast", disabled=not use_statcast)
        use_arsenal = st.checkbox("Pitch-type matchup (arsenal)", key="ui_arsenal",
                                  help="Weights the hitter's xwOBA by pitch type vs the starter's pitch mix. Sharpens borderline calls.")
        use_recent = st.checkbox("Blend recent form", key="ui_recent")
        recent_days = st.slider("Window (days)", 7, 45, step=1, key="ui_recentdays", disabled=not use_recent)
        w_recent = st.slider("Weight on recent vs season", 0.0, 1.0, step=0.05, key="ui_wrecent", disabled=not use_recent)
        st.caption("Pitcher Strikeouts prop:")
        use_kwhiff = st.checkbox("Stabilize Ks with SwStr% (whiff)", key="ui_kwhiff",
                                 help="Blends a swinging-strike-implied K rate into the pitcher's K%. SwStr stabilizes faster than raw K% — most useful early season.")
        w_kwhiff = st.slider("Weight on SwStr-implied K", 0.0, 1.0, step=0.05, key="ui_wkwhiff", disabled=not use_kwhiff)

# Persist sidebar settings (one write only when something changed)
_uikeys = ["ui_prop", "ui_line", "ui_tossup", "ui_minedge", "ui_kelly", "ui_maxstake", "ui_shrink", "ui_confstake",
           "ui_regtb", "ui_reghrr", "ui_regkmod", "ui_splits", "ui_homeaway", "ui_components", "ui_calib", "ui_autocal", "ui_hrrdisp", "ui_kdisp", "ui_lineupctx", "ui_tbtemp", "ui_hrrtemp", "ui_ktemp",
           "ui_park", "ui_parkstr", "ui_weather", "ui_weatherstr", "ui_autobp", "ui_spshare", "ui_bprate",
           "ui_statcast", "ui_wstatcast", "ui_arsenal", "ui_recent", "ui_recentdays", "ui_wrecent",
           "ui_kwhiff", "ui_wkwhiff"]
_newui = json.dumps({k: st.session_state.get(k) for k in _uikeys}, default=str, sort_keys=True)
if _newui != _ui_saved_raw:
    try:
        T.set_setting("ui_settings", _newui)
    except Exception:
        pass

fg_rates = {}


# --------------------------------------------------------------------------- #
# Load slate                                                                  #
# --------------------------------------------------------------------------- #
@st.cache_data(ttl=900, show_spinner="Pulling lineups & stats from MLB API…")
def load(date_str: str, season: int, want_recent: bool, recent_days: int, want_weather: bool):
    geo = dict(PF.PARK_GEO)
    for alias, real in PF.PARK_ALIASES.items():
        if real in PF.PARK_GEO:
            geo[alias] = PF.PARK_GEO[real]
    return D.build_slate(date_str, season,
                         recent_days=recent_days if want_recent else 0,
                         want_weather=want_weather, park_geo=geo)

@st.cache_data(ttl=3600, show_spinner="Pulling Statcast expected stats…")
def load_savant(season: int):
    return D.load_savant_expected(season, "batter"), D.load_savant_expected(season, "pitcher")

@st.cache_data(ttl=3600, show_spinner="Pulling pitch-arsenal data…")
def load_arsenal(season: int):
    fn = getattr(D, "load_savant_arsenal", None)
    if fn is None:
        return {}, {}
    try:
        return fn(season, "batter"), fn(season, "pitcher")
    except Exception:
        return {}, {}

@st.cache_data(ttl=3600, show_spinner="Pulling SwStr / whiff data…")
def load_whiff(season: int):
    fn = getattr(D, "load_pitcher_whiff", None)
    if fn is None:
        return {}
    try:
        return fn(season)
    except Exception:
        return {}

@st.cache_data(ttl=86400, show_spinner=False)
def load_league_rates(season: int):
    fn = getattr(D, "league_event_rates", None)
    if fn is None:
        return None
    try:
        return fn(season)
    except Exception:
        return None

def _bets_cached():
    if "bets_cache" not in st.session_state:
        st.session_state["bets_cache"] = T.read_bets()
    return st.session_state["bets_cache"]

def _log_cached():
    if "log_cache" not in st.session_state:
        st.session_state["log_cache"] = T.read_log()
    return st.session_state["log_cache"]

def _invalidate_tracker_cache():
    st.session_state.pop("bets_cache", None)
    st.session_state.pop("log_cache", None)

colA, colB = st.columns([1, 5])
with colA:
    go = st.button("Load slate", type="primary")

if go:
    try:
        st.session_state["slate"] = load(date.isoformat(), int(season), use_recent, int(recent_days), use_weather)
        st.session_state["slate_date"] = date.isoformat()
        st.session_state["savant"] = load_savant(int(season)) if use_statcast else ({}, {})
        st.session_state["arsenal"] = load_arsenal(int(season)) if use_arsenal else ({}, {})
        st.session_state["whiff"] = load_whiff(int(season)) if use_kwhiff else {}
    except Exception as ex:
        st.error(f"Could not load slate: {ex}")

slate = st.session_state.get("slate")
savant_bat, savant_pit = st.session_state.get("savant", ({}, {}))
arse_bat, arse_pit = st.session_state.get("arsenal", ({}, {}))
whiff_map = st.session_state.get("whiff", {})
import statistics as _stats
_wv = [v for v in whiff_map.values() if v and v > 0]
league_whiff = _stats.median(_wv) if _wv else None
_lr = load_league_rates(int(season))
if _lr:
    E.LEAGUE_EVENT_RATES.update({k: _lr[k] for k in ("1B", "2B", "3B", "HR") if k in _lr})
    E.LEAGUE_HRR.update({k: _lr[k] for k in ("H", "R", "RBI") if k in _lr})
    if "TB" in _lr: E.LEAGUE_TB_PER_PA = _lr["TB"]
    if "R" in _lr:  E.LEAGUE_R_PER_BF = _lr["R"]
    if _lr.get("K", 0) > 0 and hasattr(E, "LEAGUE_K_PA"): E.LEAGUE_K_PA = _lr["K"]
    if "TB" in _lr and "H" in _lr:
        st.caption(f"League baseline (current season): {_lr['TB']:.3f} TB/PA, {_lr['H']:.3f} H/PA "
                   "— model auto-adjusts to the run environment.")
league_rate = E.LEAGUE_TB_PER_PA
E.HRR_DISPERSION = float(hrr_disp)
E.K_DISPERSION = float(k_disp)
if not slate:
    st.info("Pick a date and click **Load slate**. Lineups appear ~3–4 hours before first pitch; "
            "until then you'll see probable pitchers but empty lineups.")
    st.stop()

T_cal, T_caln = 1.0, 0
if use_calibration and auto_cal:
    try:
        T_cal, T_caln = T.calibration_temperature(_log_cached())
    except Exception:
        T_cal, T_caln = 1.0, 0
    T_total = float(T_cal)
    TEMP_MAP = {pp: float(T_cal) for pp in ("TB", "HRR", "K")}
elif use_calibration:
    T_total = float(TEMPS.get(STAT, 1.0))
    TEMP_MAP = {pp: float(TEMPS.get(pp, 1.0)) for pp in ("TB", "HRR", "K")}
else:
    T_total = 1.0
    TEMP_MAP = {pp: 1.0 for pp in ("TB", "HRR", "K")}

def _calibrate(p):
    if T_total == 1.0 or not (0 < p < 1):
        return p
    lp = math.log(p / (1 - p)) / T_total
    return 1.0 / (1.0 + math.exp(-lp))

if use_calibration:
    if auto_cal:
        st.caption(f"Confidence compression: AUTO {round(T_cal, 3)} from {T_caln} graded legs (all props).")
    else:
        st.caption(f"Confidence compression (per prop): TB {tb_temp} · H+R+RBI {hrr_temp} · Ks {k_temp}. "
                   f"Active for {STAT}: {round(T_total, 3)}. >1 pulls probabilities toward 50%.")

# Dynamic bankroll: realized P&L so far updates the current bankroll used for sizing.
_bets = _bets_cached()
_log = _log_cached()
_realized = 0.0
try:
    _gg = _bets[_bets["graded"].astype(str).isin(["1", "1.0", "True"])]
    _realized = float(pd.to_numeric(_gg["profit"], errors="coerce").fillna(0).sum())
except Exception:
    _realized = 0.0
current_bk = float(start_bk)
st.caption(f"Bankroll for sizing: **${current_bk:,.2f}** (current balance, as entered — keep it updated).")


# --------------------------------------------------------------------------- #
# Project every batter vs opposing starter                                    #
# --------------------------------------------------------------------------- #
K_LINE_DEFAULT = 5.5

def project_pitcher_k(pitcher, lineup):
    """PA-weighted projected strikeouts for a starter vs the day's lineup (vs-hand K%)."""
    if not pitcher or not lineup:
        return None
    # Note: a starter with no season stats yet (debut) is kept — his K rate simply
    # regresses to league inside the loop, rather than being dropped.
    total_k = bf_used = 0.0
    for b in lineup:
        k_pa, k_n = b.k_per_pa_vs(pitcher.throws)
        bk = E.regress(k_pa, k_n, E.LEAGUE_K_PA, int(reg_kmod))
        p_k_bf, p_k_n = pitcher.k_per_bf_vs(b.bats) if use_splits else (pitcher.k_per_bf, pitcher.bf)
        pk = E.regress(p_k_bf, p_k_n, E.LEAGUE_K_PA, int(reg_kmod))
        if use_kwhiff and whiff_map and league_whiff:
            implied = E.swstr_implied_k(whiff_map.get(pitcher.mlbam_id), league_whiff)
            if implied:
                tilt = max(0.0, (250.0 - p_k_n) / 250.0)        # lean on SwStr when BF small
                w_eff = min(1.0, w_kwhiff + (1 - w_kwhiff) * 0.5 * tilt)
                pk = w_eff * implied + (1 - w_eff) * pk
        km = E.log5_rate(bk, pk, E.LEAGUE_K_PA)
        tp = PF.expected_pa(b.order)
        if pitcher.bf_per_start > 0:
            exp_pa = E.pa_vs_starter(b.order, pitcher.bf_per_start, tp)
            eff_pa = E.tto_weighted(b.order, pitcher.bf_per_start, tp, E.TTO_K_MULT)  # times-through taper
        else:
            exp_pa = eff_pa = tp
        total_k += km * eff_pa
        bf_used += exp_pa
    return total_k, bf_used


def build_k_rows(slate):
    rows = []
    for mu in slate:
        for pit, opp_lineup, opp_team in [(mu.home_pitcher, mu.away_lineup, mu.away),
                                          (mu.away_pitcher, mu.home_lineup, mu.home)]:
            res = project_pitcher_k(pit, opp_lineup)
            if not res:
                continue
            lam, bf = res
            rows.append({
                "Game": f"{mu.away} @ {mu.home}", "Batter": pit.name, "Slot": "",
                "B": pit.throws, "vs Pitcher": f"vs {opp_team}", "P": "",
                "Line": None, "vsSP%": round(bf),
                "Proj Ks": round(lam, 2), "P(Over)": None,
                "Fair Over odds": None,
                "Conf": E.confidence_score(pit.bf, pit.bf, 100, False),
                "_bid": pit.mlbam_id, "_lam": round(lam, 4), "_dist": None,
                "_b_rate": 0, "_p_rate": round(pit.k_per_bf, 3),
                "_matchup": round(lam / bf, 3) if bf else 0, "_exp_pa": round(bf, 1),
                "_park": 1.0, "_bpa": round(pit.bf), "_pbf": round(pit.bf), "_recent": None,
                "Venue": mu.venue, "Wx": "",
            })
    return rows


def project_side(batters, opp_pitcher, venue, wmult=None, batter_is_home=False, pitcher_is_home=False):
    rows = []
    if not opp_pitcher:
        return rows
    for b in batters:
        # ---- common game geometry ----
        total_pa = PF.expected_pa(b.order)
        if auto_bullpen and opp_pitcher.bf_per_start > 0:
            this_share = E.pa_vs_starter(b.order, opp_pitcher.bf_per_start, total_pa) / total_pa
        else:
            this_share = sp_share_manual
        side = b.bats
        if side == "S":
            side = "L" if opp_pitcher.throws == "R" else "R"
        _splitpa = b.pa_vs_l if opp_pitcher.throws == "L" else b.pa_vs_r
        if use_park:
            pmult = 1 + (PF.park_mult_hand(venue, side) - 1) * park_strength
            park_ev = PF.park_event_hand(venue, side, park_strength)
        else:
            pmult, park_ev = 1.0, None
        if wmult:
            park_ev = PF.combine_event_mults(park_ev, wmult)

        # ---- Hits + Runs + RBIs ----
        if STAT == "HRR":
            if use_splits:
                er = b.event_rates_vs(opp_pitcher.throws)
                h_pa = sum(er.values()) if er else b.hits_per_pa
                pr = opp_pitcher.event_rates_allowed_vs(side)
                p_h_bf = sum(pr.values()) if pr else opp_pitcher.h_per_bf
            else:
                h_pa, p_h_bf = b.hits_per_pa, opp_pitcher.h_per_bf
            if not h_pa:
                continue
            ha = b.home_away_factor(batter_is_home) if use_homeaway else 1.0
            if use_arsenal and b.mlbam_id in arse_bat and opp_pitcher.mlbam_id in arse_pit:
                ha *= E.arsenal_factor(arse_bat[b.mlbam_id], arse_pit[opp_pitcher.mlbam_id],
                                       savant_bat.get(b.mlbam_id, {}).get("xwoba", 0.320))
            park_runs = pmult * (wmult.get("HR", 1.0) if wmult else 1.0)
            _r_ctx, _rbi_ctx = PF.lineup_run_context(b.order, lineup_ctx)
            _tto = 1.0
            if opp_pitcher.bf_per_start > 0:
                _sppa = E.pa_vs_starter(b.order, opp_pitcher.bf_per_start, total_pa)
                if _sppa > 0:
                    _tto = E.tto_weighted(b.order, opp_pitcher.bf_per_start, total_pa, E.TTO_TB_MULT) / _sppa
            lam, p_cover = E.project_hrr(
                h_pa * ha, b.pa, p_h_bf, max(opp_pitcher.bf, 1),
                b.runs_per_pa * ha, b.rbi_per_pa * ha, opp_pitcher.r_per_bf,
                line=default_line, side="Over", expected_pa=total_pa,
                park_hits=pmult, park_runs=park_runs, reg_k=int(reg_hrr), sp_share=this_share,
                r_ctx=_r_ctx, rbi_ctx=_rbi_ctx, tto=_tto)
            p_cover = _calibrate(p_cover)
            rows.append({
                "Batter": b.name, "Slot": b.order, "B": b.bats,
                "vs Pitcher": opp_pitcher.name, "P": opp_pitcher.throws,
                "Line": default_line, "vsSP%": round(this_share * 100),
                "Proj HRR": round(lam, 2), "P(Over)": round(p_cover, 3),
                "Fair Over odds": round(E.prob_to_american(p_cover), 0),
                "Conf": E.confidence_score(b.pa, opp_pitcher.bf, _splitpa, use_splits),
                "_bid": b.mlbam_id, "_b_rate": round(h_pa, 3), "_p_rate": round(p_h_bf, 3),
                "_matchup": round(lam / total_pa, 3) if total_pa else 0,
                "_exp_pa": round(total_pa, 2), "_park": round(pmult, 3),
                "_bpa": round(b.pa), "_pbf": round(opp_pitcher.bf), "_recent": None,
                "_dist": None, "_lam": round(lam, 4),
            })
            continue

        # ---- Total Bases ----
        if use_splits:
            b_rate, b_n = b.tb_per_pa_vs(opp_pitcher.throws)
            p_rate = (opp_pitcher.tb_per_bf_vs_l if b.bats in ("L", "S")
                      else opp_pitcher.tb_per_bf_vs_r) or opp_pitcher.tb_per_bf
        else:
            b_rate, b_n = b.tb_per_pa, b.pa
            p_rate = opp_pitcher.tb_per_bf
        if b.name.lower() in fg_rates:
            b_rate = fg_rates[b.name.lower()]["tb_per_pa"]
            b_n = fg_rates[b.name.lower()]["pa"]
        b_rate0 = b_rate
        if use_recent and b.recent_pa > 0:
            recent_rate = b.recent_tb / b.recent_pa
            eff_w = w_recent * b.recent_pa / (b.recent_pa + 50.0)
            b_rate = eff_w * recent_rate + (1 - eff_w) * b_rate
        p_rate0 = p_rate
        if use_statcast and b.mlbam_id in savant_bat:
            b_rate = E.blend(b_rate, b_rate * savant_bat[b.mlbam_id]["luck"], w_statcast)
        if use_statcast and opp_pitcher.mlbam_id in savant_pit:
            p_rate = E.blend(p_rate, p_rate * savant_pit[opp_pitcher.mlbam_id]["luck"], w_statcast)
        if use_homeaway:
            b_rate *= b.home_away_factor(batter_is_home)
            p_rate *= opp_pitcher.home_away_factor(pitcher_is_home)
        if use_arsenal and b.mlbam_id in arse_bat and opp_pitcher.mlbam_id in arse_pit:
            b_rate *= E.arsenal_factor(arse_bat[b.mlbam_id], arse_pit[opp_pitcher.mlbam_id],
                                       savant_bat.get(b.mlbam_id, {}).get("xwoba", 0.320))
        if not b_rate or not p_rate:
            continue
        shares = b.hit_shares()
        ht = E.HitTypeShares(*shares) if shares else E.HitTypeShares()
        ber = per = None
        if use_components:
            raw_b = b.event_rates_vs(opp_pitcher.throws)
            raw_p = opp_pitcher.event_rates_allowed_vs(side)
            if raw_b and raw_p:
                b_adj = b_rate / b_rate0 if b_rate0 else 1.0
                p_adj = p_rate / p_rate0 if p_rate0 else 1.0
                ber = {ev: E.regress(raw_b[ev], b_n, E.LEAGUE_EVENT_RATES[ev], int(reg_tb)) * b_adj
                       for ev in raw_b}
                per = {ev: E.regress(raw_p[ev], max(opp_pitcher.bf, 1), E.LEAGUE_EVENT_RATES[ev], int(reg_tb)) * p_adj
                       for ev in raw_p}
        inp = E.ProjectionInput(
            batter_tb_per_pa=b_rate, batter_pa_sample=b_n,
            pitcher_tb_per_pa_allowed=p_rate, pitcher_bf_sample=max(opp_pitcher.bf, 1),
            line=default_line, side="Over",
            expected_pa=total_pa, park_mult=pmult,
            shares=ht, sp_share=this_share, bullpen_rate=bullpen_rate,
            league=league_rate, reg_k=int(reg_tb),
            batter_event_rates=ber, pitcher_event_rates=per,
            park_event_mult=park_ev,
        )
        r = E.project(inp)
        p_cover = r.p_cover
        p_cover = _calibrate(p_cover)
        rows.append({
            "Batter": b.name, "Slot": b.order, "B": b.bats,
            "vs Pitcher": opp_pitcher.name, "P": opp_pitcher.throws,
            "Line": default_line,
            "vsSP%": round(this_share * 100),
            "Proj TB": round(r.lam, 2),
            "P(Over)": round(p_cover, 3),
            "Fair Over odds": round(E.prob_to_american(p_cover), 0),
            "Conf": E.confidence_score(b_n, opp_pitcher.bf, _splitpa, use_splits),
            "_b_rate": round(r.batter_rate, 3), "_p_rate": round(r.pitcher_rate, 3),
            "_bid": b.mlbam_id,
            "_matchup": round(r.matchup_rate, 3), "_exp_pa": round(total_pa, 2),
            "_park": round(pmult, 3), "_bpa": round(b_n), "_pbf": round(opp_pitcher.bf),
            "_recent": (round(b.recent_tb / b.recent_pa, 3) if (use_recent and b.recent_pa > 0) else None),
            "_dist": list(r.distribution), "_lam": round(r.lam, 4),
        })
    return rows


def matchup_weather(mu):
    try:
        if not use_weather or not PF.weather_applies(mu.venue):
            return None, ""
        if mu.temp_f is None and mu.wind_mph is None:
            return None, ""
        geo = PF.PARK_GEO.get(PF.PARK_ALIASES.get(mu.venue, mu.venue))
        out = PF.wind_out_component(mu.wind_mph or 0, mu.wind_dir or 0, geo["cf_bearing"]) if geo else 0.0
        wm = PF.weather_event_mult(mu.temp_f, out, base_temp=PF.park_normal_temp(mu.venue))
        if weather_strength != 1.0:
            wm = {k: 1 + (v - 1) * weather_strength for k, v in wm.items()}
        if mu.temp_f is None:
            return wm, ""
        arrow = "out" if out >= 0 else "in"
        return wm, f"{round(mu.temp_f)}° {abs(round(out))}mph {arrow}"
    except Exception:
        return None, ""

# Compute projections ONLY when the slate is (re)loaded, then freeze them in the
# session so reruns (typing odds, logging bets) and forecast updates don't move them.
def _build_batter_rows(slate):
    all_rows = []
    for mu in slate:
        wm, wx = matchup_weather(mu)
        all_rows += [{**r, "Game": f"{mu.away} @ {mu.home}", "Venue": mu.venue, "Wx": wx}
                     for r in project_side(mu.away_lineup, mu.home_pitcher, mu.venue, wm,
                                           batter_is_home=False, pitcher_is_home=True)]
        all_rows += [{**r, "Game": f"{mu.away} @ {mu.home}", "Venue": mu.venue, "Wx": wx}
                     for r in project_side(mu.home_lineup, mu.away_pitcher, mu.venue, wm,
                                           batter_is_home=True, pitcher_is_home=False)]
    return all_rows

def _log_all_props(slate, date_str):
    """Log TB + H+R+RBI for the whole slate, plus any Ks you've priced (line pulled
    from the odds sheet so they can be graded). One click covers every prop with odds.
    Temporarily flips the prop globals to build each, then restores them."""
    total = 0
    _save_stat, _save_pc = STAT, proj_col
    try:
        _olook = {}
        for (_d, _pr, _gm, _bt), _rec in T.read_odds().items():
            _olook[(T._iso(_d), str(_pr).upper(), str(_bt).strip())] = _rec
    except Exception:
        _olook = {}
    try:
        for _pp, _pcc in (("TB", "Proj TB"), ("HRR", "Proj HRR")):
            globals()["STAT"] = _pp
            globals()["proj_col"] = _pcc
            _rows = _build_batter_rows(slate)
            if _rows:
                total += T.log_projections(pd.DataFrame(_rows), date_str, prop=_pp, proj_col=_pcc)
        globals()["STAT"] = "K"
        globals()["proj_col"] = "Proj Ks"
        _keep = []
        for _r in build_k_rows(slate):
            _ln = _olook.get((T._iso(date_str), "K", str(_r["Batter"]).strip()), {}).get("line")
            try:
                _lnf = float(_ln)
            except (TypeError, ValueError):
                continue
            _r = dict(_r)
            _r["Line"] = _lnf
            _r["P(Over)"] = _calibrate(E.p_cover_negbin(_r["_lam"], _lnf, "Over", E.K_DISPERSION))
            _keep.append(_r)
        if _keep:
            total += T.log_projections(pd.DataFrame(_keep), date_str, prop="K", proj_col="Proj Ks")
    finally:
        globals()["STAT"] = _save_stat
        globals()["proj_col"] = _save_pc
    return total

_proj_stale = st.session_state.get("proj_meta") != (date.isoformat(), STAT)
if go or st.session_state.get("proj_df") is None or _proj_stale:
    if STAT == "K":
        all_rows = build_k_rows(slate)
    else:
        all_rows = _build_batter_rows(slate)
    if not all_rows:
        st.warning("No projections yet — lineups likely not posted. Probable pitchers below.")
        for mu in slate:
            st.write(f"**{mu.away} @ {mu.home}** — "
                     f"{mu.away_pitcher.name if mu.away_pitcher else 'TBD'} vs "
                     f"{mu.home_pitcher.name if mu.home_pitcher else 'TBD'}")
        st.stop()
    _frozen = pd.DataFrame(all_rows)
    st.session_state["proj_df"] = _frozen
    st.session_state["proj_meta"] = (date.isoformat(), STAT)



df = st.session_state["proj_df"]

def _lean(row):
    p = row["P(Over)"]
    if p is None or (isinstance(p, float) and pd.isna(p)):
        return "—"
    p = float(p)
    if abs(p - 0.5) < tossup_band:
        return "No lean"
    return "Over" if p > 0.5 else "Under"
df["Lean"] = df.apply(_lean, axis=1)
def _lean_pct(row):
    p = row["P(Over)"]
    if p is None or (isinstance(p, float) and pd.isna(p)) or row["Lean"] not in ("Over", "Under"):
        return None
    p = float(p)
    return (p if p >= 0.5 else 1 - p) * 100
df["Lean %"] = df.apply(_lean_pct, axis=1)

bid_map = {(r["Game"], r["Batter"]): (r.get("_bid", 0), r["vs Pitcher"], r.get("Venue", ""))
           for _, r in df.iterrows()}
dist_map = {(r["Game"], r["Batter"]): r.get("_dist") for _, r in df.iterrows()}
lam_map = {(r["Game"], r["Batter"]): r.get("_lam") for _, r in df.iterrows()}

def cover_at(game, batter, line, side="Over"):
    """Recompute cover prob at an arbitrary line from the stored distribution/lam."""
    k = (game, batter)
    d = dist_map.get(k)
    if d is not None:
        return _calibrate(E.p_cover_from_dist(list(d), line, side))
    lam = lam_map.get(k)
    if lam is not None:
        if STAT == "K":
            return _calibrate(E.p_cover_negbin(float(lam), line, side, E.K_DISPERSION))
        if STAT == "HRR":
            return _calibrate(E.p_cover_negbin(float(lam), line, side, E.HRR_DISPERSION))
        return _calibrate(E.p_cover_poisson(float(lam), line, side))
    return None
_fd, _fs = st.session_state.get("proj_meta", ("", STAT))
st.caption(f"Projections frozen from your last load ({_fd}, {_fs}). "
           "Change settings or date? Click **Load slate** to recompute.")

# --------------------------------------------------------------------------- #
# Summary + projections                                                       #
# --------------------------------------------------------------------------- #
tab_bet, tab_perf, tab_paper, tab_clv, tab_gameday = st.tabs(["📊 Projections & Odds", "📈 Performance", "💰 Paper Bankroll", "🎯 Closing Lines (CLV)", "📰 Game Day"])

with tab_bet:
    m1, m2, m3, m4 = st.columns(4)
    m1.metric("Games", len(slate))
    m2.metric("Hitters projected", len(df))
    m3.metric(f"Avg projected {STAT}", f"{df[proj_col].mean():.2f}")
    m4.metric("Highest projection", f"{df[proj_col].max():.2f}")
    st.write("")

    st.subheader("Projections")
    st.caption(f"Every hitter vs the opposing starter, sorted by projected {STAT}. Add odds below for edges.")
    _exp_n = sum(1 for _mu in slate for _b in (_mu.home_lineup + _mu.away_lineup)
                 if getattr(_b, "expected", False))
    if _exp_n:
        st.warning(f"⚠ {_exp_n} hitters are from PROJECTED lineups (each team's last game), not today's "
                   "confirmed order. Get your picks in now, then reload when official lineups post to update "
                   "the changed spots (usually 7–9).")
    view_cols = ["Game", "Batter", "Slot", "B", "vs Pitcher", "P", "Line",
                 "vsSP%", proj_col, "P(Over)", "Lean", "Lean %", "Fair Over odds", "Conf", "Venue", "Wx"]
    dfv = df[view_cols].sort_values(proj_col, ascending=False).copy()
    dfv["P(Over)"] = dfv["P(Over)"] * 100
    dfv["Conf"] = dfv["Conf"].apply(lambda n: "★" * int(n) if pd.notna(n) else "")
    st.dataframe(
        dfv, use_container_width=True, hide_index=True,
        column_config={
            "vs Pitcher": st.column_config.TextColumn("vs Pitcher", width="medium"),
            "vsSP%": st.column_config.NumberColumn("vs SP", format="%d%%", help="Share of PAs vs the starter"),
            proj_col: st.column_config.NumberColumn(proj_col, format="%.2f"),
            "P(Over)": st.column_config.NumberColumn("P(Over)", format="%.1f%%"),
            "Lean %": st.column_config.NumberColumn("Lean %", format="%.0f%%", help="Model probability on the side it leans"),
            "Fair Over odds": st.column_config.NumberColumn("Fair Over", format="%+d"),
            "Conf": st.column_config.TextColumn("Conf", help="Data-quality confidence (sample size + split depth), not edge"),
            "Wx": st.column_config.TextColumn("Weather"),
        })

    if STAT == "K":
        with st.expander("Strikeout projection detail (diagnose over/under-projection)"):
            _kcols = [c for c in ["Batter", "_p_rate", "_exp_pa", "_matchup", proj_col] if c in df.columns]
            _kd = df[_kcols].copy()
            _kd = _kd.rename(columns={"Batter": "Pitcher", "_p_rate": "Season K/BF",
                                      "_exp_pa": "BF faced", "_matchup": "Eff K/BF", proj_col: "Proj Ks"})
            st.dataframe(_kd.sort_values("Proj Ks", ascending=False), use_container_width=True, hide_index=True,
                         column_config={
                             "Season K/BF": st.column_config.NumberColumn("Season K/BF", format="%.3f"),
                             "Eff K/BF": st.column_config.NumberColumn("Eff K/BF (used)", format="%.3f"),
                             "BF faced": st.column_config.NumberColumn("BF faced", format="%.1f"),
                             "Proj Ks": st.column_config.NumberColumn("Proj Ks", format="%.2f")})
            st.caption("Read it like this: if **Season K/BF** clearly differs between pitchers but **Eff K/BF** "
                       "is nearly the same for everyone, the pitcher's rate is being washed toward league average — "
                       "lower the **Regression K (PA)** slider (Model & matchup), or turn off the SwStr blend to test.")

    # --- Why the strong picks (collapsed) ---
    def _summary(row):
        p_over = float(row["P(Over)"])
        sidelbl = row.get("Lean", "Over" if p_over >= 0.5 else "Under")
        lp = p_over if sidelbl == "Over" else 1 - p_over
        bits = [f"{row.get('B','?')}-bat vs {row.get('P','?')}HP {row.get('vs Pitcher','')}".strip()]
        pk = row.get("_park", 1.0) or 1.0
        if pk >= 1.03:
            bits.append("hitter-friendly park")
        elif pk <= 0.97:
            bits.append("pitcher-friendly park")
        if row.get("Wx"):
            bits.append(f"wx {row['Wx']}")
        rc, br = row.get("_recent"), row.get("_b_rate")
        if rc is not None and br:
            bits.append("running hot lately" if rc > br else "cold lately")
        samp = f"{row.get('_bpa','?')} PA / {row.get('_pbf','?')} BF faced"
        return (f"**{row['Batter']} {sidelbl} {row['Line']}** — model {lp*100:.0f}%. "
                + "; ".join(bits) + f". Based on {samp}.")

    _strong = df[df["Lean"].isin(["Over", "Under"])].copy()
    _strong["_p_side"] = _strong.apply(
        lambda r: float(r["P(Over)"]) if r["Lean"] == "Over" else 1 - float(r["P(Over)"]), axis=1)
    _strong["_lean"] = _strong["_p_side"]
    _strong = _strong[_strong["_p_side"] >= 0.56].sort_values("_p_side", ascending=False).head(3)
    with st.expander("Why the strong picks — top leans + reasoning", expanded=False):
        st.caption("Top cover-probability leans with their drivers. Stars = data confidence (sample/splits).")
        if _strong.empty:
            st.caption("No strong leans on this slate (no side at 57%+).")
        else:
            for _, _row in _strong.iterrows():
                _stars = "★" * int(_row.get("Conf", 3))
                _side = _row["Lean"]
                st.markdown(f"**{_row['Batter']} — {_side} {_row['Line']} · "
                            f"{_row['_p_side']*100:.0f}% · {_stars}**")
                st.markdown(_summary(_row))
                _brk = {
                    "Regressed batter rate": _row.get("_b_rate"),
                    "Pitcher allowed rate": _row.get("_p_rate"),
                    "Log5 matchup / PA": _row.get("_matchup"),
                    "Expected PA": _row.get("_exp_pa"),
                    "Park factor": _row.get("_park"),
                    "Weather": _row.get("Wx") or "—",
                    "Recent rate": _row.get("_recent") if _row.get("_recent") is not None else "—",
                    proj_col: _row.get(proj_col),
                    "Model P(Over)": f"{float(_row['P(Over)'])*100:.1f}%",
                    "Confidence (data)": _stars,
                }
                st.table(pd.DataFrame(list(_brk.items()), columns=["Factor", "Value"]))
                st.divider()

    st.subheader("Add your odds")
    st.caption("Enter the Over and/or Under price (American) for each batter. The model checks BOTH sides "
               "and surfaces whichever is +EV — so hitters that project under the line show up as Under value. "
               "If you enter both prices, the market is de-vigged for a cleaner edge.")
    odds_store = st.session_state.setdefault("odds_store", {})
    if not st.session_state.get("_odds_loaded"):
        try:
            odds_store.update(T.read_odds())
        except Exception:
            pass
        st.session_state["_odds_loaded"] = True

    def _okey(game, batter):
        return (date.isoformat(), STAT, str(game), str(batter))

    # Canonical stateful data_editor: a STABLE base (rebuilt only on load) + a fixed
    # key; edits live in the widget and come back via the return value. We never write
    # the output back into the base, which is what caused the revert before.
    def _pf(g, b, side):
        return str(odds_store.get(_okey(g, b), {}).get(side, "") or "")

    def _pf_line(g, b):
        v = odds_store.get(_okey(g, b), {}).get("line", None)
        try:
            return float(v)
        except (ValueError, TypeError):
            return None if STAT == "K" else float(default_line)

    _basekey = f"odds_base_{date.isoformat()}_{STAT}"
    _edkey = f"odds_ed_{date.isoformat()}_{STAT}"
    _odds_active = (date.isoformat(), STAT)
    # Rebuild (pre-filled from the saved store) on load OR whenever prop/date changes,
    # so switching props restores each prop's saved odds + lines. Stable within a prop so edits don't revert.
    if go or _basekey not in st.session_state or st.session_state.get("_odds_active") != _odds_active:
        base = df[["Game", "Batter"]].copy()
        base["Line"] = [_pf_line(g, b) for g, b in zip(base["Game"], base["Batter"])]
        base["Over odds"] = pd.Series([_pf(g, b, "over") for g, b in zip(base["Game"], base["Batter"])], dtype="object")
        base["Under odds"] = pd.Series([_pf(g, b, "under") for g, b in zip(base["Game"], base["Batter"])], dtype="object")
        st.session_state[_basekey] = base
        st.session_state.pop(_edkey, None)
    st.session_state["_odds_active"] = _odds_active

    st.caption("Edit **Line** per player for alt lines (e.g. 0.5 / 2.5) — the cover probability and edges recompute at the line you set.")
    edited = st.data_editor(
        st.session_state[_basekey], key=_edkey,
        use_container_width=True, hide_index=True,
        disabled=["Game", "Batter"],
        column_config={
            "Line": st.column_config.NumberColumn("Line", step=0.5, help="Alt line per player; cover prob recomputes here."),
            "Over odds": st.column_config.TextColumn("Over odds", help="American odds, e.g. +120 or -110"),
            "Under odds": st.column_config.TextColumn("Under odds", help="American odds, e.g. +120 or -110"),
        })
    # mirror entered odds into the cross-date store (read-only; does not feed the base)
    for _, _r in edited.iterrows():
        _o = str(_r["Over odds"] or "").strip()
        _u = str(_r["Under odds"] or "").strip()
        _l = _r.get("Line")
        rec = {}
        if _l is not None and not pd.isna(_l) and float(_l) != float(default_line):
            rec["line"] = float(_l)
        if _o:
            rec["over"] = _o
        if _u:
            rec["under"] = _u
        if rec:
            odds_store[_okey(_r["Game"], _r["Batter"])] = rec
        else:
            odds_store.pop(_okey(_r["Game"], _r["Batter"]), None)
    import json as _json
    _oh = _json.dumps({"|".join(k): v for k, v in sorted(odds_store.items())}, sort_keys=True)
    if _oh != st.session_state.get("_odds_hash"):
        try:
            T.write_odds(odds_store)
        except Exception:
            pass
        st.session_state["_odds_hash"] = _oh

    with st.expander("Import odds (paste or CSV) — auto-fill the table", expanded=False):
        st.caption("Columns: Player, Line, Over, Under (header optional; comma or tab separated). "
                   "Paste from BettingPros or a sheet, or upload a CSV. Matches by player name; "
                   "any column you omit is just skipped.")
        _imp_csv = st.file_uploader("CSV", type=["csv"], key="imp_csv")
        _imp_txt = st.text_area("…or paste rows here", height=130, key="imp_txt",
                                placeholder="Aaron Judge, 1.5, +120, -150")
        if st.button("Import odds"):
            import io, re, unicodedata

            def _rows_from_df(d):
                d = d.copy()
                d.columns = [str(c).strip().lower() for c in d.columns]
                def col(*names):
                    for n in names:
                        for c in d.columns:
                            if n in str(c):
                                return c
                    return None
                pcol, lcol, ocol, ucol = col("player", "name", "batter"), col("line"), col("over"), col("under")
                out = []
                if pcol is None:
                    cols = list(d.columns)
                    for _, r in d.iterrows():
                        v = [r[c] for c in cols]
                        if not v or str(v[0]).strip().lower() in ("player", "name", "batter"):
                            continue
                        out.append({"player": v[0],
                                    "line": v[1] if len(v) > 1 else None,
                                    "over": v[2] if len(v) > 2 else None,
                                    "under": v[3] if len(v) > 3 else None})
                else:
                    for _, r in d.iterrows():
                        out.append({"player": r.get(pcol),
                                    "line": r.get(lcol) if lcol else None,
                                    "over": r.get(ocol) if ocol else None,
                                    "under": r.get(ucol) if ucol else None})
                return out

            rows = []
            if _imp_csv is not None:
                try:
                    rows += _rows_from_df(pd.read_csv(_imp_csv, dtype=str))
                except Exception as ex:
                    st.error(f"CSV parse failed: {ex}")
            if _imp_txt and _imp_txt.strip():
                try:
                    sep = "\t" if "\t" in _imp_txt else ","
                    rows += _rows_from_df(pd.read_csv(io.StringIO(_imp_txt), sep=sep, dtype=str, header=None))
                except Exception as ex:
                    st.error(f"Paste parse failed: {ex}")

            def _norm(s):
                s = unicodedata.normalize("NFKD", str(s)).encode("ascii", "ignore").decode()
                return re.sub(r"\s+", " ", re.sub(r"[^a-z ]", "", s.lower())).strip()

            _exact, _liname = {}, {}
            for _, r in df.iterrows():
                n = _norm(r["Batter"])
                _exact[n] = (r["Game"], r["Batter"])
                p = n.split()
                if len(p) >= 2:
                    _liname[(p[0][:1], p[-1])] = (r["Game"], r["Batter"])

            def _match(name):
                n = _norm(name)
                if n in _exact:
                    return _exact[n]
                p = n.split()
                if len(p) >= 2 and (p[0][:1], p[-1]) in _liname:
                    return _liname[(p[0][:1], p[-1])]
                for k, v in _exact.items():
                    if p and k.split()[-1] == p[-1]:
                        return v
                return None

            matched, unmatched = 0, []
            for row in rows:
                if not row.get("player"):
                    continue
                key = _match(row["player"])
                if not key:
                    unmatched.append(str(row["player"]).strip())
                    continue
                rec = dict(odds_store.get(_okey(*key), {}))
                try:
                    if row.get("line") not in (None, "") and not pd.isna(row.get("line")):
                        rec["line"] = float(row["line"])
                except (ValueError, TypeError):
                    pass
                for side in ("over", "under"):
                    v = row.get(side)
                    if v is not None and str(v).strip() and str(v).strip().lower() != "nan":
                        rec[side] = str(v).strip().replace(" ", "")
                if rec:
                    odds_store[_okey(*key)] = rec
                    matched += 1
            st.session_state.pop(_basekey, None)
            st.session_state.pop(_edkey, None)
            try:
                T.write_odds(odds_store)
            except Exception:
                pass
            msg = f"Imported odds for {matched} players."
            if unmatched:
                msg += f" Unmatched ({len(unmatched)}): " + ", ".join(unmatched[:8])
            st.success(msg)
            st.rerun()

    def _num(x):
        if x is None or (isinstance(x, float) and pd.isna(x)):
            return None
        try:
            v = float(str(x).strip())
            return None if pd.isna(v) else v
        except (ValueError, TypeError):
            return None

    results = []
    _confmap = {(r["Game"], r["Batter"]): int(r.get("Conf", 3)) for _, r in df.iterrows()} if (df is not None and not df.empty and "Conf" in df.columns) else {}
    _CONF_FACTOR = {5: 1.0, 4: 0.9, 3: 0.7, 2: 0.5, 1: 0.25}
    for _, row in edited.iterrows():
        over_odds, under_odds = _num(row["Over odds"]), _num(row["Under odds"])
        if over_odds is None and under_odds is None:
            continue
        line = _num(row["Line"]) or (None if STAT == "K" else default_line)
        if line is None:
            continue
        p_over = cover_at(row["Game"], row["Batter"], line, "Over")
        if p_over is None:
            continue
        p_under = 1 - p_over
        # Fair market probs: de-vig if both prices present, else use the single implied price
        if over_odds is not None and under_odds is not None:
            fair_over, fair_under = E.no_vig_two_way(over_odds, under_odds)
        else:
            fair_over = E.american_to_implied(over_odds) if over_odds is not None else None
            fair_under = E.american_to_implied(under_odds) if under_odds is not None else None
        for sidelabel, p_model, odds, fair in (
            ("Over", p_over, over_odds, fair_over),
            ("Under", p_under, under_odds, fair_under)):
            if odds is None:
                continue
            payout = E.american_to_decimal_profit(odds)
            ev = p_model * payout - (1 - p_model)
            edge = p_model - (fair if fair is not None else E.american_to_implied(odds))
            if conf_shrink > 0:
                _mkt = fair if fair is not None else E.american_to_implied(odds)
                _p_size = (1 - conf_shrink) * p_model + conf_shrink * _mkt
            else:
                _p_size = p_model
            _cf = _CONF_FACTOR.get(_confmap.get((row["Game"], row["Batter"]), 3), 0.45) if conf_stake else 1.0
            kel = min(E.kelly_fraction(_p_size, odds) * kelly_mult * _cf, max_stake / 100.0)
            results.append({
                "Game": row["Game"], "Batter": row["Batter"], "Line": line,
                "Side": sidelabel, "Model P": round(p_model, 3), "Odds": odds,
                "Fair P": round(fair, 3) if fair is not None else None,
                "Edge": round(edge, 3), "Model EV": round(ev, 3),
                "Stake $": round(kel * current_bk, 2),
                "Verdict": "VALUE" if edge >= min_edge else ("Lean" if edge >= 0 else "Pass"),
            })

    if results:
        rdf = pd.DataFrame(results)
        fcol1, fcol2 = st.columns(2)
        only_plays = fcol1.checkbox("Only +EV sides", True)
        plus_only = fcol2.checkbox("Plus-money only (+odds)", False,
                                   help="Show only underdog prices — where a modest hit rate still profits.")
        if only_plays:
            rdf = rdf[rdf["Model EV"] >= 0]
        if plus_only:
            rdf = rdf[rdf["Odds"] > 0]
        rdf = rdf.sort_values("Model EV", ascending=False)
        _cmap = {(r["Game"], r["Batter"]): r.get("Conf", 3) for _, r in df.iterrows()}
        rdf["Conf"] = rdf.apply(lambda r: "★" * int(_cmap.get((r["Game"], r["Batter"]), 3)), axis=1)

        _val = rdf[rdf["Model EV"] > 0].sort_values("Model EV", ascending=False).head(5)
        if not _val.empty:
            st.subheader("Top 5 value plays")
            st.caption("Ranked by expected ROI at your price (not hit rate) — plus-money value floats up.")
            _vshow = _val[["Batter", "Side", "Line", "Odds", "Model P", "Model EV", "Edge", "Stake $", "Conf"]].copy()
            _vshow["Edge"] = _vshow["Edge"] * 100      # fraction -> percentage points
            _vshow["Model EV"] = _vshow["Model EV"] * 100
            _vshow["Model P"] = _vshow["Model P"] * 100
            st.dataframe(_vshow, use_container_width=True, hide_index=True,
                         column_config={
                             "Odds": st.column_config.NumberColumn("Odds", format="%+d"),
                             "Model P": st.column_config.NumberColumn("Model %", format="%.1f%%", help="Model probability on the side you're betting"),
                             "Model EV": st.column_config.NumberColumn("EV (ROI)", format="%+.1f%%"),
                             "Edge": st.column_config.NumberColumn("Edge", format="%+.1f%%"),
                             "Stake $": st.column_config.NumberColumn("Stake", format="$%.2f"),
                         })

        st.subheader("Ranked edges")

        def _verdict_style(v):
            return {"VALUE": "background-color:#10362C;color:#5DE0BB;font-weight:600",
                    "Lean": "background-color:#3A2E12;color:#E3B341",
                    "Pass": "color:#7A828C"}.get(v, "")
        _pct = lambda x: "—" if pd.isna(x) else f"{x:.2%}"
        _spct = lambda x: "—" if pd.isna(x) else f"{x:+.1%}"
        try:
            sty = rdf.style.format({"Model P": _pct, "Fair P": _pct, "Edge": _spct,
                                    "Model EV": _spct, "Odds": lambda x: f"{x:+.0f}",
                                    "Stake $": lambda x: f"${x:,.2f}"})
            sty = sty.map(_verdict_style, subset=["Verdict"]) if hasattr(sty, "map") \
                else sty.applymap(_verdict_style, subset=["Verdict"])
            st.dataframe(sty, use_container_width=True, hide_index=True)
        except Exception:
            st.dataframe(rdf, use_container_width=True, hide_index=True)
        st.download_button("Download edges (CSV)", rdf.to_csv(index=False),
                           file_name=f"tb_edges_{date.isoformat()}.csv")

        st.markdown("**Log the plays you're betting**")
        st.caption("Stake is in DOLLARS (defaults to Kelly fraction × your bankroll). "
                   "Edit it to what you actually bet, then tick and log — the bankroll ledger sums these.")
        _bsig = tuple((str(r["Batter"]), str(r["Side"]), str(r["Odds"]), str(r["Line"]))
                      for _, r in rdf.iterrows())
        _bkey = f"bet_base_{date.isoformat()}_{STAT}"
        _bedkey = f"bet_ed_{date.isoformat()}_{STAT}"
        if st.session_state.get(_bkey + "_sig") != _bsig:
            _bb = rdf[["Game", "Batter", "Side", "Line", "Odds"]].copy()
            _bb.insert(0, "Bet", False)
            _bb["Stake $"] = rdf["Stake $"].round(2).values
            st.session_state[_bkey] = _bb
            st.session_state[_bkey + "_sig"] = _bsig
            st.session_state.pop(_bedkey, None)
        bet_edited = st.data_editor(
            st.session_state[_bkey], key=_bedkey, hide_index=True, use_container_width=True,
            disabled=["Game", "Batter", "Side", "Line", "Odds"],
            column_config={"Bet": st.column_config.CheckboxColumn("Bet", help="Tick to log this play"),
                           "Stake $": st.column_config.NumberColumn("Stake ($)", min_value=0.0, step=1.0, format="$%.2f")})
        if st.button("✓ Log selected bets", type="primary"):
            brows = []
            for _, r in bet_edited.iterrows():
                if not bool(r["Bet"]):
                    continue
                try:
                    stake = float(r["Stake $"])
                except (ValueError, TypeError):
                    stake = 1.0
                bid, pitch, ven = bid_map.get((r["Game"], r["Batter"]), (0, "", ""))
                brows.append({"date": date.isoformat(), "batter": r["Batter"], "batter_id": bid,
                              "pitcher": pitch, "venue": ven, "line": r["Line"], "side": r["Side"],
                              "odds": r["Odds"], "stake": stake, "prop": STAT})
            if brows:
                try:
                    nb = T.log_bets(pd.DataFrame(brows))
                    _invalidate_tracker_cache()
                    st.success(f"Logged {nb} bet(s) to the sheet.")
                except Exception as ex:
                    st.error(f"Bet log failed: {ex}")
            else:
                st.warning("Tick at least one play first.")

    # --------------------------------------------------------------------------- #
    # Multi-book de-vig helper                                                     #
    # --------------------------------------------------------------------------- #
    with st.expander("Multi-book no-vig fair line calculator (Pinnacle/FD/DK/MGM/Caesars/Bovada)"):
        st.caption("Enter over/under prices per book to get a weighted no-vig fair line, like your 'Weighted AVG' sheet.")
        books = ["Pinnacle", "FanDuel", "DraftKings", "MGM", "Caesars", "Bovada"]
        default_w = {"Pinnacle": 0.2, "FanDuel": 0.2, "DraftKings": 0.2,
                     "MGM": 0.1, "Caesars": 0.1, "Bovada": 0.1}
        book_df = pd.DataFrame({"Book": books,
                                "Over": ["" for _ in books],
                                "Under": ["" for _ in books],
                                "Weight": [default_w[b] for b in books]})
        be = st.data_editor(book_df, hide_index=True, use_container_width=True)
        lines = []
        for _, r in be.iterrows():
            try:
                lines.append(E.BookLine(r["Book"], float(r["Over"]), float(r["Under"]), float(r["Weight"])))
            except (ValueError, TypeError):
                continue
        if lines:
            res = E.weighted_no_vig(lines)
            if res["fair_over"]:
                st.metric("Fair Over probability", f"{res['fair_over']*100:.1f}%",
                          help=f"Fair Over odds ≈ {res['fair_over_american']:+.0f}")

    # --------------------------------------------------------------------------- #
    # Accuracy tracker                                                            #
    # --------------------------------------------------------------------------- #

with tab_perf:
    st.divider()
    st.subheader("Accuracy tracker")
    st.caption(f"Storage: {T.backend_name()}. Log a slate, then grade it after games finish "
               "to track projection error and calibration over time.")

    tc1, tc2 = st.columns(2)
    with tc1:
        if st.button("Log projections", help="Logs all props (TB + H+R+RBI + priced Ks) for the loaded slate."):
            try:
                _slate_date = st.session_state.get("slate_date", date.isoformat())
                n = _log_all_props(slate, _slate_date)
                _invalidate_tracker_cache()
                st.success(f"Logged {n} projections (all props) for {date.isoformat()}.")
            except Exception as ex:
                st.error(f"Log failed: {ex}")
    with tc2:
        if st.button("Grade results", help="Grades all ungraded projections and bets across every prop."):
            try:
                n = T.grade(int(season))
                nb = T.grade_bets(int(season))
                _invalidate_tracker_cache()
                st.success(f"Graded {n} projections and {nb} bets.")
            except Exception as ex:
                st.error(f"Grade failed: {ex}")
        if st.button("Diagnose grading", help="Shows why ungraded rows aren't grading (date not final, missing id, no result found, etc.)."):
            try:
                _gd = T.grade_diagnostic(int(season))
                st.json(_gd)
            except Exception as ex:
                st.error(f"Diagnostic failed: {ex}")

    with st.expander("Backfill past dates (recompute + log + grade all props)"):
        st.caption("Reconstructs projections for past dates so prior priced picks land in the tracker. "
                   "Re-runs the model once per day (slow — an API pull each), logs TB + H+R+RBI + priced Ks, "
                   "then grades. Your existing odds attach automatically once the projection rows exist.")
        _bfc1, _bfc2 = st.columns(2)
        with _bfc1:
            _bf_start = st.date_input("From", date - dt.timedelta(days=7), key="bf_start")
        with _bfc2:
            _bf_end = st.date_input("To", date, key="bf_end")
        if st.button("Run backfill"):
            _days = (_bf_end - _bf_start).days + 1
            if _days < 1:
                st.error("End date must be on or after start date.")
            elif _days > 45:
                st.error("Range too large (max 45 days at a time) — run it in chunks.")
            else:
                _prog = st.progress(0.0, text="Backfilling…")
                _tot = 0
                for _k in range(_days):
                    _d = _bf_start + dt.timedelta(days=_k)
                    _di = _d.isoformat()
                    try:
                        _sl = load(_di, int(season), use_recent, int(recent_days), use_weather)
                        if _sl:
                            _tot += _log_all_props(_sl, _di)
                    except Exception:
                        pass
                    _prog.progress((_k + 1) / _days, text=f"Backfilling… {_di}")
                try:
                    _ng = T.grade(int(season))
                    _nb = T.grade_bets(int(season))
                except Exception:
                    _ng = _nb = 0
                _invalidate_tracker_cache()
                st.success(f"Backfill complete: logged {_tot} projections over {_days} day(s), "
                           f"graded {_ng} projections and {_nb} bets.")

    def _prop_view(plog, pbets, label, prop_temp=1.0):
        _m = T.metrics(plog)
        if _m:
            g1, g2, g3, g4 = st.columns(4)
            g1.metric("Graded", _m["n"])
            g2.metric("Avg error", f"{_m['mae']:.2f}")
            g3.metric("Bias (proj − actual)", f"{_m['bias']:+.2f}",
                      help="Positive = model projects too high on average")
            g4.metric("Prediction accuracy",
                      f"{_m['pred_acc']*100:.0f}%" if _m.get("pred_acc") is not None else "—",
                      help="How often the projection's over/under call (proj vs the line) matched the actual result.")
            pb = T.prediction_breakdown(plog)
            if not pb.empty:
                st.caption("Prediction accuracy — projection's over/under call vs actual")
                st.dataframe(pb, use_container_width=True, hide_index=True,
                             column_config={
                                 "Prediction": "Model called",
                                 "Correct": st.column_config.NumberColumn("Correct", format="%d%%")})
            cc = T.calibration_by_confidence(plog)
            if not cc.empty:
                with st.expander("Confidence vs actual hit rate"):
                    ccd = cc.copy()
                    ccd["confidence"] = (ccd["confidence"] * 100).round(0)
                    ccd["hit_rate"] = (ccd["hit_rate"] * 100).round(0)
                    ccd["gap"] = (ccd["gap"] * 100).round(0)
                    st.dataframe(ccd, use_container_width=True, hide_index=True,
                                 column_config={
                                     "bucket": "Confidence",
                                     "n": "Picks",
                                     "confidence": st.column_config.NumberColumn("Avg conf", format="%d%%"),
                                     "hit_rate": st.column_config.NumberColumn("Actual hit", format="%d%%"),
                                     "gap": st.column_config.NumberColumn("Gap", format="%+d pts")})
            cp = T.calibration_by_probability(plog, temp=float(prop_temp))
            if not cp.empty:
                with st.expander("Calibration by model probability — P(over)"):
                    st.caption(f"Does a P(over) of X% actually go over ~X%? 'Calibrated' applies your "
                               f"current {label} temperature ({prop_temp:g}) to preview the fix against ALL "
                               f"history — aim to flatten 'Gap (cal)' toward 0.")
                    cpd = cp.copy()
                    for _c in ("predicted", "calibrated", "over_rate", "gap", "gap_cal"):
                        if _c in cpd.columns:
                            cpd[_c] = (cpd[_c] * 100).round(0)
                    st.dataframe(cpd, use_container_width=True, hide_index=True,
                                 column_config={
                                     "bucket": "P(over) bucket",
                                     "n": "Picks",
                                     "predicted": st.column_config.NumberColumn("Avg P(over)", format="%d%%"),
                                     "calibrated": st.column_config.NumberColumn("Calibrated", format="%d%%"),
                                     "over_rate": st.column_config.NumberColumn("Actual over %", format="%d%%"),
                                     "gap": st.column_config.NumberColumn("Gap (raw)", format="%+d pts"),
                                     "gap_cal": st.column_config.NumberColumn("Gap (cal)", format="%+d pts")})
        else:
            st.caption(f"No graded {label} projections yet.")
        _bm = T.bet_metrics(pbets)
        if _bm:
            st.caption("Betting P&L — graded bets")
            b1, b2, b3, b4 = st.columns(4)
            b1.metric("Record", _bm["record"])
            b2.metric("Win rate", f"{_bm['win_rate']*100:.0f}%")
            b3.metric("$ P&L", f"${_bm['units_profit']:+,.2f}", help=f"{_bm['n']} bets, ${_bm['units_staked']:,.0f} staked")
            b4.metric("ROI", f"{_bm['roi']*100:+.1f}%")
        else:
            st.caption(f"No graded {label} bets yet.")

    _tabs = st.tabs(["Total Bases", "Hits + Runs + RBIs"])
    for _tab, _pp, _lbl in zip(_tabs, ["TB", "HRR"], ["Total Bases", "H+R+RBI"]):
        with _tab:
            _pl = _log[_log["prop"].astype(str).str.upper() == _pp] if not _log.empty else _log
            _pb = _bets[_bets["prop"].astype(str).str.upper() == _pp] if not _bets.empty else _bets
            _prop_view(_pl, _pb, _lbl, prop_temp=TEMP_MAP.get(_pp, 1.0))

    # Fixed performance anchor: the curve/peak/drawdown are built from a STORED starting
    # bankroll, so retyping the sizing bankroll no longer shifts the historical peak.
    _ps_saved = T.get_setting("perf_start", 235.0)
    try:
        _ps_saved = float(_ps_saved)
    except (TypeError, ValueError):
        _ps_saved = 235.0
    if _ps_saved <= 0:
        _ps_saved = 235.0
    _perf_start = st.number_input("Starting bankroll ($)", 1.0, 1e7, value=_ps_saved, step=5.0,
                                  key="perf_start_bk",
                                  help="Your fixed starting bankroll — anchors this curve AND the Paper Bankroll tab. Set once.")
    if float(_perf_start) != _ps_saved:
        try:
            T.set_setting("perf_start", float(_perf_start))
        except Exception:
            pass
    _allcurve = T.bankroll_curve(_bets, _perf_start)
    if not _allcurve.empty:
        st.subheader("Bankroll — combined (all props)")
        st.caption("Built from your fixed starting bankroll through logged bets. Peak and drawdown are historical.")
        _abs = T.bankroll_stats(_allcurve, _perf_start)
        _cc1, _cc2, _cc3 = st.columns(3)
        _cc1.metric("Bankroll (from results)", f"${_abs['current']:,.2f}", f"{_abs['growth_pct']:+.1f}%")
        _cc2.metric("Peak", f"${_abs['peak']:,.2f}")
        _cc3.metric("Max drawdown", f"{_abs['max_drawdown_pct']:.1f}%")
        st.line_chart(_allcurve.set_index("n")["bankroll"], height=260,
                      x_label="settled bets", y_label="bankroll ($)")

    with st.expander("Recent graded results (verify)"):
        _isdone = lambda d: d["graded"].astype(str).isin(["1", "1.0", "True"])
        _gl = _log[_isdone(_log)] if not _log.empty else _log
        if _gl is not None and not _gl.empty:
            st.caption("Projections graded vs actual")
            st.dataframe(_gl[["date", "batter", "prop", "line", "proj", "actual", "over_hit"]].tail(40),
                         use_container_width=True, hide_index=True)
        _gb = _bets[_isdone(_bets)] if not _bets.empty else _bets
        if _gb is not None and not _gb.empty:
            st.caption("Bets graded")
            st.dataframe(_gb[["date", "batter", "prop", "line", "side", "odds", "stake", "actual", "result", "profit"]].tail(40),
                         use_container_width=True, hide_index=True)
        if (_gl is None or _gl.empty) and (_gb is None or _gb.empty):
            st.caption("Nothing graded yet.")


with tab_paper:
    with st.expander("Paper bankroll — every model pick (verification)"):
        st.caption("Hypothetical flat 1-unit bankroll betting the model's lean on EVERY graded "
                   "projection at one assumed price — uses your full sample, not just real bets. "
                   "'vs break-even' = hit rate vs implied.")
        _avg_odds = T.avg_realized_odds(_bets)
        _default_odds = int(_avg_odds) if _avg_odds is not None else -110
        if _avg_odds is not None:
            st.caption(f"Your average realized price is **{_avg_odds:+d}** — using it as the default so "
                       "you're testing whether the model beats the price you actually get.")
        _olu = {}
        try:
            for (_d, _pr, _gm, _bt), _rec in T.read_odds().items():
                _olu[(T._iso(_d), str(_pr).upper(), str(_bt).strip())] = _rec
        except Exception:
            _olu = {}
        _pc1, _pc2, _pc3 = st.columns(3)
        with _pc1:
            _po = st.number_input("Fallback odds (American)", -400, 400, _default_odds, step=5, key="paper_odds",
                                  help="Used for picks you never entered odds on. Defaults to your average realized price.")
        with _pc2:
            _pev = st.checkbox("Only +EV picks", value=True, key="paper_ev")
        with _pc3:
            _use_real = st.checkbox("Use my entered odds", value=True, key="paper_real",
                                    help="Use the real price you entered per pick (from the odds sheet); fall back to the price at left where none was entered.")
        _real_only = st.checkbox("Grade only picks I have odds for (skip fallback)", value=True, key="paper_realonly",
                                 disabled=not _use_real,
                                 help="Truest paper bankroll of your actual candidates — excludes picks you never priced instead of pricing them at the fallback.")
        _stake_mode = st.radio("Stake", ["Flat 1u (measures edge)", "Kelly (fixed-fraction)"],
                               horizontal=True, key="paper_stake")
        _sm = "kelly" if _stake_mode.startswith("Kelly") else "flat"
        _pstart = T.get_setting("perf_start", 235.0)
        try:
            _pstart = float(_pstart)
        except (TypeError, ValueError):
            _pstart = 235.0
        if _pstart <= 0:
            _pstart = 235.0
        st.caption(f"Starting bankroll ${_pstart:,.0f} (set it in the Performance tab).")
        _psum, _pcurve = T.paper_sim(_log, odds=int(_po), only_plus_ev=_pev,
                                     odds_lookup=(_olu if _use_real else None),
                                     real_only=(_use_real and _real_only),
                                     stake_mode=_sm, kelly_mult=float(kelly_mult), temp_map=TEMP_MAP, start_units=_pstart,
                                     max_frac=float(max_stake) / 100.0)
        _cov = []
        for _cpp, _clbl in [("TB", "Total Bases"), ("HRR", "H+R+RBI"), ("K", "Pitcher Ks")]:
            _csub = _log[_log["prop"].astype(str).str.upper() == _cpp] if (_log is not None and not _log.empty) else _log
            _ctot = int(len(_csub)) if (_csub is not None and not _csub.empty) else 0
            _cgr = int(_csub["graded"].astype(str).isin(["1", "1.0", "True"]).sum()) if (_csub is not None and not _csub.empty) else 0
            _cps, _ = T.paper_sim(_csub, odds=int(_po), only_plus_ev=False, odds_lookup=_olu, real_only=True)
            _cov.append({"Prop": _clbl, "In log": _ctot, "Graded": _cgr, "Priced (real odds)": _cps.get("n", 0)})
        st.caption("Coverage — graded projections logged vs how many you've priced (paper uses priced ones):")
        st.dataframe(pd.DataFrame(_cov), hide_index=True, use_container_width=True)
        if st.button("↻ Refresh from sheet", key="paper_refresh"):
            _invalidate_tracker_cache()
            st.rerun()
        if _log is not None and not _log.empty:
            _allvc = (_log["prop"].astype(str).str.upper().replace("", "(blank)").value_counts().to_dict())
            _gdf = _log[_log["graded"].astype(str).isin(["1", "1.0", "True"])]
            _gvc = (_gdf["prop"].astype(str).str.upper().replace("", "(blank)").value_counts().to_dict()) if not _gdf.empty else {}
            st.caption(f"Diagnostic — all rows by prop: {_allvc} · graded by prop: {_gvc} · "
                       f"total rows {len(_log)}, graded {len(_gdf)}")
        else:
            st.caption("Diagnostic: the projection log (projections worksheet) reads as empty in this session.")
        if _psum.get("n"):
            _q1, _q2, _q3, _q4 = st.columns(4)
            _q1.metric("Paper bets", _psum["n"])
            _q2.metric("Hit rate", f"{_psum['hit_rate']*100:.1f}%",
                       f"{(_psum['hit_rate'] - _psum['breakeven'])*100:+.1f} vs break-even")
            _q3.metric("ROI", f"{_psum['roi']*100:+.1f}%")
            if _sm == "kelly":
                _q4.metric("Growth", f"{_psum.get('growth', 0)*100:+.1f}%",
                           help=f"Fixed-fraction Kelly off your starting bankroll (no compounding), {kelly_mult:g}x.")
            else:
                _q4.metric("$ P/L", f"${_psum['profit']:+,.0f}")
            if _use_real:
                st.caption(f"{_psum.get('n_real', 0)} of {_psum['n']} picks used your real entered odds; "
                           f"the rest used {int(_po):+d}.")
            if not _pcurve.empty:
                st.line_chart(_pcurve.set_index("n")["bankroll"], height=240,
                              x_label="paper bets", y_label="$")
            _pp_rows = []
            _pp_curves = []
            for _pp, _lbl in [("TB", "Total Bases"), ("HRR", "H+R+RBI"), ("K", "Pitcher Ks")]:
                _sub = _log[_log["prop"].astype(str).str.upper() == _pp] if not _log.empty else _log
                _s, _sc = T.paper_sim(_sub, odds=int(_po), only_plus_ev=_pev,
                                      odds_lookup=(_olu if _use_real else None),
                                      real_only=(_use_real and _real_only),
                                      stake_mode=_sm, kelly_mult=float(kelly_mult), temp_map=TEMP_MAP, start_units=_pstart,
                                     max_frac=float(max_stake) / 100.0)
                if _s.get("n"):
                    _pp_rows.append({"Prop": _lbl, "Bets": _s["n"],
                                     "Hit%": round(_s["hit_rate"]*100, 1),
                                     "Break-even%": round(_s["breakeven"]*100, 1),
                                     "ROI%": round(_s["roi"]*100, 1),
                                     "$ P/L": round(_s["profit"], 0)})
                    if not _sc.empty:
                        _pp_curves.append((_lbl, _s, _sc))
            if _pp_rows:
                st.markdown("**By prop**")
                st.dataframe(pd.DataFrame(_pp_rows), hide_index=True, use_container_width=True)
            for _lbl, _s, _sc in _pp_curves:
                st.caption(f"{_lbl} — paper bankroll  ·  {_s['n']} bets, "
                           f"{_s['hit_rate']*100:.1f}% hit, ROI {_s['roi']*100:+.1f}%")
                st.line_chart(_sc.set_index("n")["bankroll"], height=200,
                              x_label="paper bets", y_label="$")
            st.caption("Uses your real entered odds where available (else the fallback price). Picks you never "
                       "priced use the fallback, so coverage grows as you log more odds.")
        else:
            st.caption("No graded projections yet — log and grade some slates first.")


with tab_clv:
    with st.expander("Closing lines & CLV — enter the close for your bets"):
        st.caption("For each bet, enter the closing price of the side you took (American, e.g. -110). "
                   "CLV = how your price compares to the close; beating the close consistently is the "
                   "strongest evidence your edge is real, independent of wins/losses.")
        _cb = _bets.copy()
        if _cb.empty:
            st.caption("No bets logged yet.")
        else:
            _cb = _cb.reset_index(drop=True)
            if "close_odds" not in _cb.columns:
                _cb["close_odds"] = ""
            _cv = _cb[["date", "batter", "prop", "side", "line", "odds", "close_odds"]].copy()
            _cv["close_odds"] = _cv["close_odds"].fillna("").astype(str).replace("nan", "")
            _ced = st.data_editor(
                _cv, key="clv_editor", hide_index=True, use_container_width=True,
                disabled=["date", "batter", "prop", "side", "line", "odds"],
                column_config={"close_odds": st.column_config.TextColumn(
                    "Closing odds", help="American odds at/near close for the side you bet, e.g. -110")})
            if st.button("Save closing odds"):
                _cb["close_odds"] = _ced["close_odds"].values
                try:
                    T.write_bets(_cb)
                    _invalidate_tracker_cache()
                    st.success("Saved closing odds.")
                    st.rerun()
                except Exception as ex:
                    st.warning(f"Save failed: {ex}")
            _cm = T.clv_metrics(_cb)
            if _cm and _cm["n"]:
                cl1, cl2, cl3 = st.columns(3)
                cl1.metric("Bets with close", _cm["n"])
                cl2.metric("Avg CLV", f"{_cm['avg_clv']*100:+.1f}%",
                           help="Average % your price beat the closing price. Positive = good.")
                cl3.metric("Positive-CLV rate", f"{_cm['pos_rate']*100:.0f}%",
                           help="Share of bets that beat the close. Aim well above 50%.")
            else:
                st.caption("Enter closing odds above to see CLV metrics.")


with tab_gameday:
    st.subheader("Game Day")
    st.caption("Matchup, records, recent form, betting odds (ESPN), and your model's angle for each game.")
    if not slate:
        st.info("Load a slate to see game summaries.")
    else:
        if st.session_state.get("espn_date") != date.isoformat():
            st.session_state["espn_cache"] = (D.espn_mlb_games(date.isoformat()), D.espn_mlb_headlines(12))
            st.session_state["espn_date"] = date.isoformat()
        _eg, _news = st.session_state.get("espn_cache", ({}, []))
        for mu in slate:
            _gkey = f"{mu.away} @ {mu.home}"
            _ec = _eg.get(D._team_key(mu.home)) or _eg.get(D._team_key(mu.away)) or {}
            st.markdown(f"### {mu.away} @ {mu.home}")
            _meta = [x for x in [mu.venue, _ec.get("status", ""), _ec.get("odds", "")] if x]
            if _meta:
                st.caption(" · ".join(_meta))
            _ah, _hh = _ec.get("away", {}), _ec.get("home", {})
            _c1, _c2 = st.columns(2)
            with _c1:
                st.markdown(f"**{mu.away}**" + (f"  ({_ah.get('record','')})" if _ah.get("record") else ""))
                if _ah.get("form"):
                    st.caption(f"Last 10: {_ah['form']}")
                st.caption(f"SP: {mu.away_pitcher.name if mu.away_pitcher else 'TBD'}")
            with _c2:
                st.markdown(f"**{mu.home}**" + (f"  ({_hh.get('record','')})" if _hh.get("record") else ""))
                if _hh.get("form"):
                    st.caption(f"Last 10: {_hh['form']}")
                st.caption(f"SP: {mu.home_pitcher.name if mu.home_pitcher else 'TBD'}")
            _gd = df[df["Game"] == _gkey] if (df is not None and not df.empty and "Game" in df.columns) else None
            if _gd is not None and not _gd.empty and proj_col in _gd.columns:
                _cols = [c for c in ["Batter", proj_col, "P(Over)", "Lean"] if c in _gd.columns]
                _top = _gd.sort_values(proj_col, ascending=False).head(5)[_cols]
                st.caption(f"Model's top {STAT} projections in this game:")
                st.dataframe(_top, hide_index=True, use_container_width=True)
            st.divider()
        if _news:
            with st.expander("MLB headlines (ESPN)"):
                for _n in _news:
                    st.markdown(f"**{_n.get('headline','')}**")
                    if _n.get("desc"):
                        st.caption(_n["desc"])
        st.caption("Next: AI-written matchup narratives that synthesize all of the above — they need an LLM API key in the app's secrets (your choice of provider).")
