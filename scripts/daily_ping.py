"""
Daily slate ping — no Streamlit involved.

Projects today's Total Bases slate with the app's default settings
(splits, home/away, park, Statcast blend, recent form, auto bullpen split —
see DEFAULTS below) and posts the strongest model leans to a Discord webhook.

This does NOT check odds or compute edge/EV — the app has no automated odds
feed yet, so these are the model's highest-confidence leans (P(Over) or
P(Under) furthest from 50%), not verified +EV plays. Treat it as a shortlist
to price yourself, the same way you would eyeball the "Why the strong picks"
section in the app.

Run standalone:
    DISCORD_WEBHOOK_URL=... python scripts/daily_ping.py [--date YYYY-MM-DD]
"""
import argparse
import datetime as dt
import os
import sys

import requests

if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8")

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import data as D
import engine as E
import park_factors as PF

DEFAULT_LINE = 1.5
REG_TB = 175
RECENT_DAYS = 21
W_RECENT = 0.35
W_STATCAST = 0.5
BULLPEN_RATE = 0.345
LEAN_THRESHOLD = 0.56
TOP_N = 8


def project_batter(b, opp_pitcher, venue, savant_bat, savant_pit, batter_is_home, pitcher_is_home):
    total_pa = PF.expected_pa(b.order)
    this_share = (E.pa_vs_starter(b.order, opp_pitcher.bf_per_start, total_pa) / total_pa
                  if opp_pitcher.bf_per_start > 0 else 1.0)
    side = b.bats
    if side == "S":
        side = "L" if opp_pitcher.throws == "R" else "R"
    split_pa = b.pa_vs_l if opp_pitcher.throws == "L" else b.pa_vs_r

    pmult = PF.park_mult_hand(venue, side)
    park_ev = PF.park_event_hand(venue, side, 1.0)

    b_rate, b_n = b.tb_per_pa_vs(opp_pitcher.throws)
    if not b_rate:
        b_rate, b_n = b.tb_per_pa, b.pa
    p_rate = (opp_pitcher.tb_per_bf_vs_l if b.bats in ("L", "S") else opp_pitcher.tb_per_bf_vs_r) \
        or opp_pitcher.tb_per_bf

    if b.recent_pa > 0:
        recent_rate = b.recent_tb / b.recent_pa
        eff_w = W_RECENT * b.recent_pa / (b.recent_pa + 50.0)
        b_rate = eff_w * recent_rate + (1 - eff_w) * b_rate

    if b.mlbam_id in savant_bat:
        b_rate = E.blend(b_rate, b_rate * savant_bat[b.mlbam_id]["luck"], W_STATCAST)
    if opp_pitcher.mlbam_id in savant_pit:
        p_rate = E.blend(p_rate, p_rate * savant_pit[opp_pitcher.mlbam_id]["luck"], W_STATCAST)

    b_rate *= b.home_away_factor(batter_is_home)
    p_rate *= opp_pitcher.home_away_factor(pitcher_is_home)
    if not b_rate or not p_rate:
        return None

    shares = b.hit_shares()
    ht = E.HitTypeShares(*shares) if shares else E.HitTypeShares()

    ber = per = None
    raw_b = b.event_rates_vs(opp_pitcher.throws)
    raw_p = opp_pitcher.event_rates_allowed_vs(side)
    if raw_b and raw_p:
        ber = {ev: E.regress(raw_b[ev], b_n, E.LEAGUE_EVENT_RATES[ev], REG_TB) for ev in raw_b}
        per = {ev: E.regress(raw_p[ev], max(opp_pitcher.bf, 1), E.LEAGUE_EVENT_RATES[ev], REG_TB) for ev in raw_p}

    inp = E.ProjectionInput(
        batter_tb_per_pa=b_rate, batter_pa_sample=b_n,
        pitcher_tb_per_pa_allowed=p_rate, pitcher_bf_sample=max(opp_pitcher.bf, 1),
        line=DEFAULT_LINE, side="Over",
        expected_pa=total_pa, park_mult=pmult,
        shares=ht, sp_share=this_share, bullpen_rate=BULLPEN_RATE,
        reg_k=REG_TB, batter_event_rates=ber, pitcher_event_rates=per,
        park_event_mult=park_ev,
    )
    r = E.project(inp)
    conf = E.confidence_score(b_n, opp_pitcher.bf, split_pa, True)
    return {
        "batter": b.name, "slot": b.order, "pitcher": opp_pitcher.name,
        "p_over": r.p_cover, "conf": conf,
        "expected": bool(getattr(b, "expected", False)),
    }


def build_picks(date_str, season):
    geo = dict(PF.PARK_GEO)
    for alias, real in PF.PARK_ALIASES.items():
        if real in PF.PARK_GEO:
            geo[alias] = PF.PARK_GEO[real]
    slate = D.build_slate(date_str, season, recent_days=RECENT_DAYS, want_weather=False, park_geo=geo)

    lr = D.league_event_rates(season)
    if lr:
        E.LEAGUE_EVENT_RATES.update({k: lr[k] for k in ("1B", "2B", "3B", "HR") if k in lr})
        if "TB" in lr:
            E.LEAGUE_TB_PER_PA = lr["TB"]

    savant_bat, savant_pit = D.load_savant_expected(season, "batter"), D.load_savant_expected(season, "pitcher")

    rows = []
    for mu in slate:
        for b in mu.away_lineup:
            row = project_batter(b, mu.home_pitcher, mu.venue, savant_bat, savant_pit, False, True)
            if row:
                rows.append({**row, "game": f"{mu.away} @ {mu.home}"})
        for b in mu.home_lineup:
            row = project_batter(b, mu.away_pitcher, mu.venue, savant_bat, savant_pit, True, False)
            if row:
                rows.append({**row, "game": f"{mu.away} @ {mu.home}"})

    picks = []
    for r in rows:
        p = r["p_over"]
        side = "Over" if p >= 0.5 else "Under"
        p_side = p if side == "Over" else 1 - p
        if p_side >= LEAN_THRESHOLD:
            picks.append({**r, "side": side, "p_side": p_side})
    picks.sort(key=lambda x: x["p_side"], reverse=True)
    return picks[:TOP_N], len(slate)


def format_message(date_str, picks, n_games):
    if not n_games:
        return f"**{date_str}** — no MLB games today."
    if not picks:
        return f"**{date_str}** — {n_games} games loaded, but no lean cleared {LEAN_THRESHOLD:.0%} yet " \
               f"(lineups may not be posted). Try again closer to first pitch."
    lines = [f"**MLB TB Model — {date_str}** ({n_games} games)\n"]
    any_expected = False
    for p in picks:
        stars = "*" * p["conf"]
        flag = ""
        if p["expected"]:
            flag = " (proj. lineup)"
            any_expected = True
        lines.append(
            f"`{p['p_side']*100:4.1f}%` **{p['batter']}** {p['side']} {DEFAULT_LINE} TB "
            f"— vs {p['pitcher']} ({p['game']}){flag} [{stars}]"
        )
    if any_expected:
        lines.append("\n_(proj. lineup) = today's official batting order isn't posted yet for that game — "
                      "this used the team's last lineup as a placeholder. Re-check once it's confirmed, "
                      "usually 3-4 hrs before first pitch._")
    game_counts = {}
    for p in picks:
        game_counts[p["game"]] = game_counts.get(p["game"], 0) + 1
    top_game, top_n = max(game_counts.items(), key=lambda kv: kv[1])
    if top_n >= 3 or (top_n >= 2 and top_n / len(picks) > 0.5):
        lines.append(f"\n**Correlated:** {top_n} of these {len(picks)} picks are from the same game ({top_game}) "
                      "— one game script can sweep or wash several of them together.")
    lines.append("_Model leans only — no odds feed yet, so this isn't verified +EV. Price it yourself._")
    return "\n".join(lines)


def send_discord(webhook_url, content):
    resp = requests.post(webhook_url, json={"content": content}, timeout=30)
    resp.raise_for_status()


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--date", default=dt.date.today().isoformat())
    parser.add_argument("--season", type=int, default=dt.date.today().year)
    parser.add_argument("--dry-run", action="store_true", help="Print instead of posting to Discord")
    args = parser.parse_args()

    picks, n_games = build_picks(args.date, args.season)
    message = format_message(args.date, picks, n_games)

    if args.dry_run:
        print(message)
        return

    webhook_url = os.environ.get("DISCORD_WEBHOOK_URL")
    if not webhook_url:
        print("DISCORD_WEBHOOK_URL not set — printing instead:\n")
        print(message)
        return
    send_discord(webhook_url, message)
    print("Posted to Discord.")


if __name__ == "__main__":
    main()
