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
from math import exp, factorial
from typing import Iterable

LEAGUE_TB_PER_PA = 0.355   # sheet E13
REG_K_PA = 175             # sheet E12 (regression constant, in PA)


# --------------------------------------------------------------------------- #
# Odds helpers (American)                                                      #
# --------------------------------------------------------------------------- #
def american_to_decimal_profit(odds: float) -> float:
    """Profit per 1u stake (sheet's IF(G>0,G/100,100/ABS(G)))."""
    return odds / 100 if odds > 0 else 100 / abs(odds)


def american_to_implied(odds: float) -> float:
    """Implied (vig-inclusive) probability of a single American price."""
    return 100 / (odds + 100) if odds > 0 else abs(odds) / (abs(odds) + 100)


def prob_to_american(p: float) -> float:
    """Fair probability -> fair American odds (sheet F2 logic)."""
    if p <= 0 or p >= 1:
        return float("nan")
    return -p / (1 - p) * 100 if p >= 0.5 else (1 - p) / p * 100


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


def log5_rate(batter: float, pitcher: float, league: float = LEAGUE_TB_PER_PA) -> float:
    """
    Sheet J12 odds-ratio Log5:
        (b*p/l) / (b*p/l + (1-b)(1-p)/(1-l))
    Returns the matchup TB/PA rate.
    """
    num = batter * pitcher / league
    denom = num + (1 - batter) * (1 - pitcher) / (1 - league)
    return num / denom


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


def p_cover_poisson(lam: float, line: float, side: str) -> float:
    """The sheet's original Poisson method (J14), kept for comparison."""
    k = int(line)
    cdf = sum(exp(-lam) * lam**i / factorial(i) for i in range(k + 1))
    return (1 - cdf) if side.lower() == "over" else cdf


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

    matchup = log5_rate(b, p, inp.league) * inp.park_mult
    lam = matchup * inp.expected_pa

    pmf_sp = single_pa_pmf(matchup, inp.shares)
    pmf_pen = single_pa_pmf(inp.league * inp.park_mult, inp.shares)  # league-avg bullpen
    dist = tb_distribution(inp.expected_pa, pmf_sp, pmf_pen, inp.sp_share)

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
