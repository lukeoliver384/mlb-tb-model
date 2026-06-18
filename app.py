"""
MLB Total Bases — daily slate model.

Pulls today's confirmed lineups + probable pitchers, runs your Log5 + regression
projection on every batter vs the opposing starter, and ranks edges against the
odds you paste in. Built to run as a website (Streamlit Community Cloud) — no
download required.
"""

import datetime as dt

import pandas as pd
import streamlit as st

import engine as E
import park_factors as PF
import data as D
import tracker as T

st.set_page_config(page_title="MLB TB Model", page_icon="⚾", layout="wide")

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
with st.sidebar:
    st.header("Slate")
    date = st.date_input("Date", dt.date.today())
    season = st.number_input("Stats season", 2015, 2030, dt.date.today().year)
    prop = st.radio("Prop", ["Total Bases", "Hits + Runs + RBIs"],
                    help="HRR: Hits is a clean matchup; Runs/RBIs are scaled by the pitcher's run-suppression + run environment (lineup context approximated).")
    STAT = "TB" if prop.startswith("Total") else "HRR"
    proj_col = "Proj TB" if STAT == "TB" else "Proj HRR"

    st.header("Model")
    league_rate = st.number_input("League TB/PA", 0.30, 0.45, E.LEAGUE_TB_PER_PA, 0.001, format="%.3f")
    reg_k = st.number_input("Regression K (PA)", 0, 600, E.REG_K_PA, 5)
    use_splits = st.checkbox("Use L/R handedness splits", True)
    use_homeaway = st.checkbox("Home/away splits (regressed)", True,
                               help="Small, heavily-regressed nudge from each player's home vs away TB rate.")
    use_park = st.checkbox("Apply park factors", True)
    park_strength = st.slider("Park factor strength", 0.0, 1.5, 1.0, 0.05, disabled=not use_park,
                              help="Scales how hard park factors push. 0 = ignore parks, 1 = full Savant factor, >1 = amplify.")
    use_weather = st.checkbox("Apply weather (Open-Meteo)", False,
                              help="Pulls temp + wind per game; adjusts HR/XBH via wind out/in and temperature. Per-event mode only. Domes auto-neutral.")
    weather_strength = st.slider("Weather strength", 0.0, 1.5, 1.0, 0.05, disabled=not use_weather)
    auto_bullpen = st.checkbox("Auto starter/bullpen split (from starter depth)", True,
                               help="Computes each batter's share of PAs vs the starter from his batters-faced/start. Uncheck to use a single slider.")
    sp_share_manual = st.slider("Manual share of PAs vs starter", 0.40, 1.00, 1.00, 0.05,
                                disabled=auto_bullpen)
    bullpen_rate = st.number_input("Bullpen TB/BF (later PAs)", 0.28, 0.42, 0.345, 0.005, format="%.3f")

    st.header("Statcast (expected TB)")
    use_statcast = st.checkbox("Blend Statcast expected (xSLG)", True)
    w_statcast = st.slider("Weight on expected vs actual", 0.0, 1.0, 0.5, 0.05, disabled=not use_statcast)

    st.header("Recent form")
    use_recent = st.checkbox("Blend recent form", True)
    recent_days = st.slider("Window (days)", 7, 45, 21, disabled=not use_recent)
    w_recent = st.slider("Weight on recent vs season", 0.0, 1.0, 0.35, 0.05, disabled=not use_recent)
    use_components = st.checkbox("Per-event log5 (advanced)", True,
                                help="Project 1B/2B/3B/HR separately via log5, then assemble the TB distribution.")
    method = st.radio("Cover-probability method", ["Exact distribution (recommended)", "Poisson (sheet original)"])
    default_line = st.number_input("Default TB line", 0.5, 5.5, 1.5, 0.5)
    min_edge = st.slider("Flag VALUE at edge ≥", 0.0, 0.20, 0.05, 0.01)
    kelly_mult = st.slider("Kelly fraction", 0.1, 1.0, 0.25, 0.05,
                           help="Fraction of full Kelly to stake. Default 0.25 = quarter Kelly. Stakes are % of bankroll.")
    max_stake = st.number_input("Max stake (% bankroll)", 0.5, 25.0, 5.0, 0.5,
                                help="Hard cap on any single bet, applied AFTER Kelly fractioning. Guards against model overconfidence.")
    conf_shrink = st.slider("Shrink toward market (optional)", 0.0, 0.6, 0.0, 0.05,
                            help="Optional: pulls the model probability toward the market before sizing. 0 = off (stake reflects the model's cover probability directly).")

    st.caption("Fangraphs CSV (optional) — overrides MLB-API batter TB/PA")
    fg_csv = st.file_uploader("Fangraphs batting export (.csv)", type=["csv"])

fg_rates = D.load_fangraphs_csv(fg_csv) if fg_csv else {}


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

colA, colB = st.columns([1, 5])
with colA:
    go = st.button("Load slate", type="primary")

if go:
    try:
        st.session_state["slate"] = load(date.isoformat(), int(season), use_recent, int(recent_days), use_weather)
        st.session_state["savant"] = load_savant(int(season)) if use_statcast else ({}, {})
    except Exception as ex:
        st.error(f"Could not load slate: {ex}")

slate = st.session_state.get("slate")
savant_bat, savant_pit = st.session_state.get("savant", ({}, {}))
if not slate:
    st.info("Pick a date and click **Load slate**. Lineups appear ~3–4 hours before first pitch; "
            "until then you'll see probable pitchers but empty lineups.")
    st.stop()


# --------------------------------------------------------------------------- #
# Project every batter vs opposing starter                                    #
# --------------------------------------------------------------------------- #
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
            park_runs = pmult * (wmult.get("HR", 1.0) if wmult else 1.0)
            lam, p_cover = E.project_hrr(
                h_pa * ha, b.pa, p_h_bf, max(opp_pitcher.bf, 1),
                b.runs_per_pa * ha, b.rbi_per_pa * ha, opp_pitcher.r_per_bf,
                line=default_line, side="Over", expected_pa=total_pa,
                park_hits=pmult, park_runs=park_runs, reg_k=int(reg_k))
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
                ber = {ev: E.regress(raw_b[ev], b_n, E.LEAGUE_EVENT_RATES[ev], int(reg_k)) * b_adj
                       for ev in raw_b}
                per = {ev: E.regress(raw_p[ev], max(opp_pitcher.bf, 1), E.LEAGUE_EVENT_RATES[ev], int(reg_k)) * p_adj
                       for ev in raw_p}
        inp = E.ProjectionInput(
            batter_tb_per_pa=b_rate, batter_pa_sample=b_n,
            pitcher_tb_per_pa_allowed=p_rate, pitcher_bf_sample=max(opp_pitcher.bf, 1),
            line=default_line, side="Over",
            expected_pa=total_pa, park_mult=pmult,
            shares=ht, sp_share=this_share, bullpen_rate=bullpen_rate,
            league=league_rate, reg_k=int(reg_k),
            batter_event_rates=ber, pitcher_event_rates=per,
            park_event_mult=park_ev,
        )
        r = E.project(inp)
        p_cover = r.p_cover if method.startswith("Exact") else r.p_cover_poisson
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
_proj_stale = st.session_state.get("proj_meta") != (date.isoformat(), STAT)
if go or st.session_state.get("proj_df") is None or _proj_stale:
    all_rows = []
    for mu in slate:
        wm, wx = matchup_weather(mu)
        all_rows += [{**r, "Game": f"{mu.away} @ {mu.home}", "Venue": mu.venue, "Wx": wx}
                     for r in project_side(mu.away_lineup, mu.home_pitcher, mu.venue, wm,
                                           batter_is_home=False, pitcher_is_home=True)]
        all_rows += [{**r, "Game": f"{mu.away} @ {mu.home}", "Venue": mu.venue, "Wx": wx}
                     for r in project_side(mu.home_lineup, mu.away_pitcher, mu.venue, wm,
                                           batter_is_home=True, pitcher_is_home=False)]
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
bid_map = {(r["Game"], r["Batter"]): (r.get("_bid", 0), r["vs Pitcher"], r.get("Venue", ""))
           for _, r in df.iterrows()}
dist_map = {(r["Game"], r["Batter"]): r.get("_dist") for _, r in df.iterrows()}
lam_map = {(r["Game"], r["Batter"]): r.get("_lam") for _, r in df.iterrows()}

def cover_at(game, batter, line, side="Over"):
    """Recompute cover prob at an arbitrary line from the stored distribution/lam."""
    k = (game, batter)
    d = dist_map.get(k)
    if d is not None:
        return E.p_cover_from_dist(list(d), line, side)
    lam = lam_map.get(k)
    if lam is not None:
        return E.p_cover_poisson(float(lam), line, side)
    return None
_fd, _fs = st.session_state.get("proj_meta", ("", STAT))
st.caption(f"Projections frozen from your last load ({_fd}, {_fs}). "
           "Change settings or date? Click **Load slate** to recompute.")

# --------------------------------------------------------------------------- #
# Summary + projections                                                       #
# --------------------------------------------------------------------------- #
m1, m2, m3, m4 = st.columns(4)
m1.metric("Games", len(slate))
m2.metric("Hitters projected", len(df))
m3.metric(f"Avg projected {STAT}", f"{df[proj_col].mean():.2f}")
m4.metric("Highest projection", f"{df[proj_col].max():.2f}")
st.write("")

st.subheader("Projections")
st.caption(f"Every hitter vs the opposing starter, sorted by projected {STAT}. Add odds below for edges.")
view_cols = ["Game", "Batter", "Slot", "B", "vs Pitcher", "P", "Line",
             "vsSP%", proj_col, "P(Over)", "Fair Over odds", "Conf", "Venue", "Wx"]
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
        "Fair Over odds": st.column_config.NumberColumn("Fair Over", format="%+d"),
        "Conf": st.column_config.TextColumn("Conf", help="Data-quality confidence (sample size + split depth), not edge"),
        "Wx": st.column_config.TextColumn("Weather"),
    })

# --- Why the strong picks ---
st.subheader("Why the strong picks")
st.caption("High cover-probability leans with the drivers behind them. The stars = how well-founded "
           "the number is (sample size + split depth) — a strong % on thin data gets fewer stars.")

def _summary(row):
    p_over = float(row["P(Over)"])
    sidelbl = "Over" if p_over >= 0.5 else "Under"
    lp = max(p_over, 1 - p_over)
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

_strong = df.copy()
_strong["_lean"] = _strong["P(Over)"].apply(lambda p: max(float(p), 1 - float(p)))
_strong = _strong[_strong["_lean"] >= 0.56].sort_values("_lean", ascending=False).head(3)
if _strong.empty:
    st.caption("No strong leans on this slate (no side at 57%+).")
else:
    for _, _row in _strong.iterrows():
        _stars = "★" * int(_row.get("Conf", 3))
        _side = "Over" if float(_row["P(Over)"]) >= 0.5 else "Under"
        with st.expander(f"{_row['Batter']} — {_side} {_row['Line']} · "
                         f"{_row['_lean']*100:.0f}% · {_stars}"):
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

_basekey = f"odds_base_{date.isoformat()}_{STAT}"
_edkey = f"odds_ed_{date.isoformat()}_{STAT}"
_odds_active = (date.isoformat(), STAT)
# Rebuild (pre-filled from the saved store) on load OR whenever prop/date changes,
# so switching props restores each prop's saved odds. Stable within a prop so edits don't revert.
if go or _basekey not in st.session_state or st.session_state.get("_odds_active") != _odds_active:
    base = df[["Game", "Batter", "Line", "P(Over)"]].copy()
    base["Over odds"] = pd.Series([_pf(g, b, "over") for g, b in zip(base["Game"], base["Batter"])], dtype="object")
    base["Under odds"] = pd.Series([_pf(g, b, "under") for g, b in zip(base["Game"], base["Batter"])], dtype="object")
    st.session_state[_basekey] = base
    st.session_state.pop(_edkey, None)
st.session_state["_odds_active"] = _odds_active

edited = st.data_editor(
    st.session_state[_basekey], key=_edkey,
    use_container_width=True, hide_index=True,
    disabled=["Game", "Batter", "Line", "P(Over)"],
    column_config={
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

def _num(x):
    if x is None or (isinstance(x, float) and pd.isna(x)):
        return None
    try:
        v = float(str(x).strip())
        return None if pd.isna(v) else v
    except (ValueError, TypeError):
        return None

results = []
for _, row in edited.iterrows():
    over_odds, under_odds = _num(row["Over odds"]), _num(row["Under odds"])
    if over_odds is None and under_odds is None:
        continue
    line = _num(row["Line"]) or default_line
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
        kel = min(E.kelly_fraction(_p_size, odds) * kelly_mult, max_stake / 100.0)
        results.append({
            "Game": row["Game"], "Batter": row["Batter"], "Line": line,
            "Side": sidelabel, "Model P": round(p_model, 3), "Odds": odds,
            "Fair P": round(fair, 3) if fair is not None else None,
            "Edge": round(edge, 3), "Model EV": round(ev, 3),
            "Kelly %": round(kel * 100, 2),
            "Verdict": "VALUE" if edge >= min_edge else ("Lean" if edge >= 0 else "Pass"),
        })

if results:
    rdf = pd.DataFrame(results)
    only_plays = st.checkbox("Show only +EV sides (hide Pass)", True)
    if only_plays:
        rdf = rdf[rdf["Edge"] >= 0]
    rdf = rdf.sort_values("Edge", ascending=False)
    _cmap = {(r["Game"], r["Batter"]): r.get("Conf", 3) for _, r in df.iterrows()}
    rdf["Conf"] = rdf.apply(lambda r: "★" * int(_cmap.get((r["Game"], r["Batter"]), 3)), axis=1)

    _val = rdf[rdf["Edge"] > 0].head(5)
    if not _val.empty:
        st.subheader("Top 5 value plays")
        st.caption("Highest edge vs the market you entered, with data-confidence stars.")
        _vshow = _val[["Batter", "Side", "Line", "Odds", "Edge", "Kelly %", "Conf"]].copy()
        _vshow["Edge"] = _vshow["Edge"] * 100      # fraction -> percentage points
        st.dataframe(_vshow, use_container_width=True, hide_index=True,
                     column_config={
                         "Odds": st.column_config.NumberColumn("Odds", format="%+d"),
                         "Edge": st.column_config.NumberColumn("Edge", format="%+.1f%%"),
                         "Kelly %": st.column_config.NumberColumn("Stake %", format="%.2f%%"),
                     })

    st.subheader("Ranked edges")

    def _verdict_style(v):
        return {"VALUE": "background-color:#10362C;color:#5DE0BB;font-weight:600",
                "Lean": "background-color:#3A2E12;color:#E3B341",
                "Pass": "color:#7A828C"}.get(v, "")
    _pct = lambda x: "—" if pd.isna(x) else f"{x:.1%}"
    _spct = lambda x: "—" if pd.isna(x) else f"{x:+.1%}"
    try:
        sty = rdf.style.format({"Model P": _pct, "Fair P": _pct, "Edge": _spct,
                                "Model EV": _spct, "Odds": lambda x: f"{x:+.0f}",
                                "Kelly %": lambda x: f"{x:.2f}%"})
        sty = sty.map(_verdict_style, subset=["Verdict"]) if hasattr(sty, "map") \
            else sty.applymap(_verdict_style, subset=["Verdict"])
        st.dataframe(sty, use_container_width=True, hide_index=True)
    except Exception:
        st.dataframe(rdf, use_container_width=True, hide_index=True)
    st.download_button("Download edges (CSV)", rdf.to_csv(index=False),
                       file_name=f"tb_edges_{date.isoformat()}.csv")

    st.markdown("**Log the plays you're betting**")
    st.caption("Stake defaults to the suggested fractional-Kelly size (% of bankroll). "
               "Tick the plays you took, adjust stakes if needed, then tap to log them to your sheet.")
    bet_edit = rdf[["Game", "Batter", "Side", "Line", "Odds"]].copy()
    bet_edit.insert(0, "Bet", False)
    bet_edit["Stake (u)"] = rdf["Kelly %"].round(2).values   # fractional-Kelly % of bankroll
    bet_edited = st.data_editor(
        bet_edit, hide_index=True, use_container_width=True,
        disabled=["Game", "Batter", "Side", "Line", "Odds"],
        column_config={"Bet": st.column_config.CheckboxColumn("Bet", help="Tick to log this play"),
                       "Stake (u)": st.column_config.NumberColumn("Stake (u)", min_value=0.0, step=0.5)})
    if st.button("✓ Log selected bets", type="primary"):
        brows = []
        for _, r in bet_edited.iterrows():
            if not bool(r["Bet"]):
                continue
            try:
                stake = float(r["Stake (u)"])
            except (ValueError, TypeError):
                stake = 1.0
            bid, pitch, ven = bid_map.get((r["Game"], r["Batter"]), (0, "", ""))
            brows.append({"date": date.isoformat(), "batter": r["Batter"], "batter_id": bid,
                          "pitcher": pitch, "venue": ven, "line": r["Line"], "side": r["Side"],
                          "odds": r["Odds"], "stake": stake, "prop": STAT})
        if brows:
            try:
                nb = T.log_bets(pd.DataFrame(brows))
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
st.divider()
st.subheader("Accuracy tracker")
st.caption(f"Storage: {T.backend_name()}. Log a slate, then grade it after games finish "
           "to track projection error and calibration over time.")

tc1, tc2 = st.columns(2)
with tc1:
    if st.button("Log today's projections"):
        try:
            n = T.log_projections(df, date.isoformat(), prop=STAT, proj_col=proj_col)
            st.success(f"Logged {n} projections for {date.isoformat()}.")
        except Exception as ex:
            st.error(f"Log failed: {ex}")
with tc2:
    if st.button("Grade past results"):
        try:
            n = T.grade(int(season))
            nb = T.grade_bets(int(season))
            st.success(f"Graded {n} projections and {nb} bets.")
        except Exception as ex:
            st.error(f"Grade failed: {ex}")

_log = T.read_log()
_m = T.metrics(_log)
if _m:
    g1, g2, g3, g4 = st.columns(4)
    g1.metric("Graded", _m["n"])
    g2.metric("Avg TB error", f"{_m['mae']:.2f}")
    g3.metric("Bias (proj − actual)", f"{_m['bias']:+.2f}",
              help="Positive = the model projects too high on average")
    g4.metric("Over rate: pred vs actual",
              f"{_m['over_rate_pred']*100:.0f}% / {_m['over_rate_actual']*100:.0f}%")
    cal = T.calibration(_log)
    if not cal.empty:
        st.caption("Calibration — predicted P(Over) vs actual hit rate by bucket")
        cal_disp = cal.copy()
        cal_disp["predicted"] = (cal_disp["predicted"] * 100).round(0)
        cal_disp["actual"] = (cal_disp["actual"] * 100).round(0)
        cal_disp["gap"] = (cal_disp["gap"] * 100).round(0)
        st.dataframe(cal_disp, use_container_width=True, hide_index=True,
                     column_config={
                         "bucket": "P(Over) bucket",
                         "predicted": st.column_config.NumberColumn("Predicted", format="%d%%"),
                         "actual": st.column_config.NumberColumn("Actual", format="%d%%"),
                         "gap": st.column_config.NumberColumn("Gap", format="%+d pts"),
                     })
else:
    st.info("No graded results yet. Log a slate, and after the games play, click **Grade past results**.")

_bets = T.read_bets()
_bm = T.bet_metrics(_bets)
if _bm:
    st.caption("Betting P&L — graded bets")
    b1, b2, b3, b4 = st.columns(4)
    b1.metric("Record", _bm["record"])
    b2.metric("Win rate", f"{_bm['win_rate']*100:.0f}%")
    b3.metric("Units P&L", f"{_bm['units_profit']:+.2f}", help=f"{_bm['n']} graded bets, {_bm['units_staked']:.1f}u staked")
    b4.metric("ROI", f"{_bm['roi']*100:+.1f}%")

    start_bk = st.number_input("Starting bankroll (units)", 1.0, 1_000_000.0, 100.0, 10.0,
                               help="Kelly stakes are % of bankroll, so the curve compounds from here.")
    curve = T.bankroll_curve(_bets, start_bk)
    if not curve.empty:
        bs = T.bankroll_stats(curve, start_bk)
        c1, c2, c3 = st.columns(3)
        c1.metric("Current bankroll", f"{bs['current']:.1f}", f"{bs['growth_pct']:+.1f}%")
        c2.metric("Peak", f"{bs['peak']:.1f}")
        c3.metric("Max drawdown", f"{bs['max_drawdown_pct']:.1f}%")
        st.line_chart(curve.set_index("n")["bankroll"], height=240,
                      x_label="settled bets", y_label="bankroll")
