# MLB Total Bases — Daily Slate Model

A website version of your TB props model. It pulls the day's confirmed lineups
and probable pitchers, runs your Log5 + regression projection on **every batter
vs the opposing starter**, and ranks edges against the odds you paste in. Runs
in the browser at a URL — nothing to download.

---

## What it does

1. **Pulls the slate** from the free MLB StatsAPI: games, probable pitchers,
   confirmed batting orders, batter/pitcher handedness.
2. **Pulls season stats + L/R splits** (batter TB/PA, pitcher TB/BF-allowed).
3. **Projects each batter** vs the opposing starter using your exact model.
4. **You paste odds** → it de-vigs and ranks VALUE / Lean / Pass by edge.

## The model (ported from your "Weighted AVG" sheet)

- **Regression:** `(raw·n + league·K) / (n + K)`, league = 0.355, K = 175. *(identical to your sheet)*
- **Log5:** `(b·p/l) / (b·p/l + (1−b)(1−p)/(1−l))`. *(identical)*
- **Projected TB (λ):** `matchup_rate · expected_PA · park_mult`.
- **Cover probability — two methods:**
  - **Exact distribution (default):** builds a per-PA out/1B/2B/3B/HR distribution
    and convolves it over the game. This is your Monte Carlo block done exactly
    (no sim noise) **and** fed by the fully-adjusted matchup rate — so it inherits
    pitcher strength (Log5), park, and L/R splits, which the spreadsheet MC lacked.
  - **Poisson:** your original `POISSON.DIST` method, kept for comparison.

> ⚠️ The two methods diverge a lot. On the same inputs the Poisson method tends to
> read ~10–20 points higher on Overs because total bases aren't Poisson-distributed
> (it lets bases "cluster" unrealistically). The exact distribution is the more
> faithful number. Check it against your calibration tracker.

## Advancements over the spreadsheet

| Lever | Spreadsheet | This app |
|---|---|---|
| Scope | one batter, hand-fed | whole slate, auto from lineups |
| Cover prob | Poisson (or unused MC) | exact per-PA distribution, fully adjusted |
| Handedness | not used | batter TB/PA vs LHP/RHP × pitcher vs LHB/RHB |
| Expected PA | fixed 4.7 | by lineup slot (4.65 leadoff → 3.85 nine-hole) |
| Bullpen | ignored | optional: blend league-avg bullpen for later PAs |
| Park | manual mult | per-venue multipliers (editable in `park_factors.py`) |
| Hit-type split | fixed 62.5/12/0/25 | each batter's own 1B/2B/3B/HR mix |
| Odds | one book | multi-book weighted no-vig calculator |

## Files

- `engine.py` — pure projection math (no network). Unit-tested against your sheet.
- `data.py` — MLB StatsAPI + Fangraphs CSV loader.
- `park_factors.py` — park multipliers + expected-PA-by-slot (tune these).
- `app.py` — the Streamlit UI.

---

## Run it locally (optional, to test)

```bash
pip install -r requirements.txt
streamlit run app.py
```

## Deploy as a website (free, no download) — Streamlit Community Cloud

1. **Make a GitHub repo.** Go to github.com → New repository → name it
   `mlb-tb-model` → Create. Upload these four `.py` files + `requirements.txt`
   (drag-and-drop via "Add file → Upload files", or use git).
2. **Go to share.streamlit.io**, sign in with GitHub.
3. Click **New app** → pick your repo, branch `main`, main file `app.py` → **Deploy**.
4. You get a URL like `https://mlb-tb-model.streamlit.app` — open it on any device.
   Every push to the repo redeploys automatically.

That's it. No server to manage, no download.

## Daily workflow

1. Open your URL a few hours before first pitch (lineups post ~3–4 hrs out).
2. Pick the date → **Load slate**.
3. Review projections, paste your Over prices, read the ranked edges.
4. (Optional) Drop in a Fangraphs batting CSV to override MLB-API rates.

## Roadmap / where to take it next

- **Weather:** fold wind/temp into the park multiplier (the `park_mult` knob is
  your old "park / weather / form" field — a weather API can set it per game).
- **Expected stats:** regress to Statcast xSLG instead of actual TB (stabilizes
  faster, less noise) via Baseball Savant.
- **Auto odds:** The Odds API player-props tier (~$30/mo) for FD/DK/MGM/Bovada.
- **Calibration feedback:** log every projection + result, refit a calibration
  curve (Platt scaling) so probabilities self-correct — your Calibration sheet,
  automated.

## v2 advancements (added)

- **Statcast expected TB (xSLG):** pulls Baseball Savant's expected-stats leaderboard
  and computes a luck factor (`est_slg / slg`) per player, then blends actual TB/PA
  toward the expected rate (weight adjustable). Applied to both batters and the
  starter. Over-performers get pulled down, unlucky hitters get pulled up.
- **Recent-form weighting:** pulls each batter's last-N-days TB/PA from the MLB API
  (`byDateRange`) and blends it with the season rate (window + weight adjustable).
- **Computed starter/bullpen split:** uses the starter's batters-faced-per-start
  (`BF ÷ GS`) to work out, per lineup slot, how many PAs come vs the starter vs the
  bullpen — shown as the **vsSP%** column. Later PAs use a bullpen TB/BF rate
  (default 0.345, editable). Replaces the old fixed slider with a per-batter value.

All are toggleable from the sidebar; turn any of them off to fall back to the
simpler model. No new dependencies — still just streamlit / pandas / requests.
