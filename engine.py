"""
Total Bases projection engine.

Ported from the 'Weighted AVG' sheet of the BettingPros workbook, with several
model advancements layered on top (see README). Pure-python, no network, fully
unit-testable.

Core idea (unchanged from the spreadsheet):
  1. Regress batter TB/PA and pitcher TB/PA-allowed toward league mean.
  2. Combine them with a Log5 (odds-ratio) formula into a matchup TB/PA rate.
  3. Turn that rate into a *distribution* of total bases over the game, then
     read off P(cover) for the prop line and compare to the no-vig market.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from math import exp, factorial, lgamma
from typing import Iterable

LEAGUE_TB_PER_PA = 0.355   # sheet E13

# Approximate league per-PA event rates (tune from a league pull each season).
LEAGUE_EVENT_RATES = {"1B": 0.137, "2B": 0.044, "3B": 0.004, "HR": 0.030}

# Approximate league per-PA rates for Hits / Runs / RBIs, and runs allowed per BF.
LEAGUE_HRR = {"H": 0.235, "R": 0.115, "RBI": 0.110}
LEAGUE_R_PER_BF = 0.118
LEAGUE_K_PA = 0.225   # league strikeouts per PA (updated live)
HRR_DISPERSION = 1.5   # >1 = overdispersed (more 0-1 games than Poisson); tune via calibration
K_DISPERSION = 1.4    # pitcher Ks: workload (IP/BF) variance widens tails vs Poisson; tune via calibration
REG_K_PA = 175             # sheet E12 (regression constant, in PA)


# --------------------------------------------------------------------------- #
# Odds helpers (American)                                                      #
# --------------------------------------------------------------------------- #
def american_to_decimal_profit(odds: float) -> float:
    """Profit per 1u stake (sheet's IF(G>0,G/100,100/ABS(G)))."""
    return odds / 100 if odds > 0 else 100 / abs(odds)


def kelly_fraction(p: float, odds: float) -> float:
    """Full-Kelly fraction of bankroll for win prob p at American odds. 0 if -EV."""
    b = american_to_decimal_profit(odds)
    if b <= 0:
        return 0.0
    f = (p * (b + 1) - 1) / b
    return max(0.0, f)


def american_to_implied(odds: float) -> float:
    """Implied (vig-inclusive) probability of a single American price."""
    return 100 / (odds + 100) if odds > 0 else abs(odds) / (abs(odds) + 100)


def prob_to_american(p: float) -> float:
    """Fair probability -> fair American odds (sheet F2 logic)."""
    if p <= 0 or p >= 1:
        return float("nan")
    return -p / (1 - p) * 100 if p >= 0.5 else (1 - p) / p * 100


def arsenal_factor(batter_xwoba_by_pitch: dict, pitcher_usage_by_pitch: dict,
                   batter_overall_xwoba: float) -> float:
    """
    Pitch-type matchup multiplier. Weights the hitter's xwOBA vs each pitch type by
    how often the starter throws it, compares to the hitter's overall xwOBA, and
    returns a small (regressed, clamped) rate multiplier. >1 = the pitcher throws
    stuff this hitter handles well; <1 = a bad matchup of pitch types.
    """
    if not batter_xwoba_by_pitch or not pitcher_usage_by_pitch or batter_overall_xwoba <= 0:
        return 1.0
    num = w = 0.0
    for pt, usage in pitcher_usage_by_pitch.items():
        xw = batter_xwoba_by_pitch.get(pt)
        if xw and usage:
            num += usage * xw
            w += usage
    if w <= 0:
        return 1.0
    weighted = num / w
    raw = weighted / batter_overall_xwoba
    return max(0.85, min(1.15, 1 + (raw - 1) * 0.5))   # half-weight, ±15% cap


def confidence_score(batter_pa: float, pitcher_bf: float, split_pa: float,
                     use_splits: bool) -> int:
    """
    1-5 data-sufficiency score (NOT an edge/win score). Based on sample
    reliability via stabilization-style shrinkage: reliability = n/(n+k), with
    k near where rate stats stabilize (~350 PA for hitters, ~300 BF for pitchers).
    A thin or missing platoon split discounts it. Maps reliability to 1-5 stars.
    """
    rb = batter_pa / (batter_pa + 350.0) if batter_pa else 0.0
    rp = pitcher_bf / (pitcher_bf + 300.0) if pitcher_bf else 0.0
    rel = 0.65 * rb + 0.35 * rp                      # ~0 (no data) to ~0.6 (full season)
    if use_splits and split_pa < 50:                 # platoon split thin / fell back
        rel *= 0.85
    stars = 1 + (rel - 0.15) / 0.47 * 4              # rel .15->1 star, ~.62->5 stars
    return int(max(1, min(5, round(stars))))


def no_vig_two_way(over_odds: float, under_odds: float) -> tuple[float, float]:
    """De-vig a two-way market into fair (over, under) probabilities."""
    io, iu = american_to_implied(over_odds), american_to_implied(under_odds)
    total = io + iu
    return io / total, iu / total


@dataclass
class BookLine:
    book: str
    over_odds: float
    under_odds: float
    weight: float = 1.0


def weighted_no_vig(lines: Iterable[BookLine]) -> dict:
    """
    Multi-book fair line, matching the 'Weighted AVG' sheet.
    Returns weighted fair over/under probs and the fair American over line.
    """
    lines = [l for l in lines if l.over_odds and l.under_odds]
    if not lines:
        return {"fair_over": None, "fair_under": None, "fair_over_american": None}
    wsum = sum(l.weight for l in lines) or 1.0
    fair_over = 0.0
    for l in lines:
        o, _ = no_vig_two_way(l.over_odds, l.under_odds)
        fair_over += (l.weight / wsum) * o
    return {
        "fair_over": fair_over,
        "fair_under": 1 - fair_over,
        "fair_over_american": prob_to_american(fair_over),
    }


# --------------------------------------------------------------------------- #
# Rates: regression + Log5                                                     #
# --------------------------------------------------------------------------- #
def regress(raw_rate: float, sample_pa: float,
            league: float = LEAGUE_TB_PER_PA, k: float = REG_K_PA) -> float:
    """Sheet E18/G18: (raw*n + league*K)/(n+K)."""
    return (raw_rate * sample_pa + league * k) / (sample_pa + k)


def swstr_implied_k(whiff, league_whiff, league_k=LEAGUE_K_PA, elasticity=1.25):
    """SwStr/whiff-implied K rate, anchored multiplicatively to league so it self-calibrates:
    a pitcher whiffing X% above league projects league_k * (X) ** elasticity. Capped to a
    sane band. Returns None if inputs missing."""
    if not whiff or not league_whiff or league_whiff <= 0:
        return None
    implied = league_k * (whiff / league_whiff) ** elasticity
    return max(0.08, min(0.45, implied))


def log5_rate(batter: float, pitcher: float, league: float = LEAGUE_TB_PER_PA) -> float:
    """
    Sheet J12 odds-ratio Log5:
        (b*p/l) / (b*p/l + (1-b)(1-p)/(1-l))
    Returns the matchup TB/PA rate.
    """
    if league <= 0 or league >= 1:
        return batter  # degenerate league baseline: fall back to batter rate
    num = batter * pitcher / league
    denom = num + (1 - batter) * (1 - pitcher) / (1 - league)
    return num / denom if denom else batter


def blend(actual: float, expected: "float | None", w_expected: float) -> float:
    """
    Blend an actual rate with a Statcast *expected* rate.
    w_expected in [0,1]: 0 = pure actual, 1 = pure expected.
    If expected is missing, returns actual unchanged.
    """
    if expected is None or expected <= 0:
        return actual
    return (1 - w_expected) * actual + w_expected * expected


def pa_vs_starter(slot: int, sp_bf_per_start: float, total_pa: float) -> float:
    """
    Expected number of this slot's PAs that come against the STARTER.
    The starter faces roughly the first `sp_bf_per_start` hitters of the lineup;
    slot s bats at team-PA indices s, s+9, s+18, ...  Count how many land within
    the starter's workload, capped by the slot's total expected PAs.
    """
    if sp_bf_per_start <= 0 or slot < 1:
        return 0.0
    full = int(sp_bf_per_start // 9)          # full times through the order
    rem = sp_bf_per_start - full * 9          # batters into the next turn (0-9)
    if slot <= int(rem):
        extra = 1.0
    elif slot == int(rem) + 1:
        extra = rem - int(rem)                # fractional last batter
    else:
        extra = 0.0
    n = full + extra
    return max(0.0, min(n, total_pa))


# --------------------------------------------------------------------------- #
# Total-bases distribution                                                     #
# --------------------------------------------------------------------------- #
@dataclass
class HitTypeShares:
    """Share of a player's HITS that are 1B/2B/3B/HR (sheet M17:M20)."""
    single: float = 0.625
    double: float = 0.12
    triple: float = 0.0
    homer: float = 0.25

    def bases_per_hit(self) -> float:
        return self.single * 1 + self.double * 2 + self.triple * 3 + self.homer * 4

    def normalized(self) -> "HitTypeShares":
        s = self.single + self.double + self.triple + self.homer
        if s == 0:
            return HitTypeShares()
        return HitTypeShares(self.single / s, self.double / s,
                             self.triple / s, self.homer / s)


def single_pa_pmf(tb_per_pa: float, shares: HitTypeShares) -> list[float]:
    """
    Distribution of total bases from ONE plate appearance: [P(0),P(1),P(2),P(3),P(4)].
    hit_prob = tb_per_pa / bases_per_hit  (sheet M14 = M12/M13).
    """
    sh = shares.normalized()
    bph = sh.bases_per_hit()
    hit_prob = min(max(tb_per_pa / bph, 0.0), 1.0) if bph > 0 else 0.0
    return [
        1 - hit_prob,                 # out  -> 0 bases
        hit_prob * sh.single,         # 1B
        hit_prob * sh.double,         # 2B
        hit_prob * sh.triple,         # 3B
        hit_prob * sh.homer,          # HR
    ]


def component_log5_pmf(b_rates: dict, p_rates: dict,
                       league_rates: dict = None,
                       park_event_mult: dict = None,
                       park_mult: float = 1.0) -> list[float]:
    """
    Per-PA total-bases distribution built from PER-EVENT log5.

    Instead of one aggregate TB/PA rate, log5 each hit type separately
    (1B/2B/3B/HR) from the batter's and pitcher's per-PA event rates, so platoon
    and park effects act on each event correctly. Returns [P0, P1B, P2B, P3B, PHR].

    b_rates / p_rates: dict event -> per-PA (or per-BF) probability.
    park_event_mult: optional per-event park multipliers; else `park_mult` applied to all.
    """
    league_rates = league_rates or LEAGUE_EVENT_RATES
    probs = {}
    for e in ("1B", "2B", "3B", "HR"):
        be, pe, le = b_rates.get(e, 0.0), p_rates.get(e, 0.0), league_rates[e]
        if be <= 0 or pe <= 0 or le <= 0:
            p = be if be > 0 else le      # graceful fallback
        else:
            p = log5_rate(be, pe, le)
        pm = (park_event_mult or {}).get(e, park_mult)
        probs[e] = max(0.0, p * pm)
    total_hit = sum(probs.values())
    if total_hit > 0.95:                  # guard against impossible PAs
        scale = 0.95 / total_hit
        for e in probs:
            probs[e] *= scale
        total_hit = 0.95
    return [1 - total_hit, probs["1B"], probs["2B"], probs["3B"], probs["HR"]]


def _convolve(a: list[float], b: list[float]) -> list[float]:
    out = [0.0] * (len(a) + len(b) - 1)
    for i, ai in enumerate(a):
        if ai == 0:
            continue
        for j, bj in enumerate(b):
            out[i + j] += ai * bj
    return out


def tb_distribution_integer_pa(pa: int, pmf: list[float]) -> list[float]:
    """Exact TB distribution over an integer number of identical PAs (convolution)."""
    dist = [1.0]
    for _ in range(pa):
        dist = _convolve(dist, pmf)
    return dist


def tb_distribution(expected_pa: float, pmf_vs_sp: list[float],
                    pmf_vs_pen: "list[float] | None" = None,
                    sp_share: float = 1.0) -> list[float]:
    """
    TB distribution over a fractional number of PAs.

    Advancement vs. the sheet:
      * Fractional PA handled by blending floor/ceil distributions.
      * Optional starter-vs-bullpen weighting: each PA is vs the SP with prob
        `sp_share`, else vs a league-average bullpen pmf.
    """
    if pmf_vs_pen is None or sp_share >= 1.0:
        per_pa = pmf_vs_sp
    else:
        per_pa = [sp_share * s + (1 - sp_share) * p
                  for s, p in zip(pmf_vs_sp, pmf_vs_pen)]
    lo = int(expected_pa)
    frac = expected_pa - lo
    dist_lo = tb_distribution_integer_pa(lo, per_pa)
    if frac <= 1e-9:
        return dist_lo
    dist_hi = tb_distribution_integer_pa(lo + 1, per_pa)
    n = max(len(dist_lo), len(dist_hi))
    dist_lo += [0.0] * (n - len(dist_lo))
    dist_hi += [0.0] * (n - len(dist_hi))
    return [(1 - frac) * a + frac * b for a, b in zip(dist_lo, dist_hi)]


def p_cover_from_dist(dist: list[float], line: float, side: str) -> float:
    """P(cover) for an O/U prop. Lines are half-integers (1.5, 2.5...)."""
    threshold = line  # e.g. 1.5
    p_over = sum(p for tb, p in enumerate(dist) if tb > threshold)
    return p_over if side.lower() == "over" else 1 - p_over


def p_cover_negbin(lam: float, line: float, side: str, dispersion: float = 1.5) -> float:
    """Cover prob for a count with overdispersion (var = dispersion*mean). Negative
    binomial; falls back to Poisson if dispersion<=1. Used for H+R+RBI, where H/R/RBI
    correlate and real games lump at 0-1 more than Poisson allows."""
    if dispersion <= 1.0 or lam <= 0:
        return p_cover_poisson(lam, line, side)
    mu = lam
    r = mu / (dispersion - 1.0)            # size param so var = dispersion*mu
    p = r / (r + mu)
    k_max = int(line)
    cdf = 0.0
    for k in range(k_max + 1):
        pmf = exp(lgamma(k + r) - lgamma(r) - lgamma(k + 1)) * (p ** r) * ((1 - p) ** k)
        cdf += pmf
    cdf = min(max(cdf, 0.0), 1.0)
    return (1 - cdf) if side.lower() == "over" else cdf


def p_cover_poisson(lam: float, line: float, side: str) -> float:
    """The sheet's original Poisson method (J14), kept for comparison."""
    k = int(line)
    cdf = sum(exp(-lam) * lam**i / factorial(i) for i in range(k + 1))
    return (1 - cdf) if side.lower() == "over" else cdf


# --------------------------------------------------------------------------- #
# Hits + Runs + RBIs projection                                               #
# --------------------------------------------------------------------------- #
def project_hrr(h_pa, h_pa_n, p_h_per_bf, p_bf, r_pa, rbi_pa, p_r_per_bf,
                line, side="Over", expected_pa=4.3,
                park_hits=1.0, park_runs=1.0, reg_k=REG_K_PA, sp_share=1.0,
                r_ctx=1.0, rbi_ctx=1.0):
    """
    Hits + Runs + RBIs as a combined per-game count.

      * Hits: full batter-vs-pitcher log5 (clean matchup), like total bases.
      * Runs & RBIs: the batter's own rates, regressed, then scaled by the
        pitcher's run-suppression (runs allowed/BF vs league) and the park/weather
        run environment. This is the lineup-context approximation — R/RBI also
        depend on the hitters around him, which a batter-vs-pitcher model can't see.

    Returns (lam, p_cover). Cover prob uses Poisson on the combined mean (a count
    approximation; H+R+RBI is mildly overdispersed, so deep overs are slightly
    understated — tune against the tracker).
    """
    h = regress(h_pa, h_pa_n, LEAGUE_HRR["H"], reg_k)
    ph = regress(p_h_per_bf, p_bf, LEAGUE_HRR["H"], reg_k)
    hits_adj = log5_rate(h, ph, LEAGUE_HRR["H"]) * park_hits

    run_supp = (p_r_per_bf if p_r_per_bf else LEAGUE_R_PER_BF) / LEAGUE_R_PER_BF
    run_supp = max(0.6, min(1.5, run_supp))
    r = regress(r_pa, h_pa_n, LEAGUE_HRR["R"], reg_k)
    rbi = regress(rbi_pa, h_pa_n, LEAGUE_HRR["RBI"], reg_k)
    runs_adj = r * run_supp * park_runs * r_ctx
    rbi_adj = rbi * run_supp * park_runs * rbi_ctx

    starter_hrr = hits_adj + runs_adj + rbi_adj
    # Bullpen share: hitter vs average arms -> his own regressed rates, neutral run env.
    bullpen_hrr = h * park_hits + r * park_runs * r_ctx + rbi * park_runs * rbi_ctx
    hrr_pa = sp_share * starter_hrr + (1 - sp_share) * bullpen_hrr
    lam = hrr_pa * expected_pa
    return lam, p_cover_negbin(lam, line, side, HRR_DISPERSION)


# --------------------------------------------------------------------------- #
# Full projection                                                              #
# --------------------------------------------------------------------------- #
@dataclass
class ProjectionInput:
    batter_tb_per_pa: float
    batter_pa_sample: float
    pitcher_tb_per_pa_allowed: float
    pitcher_bf_sample: float
    line: float = 1.5
    side: str = "Over"
    odds: "float | None" = None           # your price (American)
    fair_prob: "float | None" = None      # no-vig market prob for this side
    expected_pa: float = 4.3
    park_mult: float = 1.0
    shares: HitTypeShares = field(default_factory=HitTypeShares)
    sp_share: float = 1.0                 # 1.0 = ignore bullpen (sheet behaviour)
    bullpen_rate: "float | None" = None   # league-avg reliever TB/BF for non-SP PAs
    batter_event_rates: "dict | None" = None   # {"1B":..,"2B":..,"3B":..,"HR":..} per PA
    pitcher_event_rates: "dict | None" = None  # allowed per BF, vs this batter's hand
    park_event_mult: "dict | None" = None      # optional per-event park factors
    league: float = LEAGUE_TB_PER_PA
    reg_k: float = REG_K_PA
    use_regression: bool = True


@dataclass
class ProjectionResult:
    batter_rate: float
    pitcher_rate: float
    matchup_rate: float       # Log5 TB/PA
    lam: float                # projected TB (expPA * rate * park)
    p_cover: float            # exact-distribution method
    p_cover_poisson: float
    breakeven: "float | None"
    model_ev: "float | None"
    edge: "float | None"      # p_cover - breakeven
    verdict: str
    distribution: list[float]


def project(inp: ProjectionInput) -> ProjectionResult:
    b = regress(inp.batter_tb_per_pa, inp.batter_pa_sample, inp.league, inp.reg_k) \
        if inp.use_regression else inp.batter_tb_per_pa
    p = regress(inp.pitcher_tb_per_pa_allowed, inp.pitcher_bf_sample, inp.league, inp.reg_k) \
        if inp.use_regression else inp.pitcher_tb_per_pa_allowed

    if inp.batter_event_rates and inp.pitcher_event_rates:
        # ---- per-event (component) log5 path ----
        pmf_sp = component_log5_pmf(inp.batter_event_rates, inp.pitcher_event_rates,
                                    park_event_mult=inp.park_event_mult,
                                    park_mult=inp.park_mult)
        # bullpen: scale league event rates to the bullpen TB level, still log5 vs batter
        pen_scale = (inp.bullpen_rate if inp.bullpen_rate else inp.league) / inp.league
        pen_event = {e: r * pen_scale for e, r in LEAGUE_EVENT_RATES.items()}
        pmf_pen = component_log5_pmf(inp.batter_event_rates, pen_event,
                                     park_event_mult=inp.park_event_mult,
                                     park_mult=inp.park_mult)
        matchup = sum(i * pmf_sp[i] for i in range(1, 5))   # expected TB/PA vs starter
    else:
        # ---- aggregate TB/PA path (original) ----
        matchup = log5_rate(b, p, inp.league) * inp.park_mult
        pmf_sp = single_pa_pmf(matchup, inp.shares)
        pen_rate = (inp.bullpen_rate if inp.bullpen_rate else inp.league) * inp.park_mult
        pmf_pen = single_pa_pmf(pen_rate, inp.shares)

    dist = tb_distribution(inp.expected_pa, pmf_sp, pmf_pen, inp.sp_share)
    lam = sum(tb * pdist for tb, pdist in enumerate(dist))   # mean of final distribution

    p_cover = p_cover_from_dist(dist, inp.line, inp.side)
    p_pois = p_cover_poisson(lam, inp.line, inp.side)

    breakeven = american_to_implied(inp.odds) if inp.odds is not None else None
    model_ev = edge = None
    if inp.odds is not None:
        payout = american_to_decimal_profit(inp.odds)
        model_ev = p_cover * payout - (1 - p_cover)
        edge = p_cover - breakeven

    verdict = "—"
    if edge is not None:
        verdict = "VALUE" if edge >= 0.05 else ("Lean" if edge >= 0 else "Pass")

    return ProjectionResult(
        batter_rate=b, pitcher_rate=p, matchup_rate=matchup, lam=lam,
        p_cover=p_cover, p_cover_poisson=p_pois, breakeven=breakeven,
        model_ev=model_ev, edge=edge, verdict=verdict, distribution=dist,
    )
