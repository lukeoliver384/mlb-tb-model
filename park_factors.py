"""
Park factors for total bases, by batter handedness.

Source: Baseball Savant Statcast Park Factors (statcast-park-factors leaderboard),
3-year regressed window (2023-2025), split by batter hand. Each park's per-hit-type
indices (1B/2B/3B/HR, where 100 = league average) were combined into a single
total-bases factor, weighted by each hit type's share of league total bases:

    TB factor = 0.40*1B + 0.24*2B + 0.03*3B + 0.33*HR   (indices, /100)

Triples were clamped to [60,160] before weighting (tiny sample, very noisy), and
the whole factor was shrunk 15% toward 1.0 so single-season noise and the
single-game application can't manufacture edge. Re-pull from Savant each season
and re-run the build to refresh these.

Parks with no 2023-2025 Savant history (Sutter Health Park, Steinbrenner Field)
default to neutral 1.0.
"""

# venue (MLB StatsAPI name) -> {"L": lefty TB factor, "R": righty TB factor}
PARK_TB_HAND = {
    "American Family Field": {"L": 0.943, "R": 0.982},
    "Angel Stadium": {"L": 0.98, "R": 1.021},
    "Busch Stadium": {"L": 0.987, "R": 0.995},
    "Chase Field": {"L": 0.981, "R": 1.054},
    "Citi Field": {"L": 0.947, "R": 0.97},
    "Citizens Bank Park": {"L": 1.053, "R": 1.006},
    "Comerica Park": {"L": 1.001, "R": 0.988},
    "Coors Field": {"L": 1.115, "R": 1.133},
    "Daikin Park": {"L": 1.015, "R": 0.999},
    "Dodger Stadium": {"L": 1.003, "R": 1.057},
    "Fenway Park": {"L": 1.077, "R": 1.001},
    "Globe Life Field": {"L": 0.988, "R": 0.981},
    "Great American Ball Park": {"L": 1.055, "R": 1.036},
    "Kauffman Stadium": {"L": 0.978, "R": 1.034},
    "Nationals Park": {"L": 1.026, "R": 0.994},
    "Oracle Park": {"L": 0.961, "R": 0.98},
    "Oriole Park at Camden Yards": {"L": 1.065, "R": 1.001},
    "PNC Park": {"L": 0.999, "R": 0.942},
    "Petco Park": {"L": 0.945, "R": 0.986},
    "Progressive Field": {"L": 0.974, "R": 0.944},
    "Rate Field": {"L": 0.968, "R": 0.966},
    "Rogers Centre": {"L": 0.985, "R": 1.011},
    "T-Mobile Park": {"L": 0.943, "R": 0.89},
    "Target Field": {"L": 1.018, "R": 1.036},
    "Truist Park": {"L": 1.021, "R": 1.005},
    "Wrigley Field": {"L": 0.977, "R": 0.959},
    "Yankee Stadium": {"L": 0.994, "R": 0.992},
    "loanDepot park": {"L": 1.035, "R": 0.984},
}

# Name aliases so MLB StatsAPI venue strings still resolve.
PARK_ALIASES = {
    "Minute Maid Park": "Daikin Park",
    "Guaranteed Rate Field": "Rate Field",
    "UNIQLO Field at Dodger Stadium": "Dodger Stadium",
}


def park_mult_hand(venue: str, side: str) -> float:
    """
    Total-bases park multiplier for a given venue and batting side ("L"/"R").
    Switch hitters: pass the side they'll bat from against this pitcher.
    Unknown venue or side -> 1.0 (neutral) / average of the two sides.
    """
    v = PARK_ALIASES.get(venue, venue)
    entry = PARK_TB_HAND.get(v)
    if not entry:
        return 1.0
    if side in ("L", "R"):
        return entry[side]
    return round((entry["L"] + entry["R"]) / 2, 3)


def park_mult(venue: str) -> float:
    """Handedness-agnostic park factor (average of L/R). Kept for compatibility."""
    return park_mult_hand(venue, "")


# Expected plate appearances by batting-order slot (approximate, full game).
PA_BY_SLOT = {1: 4.65, 2: 4.55, 3: 4.45, 4: 4.35, 5: 4.25,
              6: 4.15, 7: 4.05, 8: 3.95, 9: 3.85}


def expected_pa(slot: int) -> float:
    return PA_BY_SLOT.get(slot, 4.3)
