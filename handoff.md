# Handoff — MLB TB Prop Model

## Goal

A Streamlit app (`app.py`) that projects MLB Total Bases (and H+R+RBI, pitcher
Ks) props for every batter on the day's slate, ranks edges against pasted
odds, and tracks results over time. Built originally in Cowork; this session
picked it up to (1) get it under proper git/GitHub version control, (2) polish
the UI, (3) set up a local dev environment so changes can actually be tested
before shipping, and (4) add a handful of "make it more advanced" features the
user picked from a brainstormed list.

## Current state — everything below is committed and pushed

Repo: `https://github.com/lukeoliver384/mlb-tb-model`, branch `main`.
Working tree is clean; local `main` == `origin/main` at commit `b4e83a1`.

Streamlit Cloud is the production deployment — **it needs a manual "Reboot
app"** (share.streamlit.io → Manage app → Reboot) to pick up commits, it does
not auto-deploy. Last confirmed reboot picked up through the font/emoji/
sidebar commit (`0ecc01a`). The SQLite backend, Calibration tab, and
correlation-flag commits (`efd52fb`, `f4793c6`, `4022b0f`, `b4e83a1`) have
**not been confirmed live on Cloud yet** — that's the first thing to check
next session.

### What's built and working (verified against live data, not just compiled)

- **Git/GitHub**: local repo merged with the pre-existing GitHub history
  (~118 "Add files via upload" commits from Cowork) via
  `--allow-unrelated-histories -X ours`, favoring local files where they
  differed. Nothing on GitHub was force-pushed or lost.
- **Local dev environment**: Python 3.12.10 (installed via winget — the
  machine had no real Python before, only a Windows Store stub), all
  `requirements.txt` deps installed, GitHub CLI (`gh`) installed and
  authenticated as `lukeoliver384`.
- **UI polish**: Inter font (Google Fonts, loaded via `@import` in the CSS
  block + `.streamlit/config.toml` theme.font), all decorative emoji removed
  (kept the ★ star confidence rating — that's functional, not decorative),
  sidebar expanders got Material Symbol icons + a "SETTINGS" section header +
  dividers inside the dense "Model & matchup" panel, colored Over/Under badges
  on the Projections table, dynamic header pill ("SLATE LOADED" /
  "NO SLATE LOADED"), "last loaded" timestamp next to the Load slate button.
- **Daily automation**: `scripts/daily_ping.py` — standalone (no Streamlit
  dependency, reuses `data.py`/`engine.py`/`park_factors.py` directly) —
  projects the day's TB slate with the app's default settings and posts the
  top leans (≥56% confidence) to a Discord webhook. Runs via
  `.github/workflows/daily-ping.yml` at 9:30 AM / 12 PM / 4 PM ET daily, plus
  manual dispatch. `DISCORD_WEBHOOK_URL` repo secret is already set and
  verified working end-to-end (real Discord messages received). Flags picks
  built from unconfirmed/projected lineups, and flags when 3+ picks (or a
  majority) share one game (correlation risk).
- **SQLite backend** (`tracker.py`): replaced the CSV local-fallback tier
  with SQLite (stdlib `sqlite3` + pandas `to_sql`/`read_sql`, zero new
  dependencies). Google Sheets remains the untouched, unchanged persistent
  cloud tier — confirmed via diff that no Sheets read/write code was modified.
  Auto-migrates legacy `tracker_*.csv` into `tracker.db` on first read.
  Verified with isolated roundtrip tests (log/bets/odds/settings) and against
  the live app (`tracker.db` confirmed created on disk with real content).
- **Same-game correlation flag**: in both the app's "Ranked edges" table and
  the Discord ping. Confirmed firing against real data (5 of 8 picks were all
  one game on 2026-07-02).
- **Platt scaling calibration** (`tracker.py`: `fit_platt_scaling`,
  `apply_platt`, `calibration_by_probability_platt`): 2-parameter logistic
  calibration (scale + directional bias) fit via hand-rolled Newton-Raphson,
  no new dependency. Verified against synthetic data with known ground-truth
  A/B (recovered both, improved log-loss). **Explicitly NOT wired into live
  projections** — the user asked for it to be a separate, read-only preview.
  It lives in its own new "Calibration" tab in `app.py`, showing fitted A/B,
  log-loss vs. raw, and a reliability-by-bucket table. Needs 30+ graded legs
  to activate; currently 0 graded legs exist anywhere (local or, as far as
  confirmed, Cloud), so this tab currently just shows the "not enough data"
  state — untested with real non-trivial numbers yet.

### Deliberately not built (user deferred)

- **Odds API integration** (for automated odds + real +EV verification):
  researched pricing, found conflicting info between The Odds API's own site
  (suggests all tiers get all markets) and a third-party blog (says player
  props are gated to their $99/mo tier) — never resolved which is accurate.
  User said not ready for this yet.
- **More props** (HR, hits, SB, pitcher outs alone) and a **historical
  backtest script** — both came up as brainstormed options; user didn't pick
  either in the batch they selected.

## Files actively touched this session

- `app.py` — the big one, many edits across the session (see commit log)
- `tracker.py` — SQLite backend + Platt scaling functions
- `scripts/daily_ping.py` — new
- `.github/workflows/daily-ping.yml` — new
- `.streamlit/config.toml` — font change only
- `.gitignore` — grew several entries (`tracker.db`, `tracker_*.csv`,
  `_pf_tmp.json`, `.claude/settings.local.json`)
- `.claude/launch.json` — new, config for the preview tool to run
  `streamlit run app.py` locally
- Not touched: `engine.py`, `data.py`, `park_factors.py`, `README.md` (except
  the initial merge-commit conflict resolution, which kept the local version)

## Things tried that didn't work / had to be corrected

1. **Redundant spinner**: first fix for "no loading feedback on Load slate"
   wrapped the click handler in `st.spinner(...)`, but `load()` already had
   `@st.cache_data(show_spinner="Pulling lineups & stats...")` — caused two
   stacked spinner messages on screen. Reverted; kept only the "last loaded"
   timestamp caption as the actual improvement.
2. **`st.rerun()` broke rebuild logic**: making the header badge update
   immediately after a slate load required an `st.rerun()`, but that resets
   the `go` (button-clicked) flag to `False` on replay, and two other code
   paths (projections table rebuild, odds editor rebuild) specifically check
   `if go:` to decide whether to refresh on a same-date reload. Fixed by
   stashing a one-shot `_just_loaded` session-state flag through the rerun
   and folding it back into `go` afterward.
3. **Full SQLite swap was almost a mistake**: initial instinct was to fully
   replace Google Sheets with SQLite everywhere. Caught before implementing —
   Streamlit Cloud's filesystem is ephemeral and wiped on every reboot, which
   happens often (every redeploy). A full swap would have silently deleted
   the user's real bet/grading history on the next Cloud reboot. Kept Sheets
   as the cloud-persistent tier; SQLite only replaces the local CSV fallback.
4. **Platt scaling wired into live projections, then un-wired**: first
   version added a sidebar checkbox that, when on, actually changed the
   P(Over) shown everywhere (Projections table, Ranked edges, etc.). User
   explicitly said not to do that — reverted the sidebar checkbox and the
   `_calibrate()` branching entirely back to pre-Platt behavior, then rebuilt
   the feature as a separate, non-mutating "Calibration" tab instead.
5. **`%-I` strftime**: used the Linux/macOS-only "no leading zero" flag for a
   timestamp format; caught before running (Windows' C runtime doesn't
   support it) and replaced with `.strftime("%I:%M %p").lstrip("0")`.
6. **`preview_screenshot` tool flakiness**: timed out repeatedly throughout
   the session for no app-related reason (confirmed via server logs showing
   no errors and DOM snapshots showing correct content each time). Not a code
   bug — just had to lean on `preview_snapshot`/`preview_eval`/server logs
   instead of screenshots for verification.

## Known open issues (not caused by this session, not yet fixed)

- A pre-existing pyarrow serialization warning ("Could not convert '�' with
  type str... Conversion failed for column Value") appears in server logs
  from early in the session, in some dataframe with a "Value" column
  (probably the multi-book no-vig calculator or a metrics table). Streamlit
  auto-recovers from it, so it's cosmetic-in-the-logs rather than broken, but
  never tracked down which exact table/column triggers it.

## Next step

1. **Confirm the Streamlit Cloud app is rebooted** onto `b4e83a1` and sanity
   check the new Calibration tab and SQLite-backed settings actually work
   there (Cloud's Google Sheets path should be unaffected, but worth eyeballing
   once — nothing in this session was tested directly against a live Sheets
   backend, only against the no-Sheets/SQLite-fallback local path).
2. Otherwise, pick up wherever the user wants: Odds API tier question, more
   props, backtest script, or just let graded data accumulate so the
   Calibration tab has something real to show.
