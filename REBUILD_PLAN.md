# Rebuild Plan — MLB Prop Model → Professional Web App

Goal: a professional, monetizable website UI that **preserves every existing
component**, backed by a fast API that serves precomputed projections. Nothing
in the current Streamlit app is dropped — this doc is the checklist that
guarantees it.

---

## 1. Component inventory (must all survive the rebuild)

Source of truth: `app.py` (1,729 lines, six tabs) as of this plan.

### Global / sidebar
- **Prop selector**: Total Bases · Hits+Runs+RBIs · Pitcher Strikeouts
- **Slate loader**: date picker + "Load slate" + last-loaded timestamp + header
  status pill (SLATE LOADED / NO SLATE LOADED)
- **Settings expanders** (all persisted):
  - Lines & staking (line, tossup band, min edge, Kelly, max stake, shrink, conf-stake)
  - Model & matchup (regression K, splits, per-prop temperature)
  - Park & weather
  - Bullpen split
  - Statcast & recent form (windows + weights)

### Tab 1 — Projections & Odds
- Projections table (every hitter vs opposing starter; B/P hand, vsSP%, proj,
  P(Over), Lean, Lean%, Fair odds, Conf ★, Venue, Wx)
- Strikeout projection detail (diagnostic)
- **Add your odds** editor  → becomes AUTO-FILLED by the scraper
- Import odds (paste / CSV)
- "Why the strong picks" (top leans + reasoning)
- Top 5 value plays
- Ranked edges (+ same-game correlation flag)
- Download edges CSV
- **Multi-book no-vig fair line calculator** (Pinnacle/FD/DK/MGM/Caesars/Bovada)
  → becomes the live multi-book compare
- Log selected bets

### Tab 2 — Performance
- Accuracy tracker (MAE, bias, directional accuracy)
- Log projections / Grade results / Diagnose grading
- Backfill past dates (recompute + log + grade all props)
- Confidence vs actual hit rate
- Calibration by model probability P(Over)
- Recent graded results

### Tab 3 — Calibration
- Fitted Platt (A/B) preview, log-loss vs raw, reliability by bucket

### Tab 4 — Paper Bankroll
- Combined bankroll (all props), refresh from sheet

### Tab 5 — Closing Lines (CLV)
- Save closing odds, CLV computation

### Tab 6 — Game Day
- Per-game cards, records/form, ESPN headlines, per-prop sub-tabs

### Data / model layer (unchanged — reused as-is behind the API)
- `engine.py` (projection math), `data.py` (MLB StatsAPI + Savant + weather),
  `park_factors.py`, `tracker.py` (Sheets + SQLite + Platt), `daily_ping.py`,
  `backtest.py` (new).

---

## 2. Target architecture

```
                    ┌─────────────────────────────┐
                    │  Next.js + React + Tailwind  │   ← professional UI
                    │  shadcn/ui, TanStack Table,  │     (Vercel)
                    │  Recharts                    │
                    └──────────────┬──────────────┘
                                   │  JSON over HTTPS
                    ┌──────────────▼──────────────┐
                    │        FastAPI backend        │   ← serves precomputed
                    │  wraps engine/data/tracker/   │     slate + odds + tracker
                    │  park_factors + odds_scrape   │     (Railway/Render)
                    └──────────────┬──────────────┘
                                   │
        ┌──────────────┬──────────┴───────┬───────────────┐
   MLB StatsAPI    Savant/weather   Odds scraper      Storage
                                    (Bovada+books)   (Postgres/Sheets)
```

Why this shape:
- **Speed**: a scheduled job precomputes the slate + odds and caches it; the
  UI just reads. Page load goes from ~20–30s to instant.
- **Reuse**: the API imports your existing modules verbatim — zero model risk.
- **Monetizable**: React frontend = real design + paywall (Stripe) + accounts.

### Recommended stack
- Frontend: **Next.js (App Router) + TypeScript + Tailwind + shadcn/ui**,
  **TanStack Table** for the projection board (sortable, pinned, color-scaled —
  the pro version of st-aggrid), **Recharts** for calibration/bankroll charts.
- Backend: **FastAPI + Uvicorn**, Pydantic response models.
- Storage: keep Google Sheets/SQLite short-term; migrate to **Postgres**
  (Railway) when accounts land.
- Auth/payments (phase 4): Clerk or Auth.js + **Stripe**.

---

## 3. Phased build

**Phase A — Odds scraper** (`odds_scrape.py`)  ← in progress
Bovada primary (your book), + DK/FD/MGM/Pinnacle adapters. Multi-book compare,
best price, de-vigged consensus. Wired into the existing odds store first so it
works in the current app immediately, then exposed via the API.

**Phase B — FastAPI backend**
Endpoints mirroring the tabs: `/slate`, `/odds`, `/edges`, `/performance`,
`/calibration`, `/bankroll`, `/clv`, `/gameday`, plus actions `/log`, `/grade`,
`/backfill`. Precompute cache + a scheduled refresh.

**Phase C — Next.js frontend**
Rebuild tab by tab against the inventory above. Ship behind the same data, so
you can diff old vs new for parity before cutting over.

**Phase D — Monetize**
Accounts, Stripe paywall, tiered access (e.g. free = model leans, paid =
edges + multi-book + CLV). Move persistence to Postgres.

**Phase E — Automation**
GitHub Actions / backend cron: daily fetch → project → scrape odds → grade
yesterday → refit Platt → cache. Discord ping becomes real +EV.

---

## 4. Parity rule

No tab is "done" in the rebuild until every bullet under it in §1 is present and
behaves the same or better. Check them off against this doc.
