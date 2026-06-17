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

st.set_page_config(page_title="MLB TB Model", layout="wide")
st.title("⚾ MLB Total Bases — Daily Slate Model")

# --------------------------------------------------------------------------- #
# Sidebar: settings                                                           #
# --------------------------------------------------------------------------- #
with st.sidebar:
    st.header("Slate")
    date = st.date_input("Date", dt.date.today())
    season = st.number_input("Stats season", 2015, 2030, dt.date.today().year)

    st.header("Model")
    league_rate = st.number_input("League TB/PA", 0.30, 0.45, E.LEAGUE_TB_PER_PA, 0.001, format="%.3f")
    reg_k = st.number_input("Regression K (PA)", 0, 600, E.REG_K_PA, 5)
    use_splits = st.checkbox("Use L/R handedness splits", True)
    use_park = st.checkbox("Apply park factors", True)
    sp_share = st.slider("Share of PAs vs the starter", 0.40, 1.00, 1.00, 0.05,
                         help="1.00 = ignore bullpen (matches your sheet). Lower blends in a league-average bullpen for later PAs.")
    method = st.radio("Cover-probability method", ["Exact distribution (recommended)", "Poisson (sheet original)"])
    default_line = st.number_input("Default TB line", 0.5, 5.5, 1.5, 0.5)
    min_edge = st.slider("Flag VALUE at edge ≥", 0.0, 0.20, 0.05, 0.01)

    st.caption("Fangraphs CSV (optional) — overrides MLB-API batter TB/PA")
    fg_csv = st.file_uploader("Fangraphs batting export (.csv)", type=["csv"])

fg_rates = D.load_fangraphs_csv(fg_csv) if fg_csv else {}


# --------------------------------------------------------------------------- #
# Load slate                                                                  #
# --------------------------------------------------------------------------- #
@st.cache_data(ttl=900, show_spinner="Pulling lineups & stats from MLB API…")
def load(date_str: str, season: int):
    return D.build_slate(date_str, season)

colA, colB = st.columns([1, 5])
with colA:
    go = st.button("Load slate", type="primary")

if go:
    try:
        st.session_state["slate"] = load(date.isoformat(), int(season))
    except Exception as ex:
        st.error(f"Could not load slate: {ex}")

slate = st.session_state.get("slate")
if not slate:
    st.info("Pick a date and click **Load slate**. Lineups appear ~3–4 hours before first pitch; "
            "until then you'll see probable pitchers but empty lineups.")
    st.stop()


# --------------------------------------------------------------------------- #
# Project every batter vs opposing starter                                    #
# --------------------------------------------------------------------------- #
def project_side(batters, opp_pitcher, venue):
    rows = []
    if not opp_pitcher:
        return rows
    pmult = PF.park_mult(venue) if use_park else 1.0
    for b in batters:
        if use_splits:
            b_rate, b_n = b.tb_per_pa_vs(opp_pitcher.throws)
            p_rate = (opp_pitcher.tb_per_bf_vs_l if b.bats in ("L", "S")
                      else opp_pitcher.tb_per_bf_vs_r) or opp_pitcher.tb_per_bf
        else:
            b_rate, b_n = b.tb_per_pa, b.pa
            p_rate = opp_pitcher.tb_per_bf
        # Fangraphs override for batter rate
        if b.name.lower() in fg_rates:
            b_rate = fg_rates[b.name.lower()]["tb_per_pa"]
            b_n = fg_rates[b.name.lower()]["pa"]
        if not b_rate or not p_rate:
            continue
        shares = b.hit_shares()
        ht = E.HitTypeShares(*shares) if shares else E.HitTypeShares()
        inp = E.ProjectionInput(
            batter_tb_per_pa=b_rate, batter_pa_sample=b_n,
            pitcher_tb_per_pa_allowed=p_rate, pitcher_bf_sample=max(opp_pitcher.bf, 1),
            line=default_line, side="Over",
            expected_pa=PF.expected_pa(b.order), park_mult=pmult,
            shares=ht, sp_share=sp_share, league=league_rate, reg_k=int(reg_k),
        )
        r = E.project(inp)
        p_cover = r.p_cover if method.startswith("Exact") else r.p_cover_poisson
        rows.append({
            "Batter": b.name, "Slot": b.order, "B": b.bats,
            "vs Pitcher": opp_pitcher.name, "P": opp_pitcher.throws,
            "Line": default_line,
            "Proj TB": round(r.lam, 2),
            "P(Over)": round(p_cover, 3),
            "Fair Over odds": round(E.prob_to_american(p_cover), 0),
            "_b_rate": round(r.batter_rate, 3), "_p_rate": round(r.pitcher_rate, 3),
        })
    return rows

all_rows = []
for mu in slate:
    all_rows += [{**r, "Game": f"{mu.away} @ {mu.home}", "Venue": mu.venue}
                 for r in project_side(mu.away_lineup, mu.home_pitcher, mu.venue)]
    all_rows += [{**r, "Game": f"{mu.away} @ {mu.home}", "Venue": mu.venue}
                 for r in project_side(mu.home_lineup, mu.away_pitcher, mu.venue)]

if not all_rows:
    st.warning("No projections yet — lineups likely not posted. Probable pitchers below.")
    for mu in slate:
        st.write(f"**{mu.away} @ {mu.home}** — "
                 f"{mu.away_pitcher.name if mu.away_pitcher else 'TBD'} vs "
                 f"{mu.home_pitcher.name if mu.home_pitcher else 'TBD'}")
    st.stop()

df = pd.DataFrame(all_rows)

# --------------------------------------------------------------------------- #
# Odds entry + edge                                                           #
# --------------------------------------------------------------------------- #
st.subheader("1 · Projections")
st.caption("Sorted by projected total bases. Paste your odds in the next section to get edges.")
view_cols = ["Game", "Batter", "Slot", "B", "vs Pitcher", "P", "Line",
             "Proj TB", "P(Over)", "Fair Over odds", "Venue"]
st.dataframe(df[view_cols].sort_values("Proj TB", ascending=False),
             use_container_width=True, hide_index=True)

st.subheader("2 · Paste your odds → edges")
st.caption("Enter the Over price (American) you're getting for each batter. Leave blank to skip. "
           "Best/value plays float to the top.")
odds_df = df[["Game", "Batter", "Line", "P(Over)"]].copy()
odds_df["Your Over odds"] = ""
edited = st.data_editor(odds_df, use_container_width=True, hide_index=True,
                        disabled=["Game", "Batter", "Line", "P(Over)"])

results = []
for _, row in edited.iterrows():
    raw = str(row["Your Over odds"]).strip()
    if not raw:
        continue
    try:
        odds = float(raw)
    except ValueError:
        continue
    p = float(row["P(Over)"])
    be = E.american_to_implied(odds)
    payout = E.american_to_decimal_profit(odds)
    ev = p * payout - (1 - p)
    edge = p - be
    results.append({
        "Game": row["Game"], "Batter": row["Batter"], "Line": row["Line"],
        "P(Over)": round(p, 3), "Your odds": odds, "Break-even": round(be, 3),
        "Edge": round(edge, 3), "Model EV": round(ev, 3),
        "Verdict": "VALUE" if edge >= min_edge else ("Lean" if edge >= 0 else "Pass"),
    })

if results:
    rdf = pd.DataFrame(results).sort_values("Edge", ascending=False)
    st.subheader("3 · Ranked edges")
    st.dataframe(rdf, use_container_width=True, hide_index=True)
    st.download_button("Download edges (CSV)", rdf.to_csv(index=False),
                       file_name=f"tb_edges_{date.isoformat()}.csv")

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
