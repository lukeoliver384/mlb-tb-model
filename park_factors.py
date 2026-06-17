"""
Park multipliers for total bases, derived from your 'Park Notes' sheet
(qualitative -> multiplier). These are starting points: tune them from
Statcast/Fangraphs park factors as you gather data. Multiplier applies to the
matchup TB/PA rate. Keyed by MLB StatsAPI venue name.
"""

# Strong Hitter ~1.06 | Hitter-Friendly ~1.03 | Neutral 1.00
# Pitcher-Friendly ~0.97 | Strong Pitcher ~0.94
PARK_TB_MULT = {
    "Oriole Park at Camden Yards": 1.06,
    "Fenway Park": 1.06,
    "Yankee Stadium": 1.03,
    "Tropicana Field": 0.97,
    "George M. Steinbrenner Field": 1.03,  # Rays' 2025+ temp home
    "Rogers Centre": 1.03,
    "Guaranteed Rate Field": 0.97,
    "Rate Field": 0.97,
    "Progressive Field": 1.00,
    "Comerica Park": 1.03,
    "Kauffman Stadium": 0.97,
    "Target Field": 1.00,
    "Daikin Park": 1.03,
    "Minute Maid Park": 1.03,
    "Angel Stadium": 1.03,
    "Sutter Health Park": 1.06,
    "T-Mobile Park": 0.94,
    "Globe Life Field": 1.00,
    "Truist Park": 1.00,
    "loanDepot park": 0.97,
    "Citi Field": 1.00,
    "Citizens Bank Park": 1.03,
    "Nationals Park": 1.00,
    "Wrigley Field": 1.00,
    "Great American Ball Park": 1.06,
    "American Family Field": 1.03,
    "PNC Park": 0.97,
    "Busch Stadium": 0.97,
    "Chase Field": 1.03,
    "Coors Field": 1.10,
    "Dodger Stadium": 1.00,
    "Petco Park": 0.97,
    "Oracle Park": 0.94,
}


def park_mult(venue: str) -> float:
    return PARK_TB_MULT.get(venue, 1.00)


# Expected plate appearances by batting-order slot (approximate, full game).
PA_BY_SLOT = {1: 4.65, 2: 4.55, 3: 4.45, 4: 4.35, 5: 4.25,
              6: 4.15, 7: 4.05, 8: 3.95, 9: 3.85}


def expected_pa(slot: int) -> float:
    return PA_BY_SLOT.get(slot, 4.3)
