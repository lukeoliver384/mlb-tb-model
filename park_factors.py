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


# Per-event park factors by batter hand (1B/2B/3B/HR multipliers), from the same
# Savant 2023-2025 per-event indices, shrunk 15% toward 1.0 (triples clamped).
# Lets the park reshape the XBH-vs-singles mix by handedness, not just overall level.
PARK_EVENT_HAND = {
    "American Family Field": {"L": {'1B': 0.966, '2B': 0.813, '3B': 1.042, 'HR': 1.0}, "R": {'1B': 0.941, '2B': 0.941, '3B': 0.745, 'HR': 1.085}},
    "Angel Stadium": {"L": {'1B': 0.958, '2B': 0.881, '3B': 1.0, 'HR': 1.077}, "R": {'1B': 0.992, '2B': 0.932, '3B': 0.949, 'HR': 1.127}},
    "Busch Stadium": {"L": {'1B': 1.06, '2B': 1.0, '3B': 0.813, 'HR': 0.906}, "R": {'1B': 1.06, '2B': 1.077, '3B': 0.847, 'HR': 0.873}},
    "Chase Field": {"L": {'1B': 1.026, '2B': 1.085, '3B': 1.51, 'HR': 0.804}, "R": {'1B': 1.026, '2B': 1.153, '3B': 1.51, 'HR': 0.974}},
    "Citi Field": {"L": {'1B': 0.941, '2B': 0.941, '3B': 0.694, 'HR': 0.983}, "R": {'1B': 0.941, '2B': 0.889, '3B': 0.839, 'HR': 1.077}},
    "Citizens Bank Park": {"L": {'1B': 0.974, '2B': 0.932, '3B': 1.026, 'HR': 1.238}, "R": {'1B': 1.0, '2B': 0.992, '3B': 0.983, 'HR': 1.026}},
    "Comerica Park": {"L": {'1B': 1.017, '2B': 0.923, '3B': 1.51, 'HR': 0.992}, "R": {'1B': 0.992, '2B': 0.983, '3B': 0.839, 'HR': 1.0}},
    "Coors Field": {"L": {'1B': 1.127, '2B': 1.144, '3B': 1.51, 'HR': 1.042}, "R": {'1B': 1.144, '2B': 1.178, '3B': 1.51, 'HR': 1.051}},
    "Daikin Park": {"L": {'1B': 0.983, '2B': 0.966, '3B': 1.06, 'HR': 1.085}, "R": {'1B': 1.008, '2B': 0.974, '3B': 0.779, 'HR': 1.026}},
    "Dodger Stadium": {"L": {'1B': 0.923, '2B': 0.958, '3B': 0.677, 'HR': 1.161}, "R": {'1B': 0.941, '2B': 0.974, '3B': 0.711, 'HR': 1.289}},
    "Fenway Park": {"L": {'1B': 1.051, '2B': 1.382, '3B': 0.932, 'HR': 0.898}, "R": {'1B': 1.06, '2B': 1.026, '3B': 0.958, 'HR': 0.915}},
    "Globe Life Field": {"L": {'1B': 0.983, '2B': 0.992, '3B': 0.703, 'HR': 1.017}, "R": {'1B': 0.966, '2B': 0.941, '3B': 0.839, 'HR': 1.042}},
    "Great American Ball Park": {"L": {'1B': 0.958, '2B': 0.992, '3B': 0.941, 'HR': 1.23}, "R": {'1B': 0.974, '2B': 0.992, '3B': 0.66, 'HR': 1.178}},
    "Kauffman Stadium": {"L": {'1B': 1.017, '2B': 1.119, '3B': 1.51, 'HR': 0.779}, "R": {'1B': 1.034, '2B': 1.102, '3B': 1.51, 'HR': 0.941}},
    "Nationals Park": {"L": {'1B': 1.094, '2B': 1.0, '3B': 0.906, 'HR': 0.974}, "R": {'1B': 1.06, '2B': 0.966, '3B': 1.119, 'HR': 0.923}},
    "Oracle Park": {"L": {'1B': 1.034, '2B': 1.017, '3B': 1.178, 'HR': 0.813}, "R": {'1B': 1.034, '2B': 1.026, '3B': 1.187, 'HR': 0.864}},
    "Oriole Park at Camden Yards": {"L": {'1B': 1.026, '2B': 0.932, '3B': 0.941, 'HR': 1.221}, "R": {'1B': 1.034, '2B': 1.026, '3B': 1.502, 'HR': 0.898}},
    "PNC Park": {"L": {'1B': 0.992, '2B': 1.187, '3B': 0.813, 'HR': 0.889}, "R": {'1B': 1.042, '2B': 1.085, '3B': 0.906, 'HR': 0.72}},
    "Petco Park": {"L": {'1B': 0.974, '2B': 0.983, '3B': 0.66, 'HR': 0.906}, "R": {'1B': 0.966, '2B': 0.889, '3B': 0.762, 'HR': 1.102}},
    "Progressive Field": {"L": {'1B': 0.992, '2B': 1.017, '3B': 0.66, 'HR': 0.949}, "R": {'1B': 0.974, '2B': 1.094, '3B': 1.068, 'HR': 0.787}},
    "Rate Field": {"L": {'1B': 1.017, '2B': 0.906, '3B': 0.813, 'HR': 0.966}, "R": {'1B': 1.0, '2B': 0.958, '3B': 0.668, 'HR': 0.958}},
    "Rogers Centre": {"L": {'1B': 0.923, '2B': 1.068, '3B': 0.796, 'HR': 1.017}, "R": {'1B': 1.008, '2B': 1.026, '3B': 0.668, 'HR': 1.034}},
    "T-Mobile Park": {"L": {'1B': 0.941, '2B': 0.949, '3B': 0.66, 'HR': 0.966}, "R": {'1B': 0.889, '2B': 0.873, '3B': 0.66, 'HR': 0.923}},
    "Target Field": {"L": {'1B': 1.008, '2B': 1.034, '3B': 0.932, 'HR': 1.026}, "R": {'1B': 0.992, '2B': 1.153, '3B': 1.008, 'HR': 1.008}},
    "Truist Park": {"L": {'1B': 1.051, '2B': 0.906, '3B': 0.847, 'HR': 1.085}, "R": {'1B': 1.017, '2B': 0.992, '3B': 1.008, 'HR': 1.0}},
    "Wrigley Field": {"L": {'1B': 1.0, '2B': 0.923, '3B': 1.315, 'HR': 0.958}, "R": {'1B': 0.974, '2B': 0.855, '3B': 0.949, 'HR': 1.017}},
    "Yankee Stadium": {"L": {'1B': 0.923, '2B': 0.923, '3B': 0.66, 'HR': 1.161}, "R": {'1B': 0.923, '2B': 0.906, '3B': 0.736, 'HR': 1.161}},
    "loanDepot park": {"L": {'1B': 1.068, '2B': 1.034, '3B': 1.17, 'HR': 0.983}, "R": {'1B': 1.017, '2B': 1.077, '3B': 1.127, 'HR': 0.864}},
}


def park_event_hand(venue: str, side: str, strength: float = 1.0):
    """Per-event park multipliers {1B,2B,3B,HR} for venue+batting side.
    `strength` scales each factor's deviation from 1.0. Unknown -> all 1.0."""
    v = PARK_ALIASES.get(venue, venue)
    entry = PARK_EVENT_HAND.get(v)
    if not entry:
        return {"1B": 1.0, "2B": 1.0, "3B": 1.0, "HR": 1.0}
    side = side if side in ("L", "R") else "R"
    return {ev: round(1 + (m - 1) * strength, 4) for ev, m in entry[side].items()}


# --------------------------------------------------------------------------- #
# Weather (ballpark geometry + wind/temperature model)                        #
# --------------------------------------------------------------------------- #
# lat/lon for the weather pull, cf_bearing = compass degrees from home plate
# toward center field (used to resolve wind out/in), roof type.
# NOTE: cf_bearing values are approximate — verify against published park
# azimuths and tune. A wrong bearing flips the wind sign, so when unsure,
# lower the weather strength or disable weather for that park.
PARK_GEO = {
    "Fenway Park":                  {"lat": 42.346, "lon": -71.097, "cf_bearing": 47,  "roof": "open"},
    "Yankee Stadium":               {"lat": 40.829, "lon": -73.926, "cf_bearing": 27,  "roof": "open"},
    "Oriole Park at Camden Yards":  {"lat": 39.284, "lon": -76.622, "cf_bearing": 32,  "roof": "open"},
    "Tropicana Field":              {"lat": 27.768, "lon": -82.653, "cf_bearing": 50,  "roof": "dome"},
    "Rogers Centre":                {"lat": 43.641, "lon": -79.389, "cf_bearing": 348, "roof": "retractable"},
    "Rate Field":                   {"lat": 41.830, "lon": -87.634, "cf_bearing": 124, "roof": "open"},
    "Progressive Field":            {"lat": 41.496, "lon": -81.685, "cf_bearing": 0,   "roof": "open"},
    "Comerica Park":                {"lat": 42.339, "lon": -83.049, "cf_bearing": 28,  "roof": "open"},
    "Kauffman Stadium":             {"lat": 39.051, "lon": -94.480, "cf_bearing": 50,  "roof": "open"},
    "Target Field":                 {"lat": 44.982, "lon": -93.278, "cf_bearing": 65,  "roof": "open"},
    "Daikin Park":                  {"lat": 29.757, "lon": -95.355, "cf_bearing": 345, "roof": "retractable"},
    "Angel Stadium":                {"lat": 33.800, "lon": -117.883,"cf_bearing": 45,  "roof": "open"},
    "Sutter Health Park":           {"lat": 38.580, "lon": -121.514,"cf_bearing": 30,  "roof": "open"},
    "T-Mobile Park":                {"lat": 47.591, "lon": -122.332,"cf_bearing": 2,   "roof": "retractable"},
    "Globe Life Field":             {"lat": 32.747, "lon": -97.084, "cf_bearing": 0,   "roof": "retractable"},
    "Truist Park":                  {"lat": 33.891, "lon": -84.468, "cf_bearing": 30,  "roof": "open"},
    "loanDepot park":               {"lat": 25.778, "lon": -80.220, "cf_bearing": 40,  "roof": "retractable"},
    "Citi Field":                   {"lat": 40.757, "lon": -73.846, "cf_bearing": 28,  "roof": "open"},
    "Citizens Bank Park":           {"lat": 39.906, "lon": -75.166, "cf_bearing": 15,  "roof": "open"},
    "Nationals Park":               {"lat": 38.873, "lon": -77.007, "cf_bearing": 30,  "roof": "open"},
    "Wrigley Field":                {"lat": 41.948, "lon": -87.656, "cf_bearing": 36,  "roof": "open"},
    "Great American Ball Park":     {"lat": 39.097, "lon": -84.507, "cf_bearing": 60,  "roof": "open"},
    "American Family Field":        {"lat": 43.028, "lon": -87.971, "cf_bearing": 36,  "roof": "retractable"},
    "PNC Park":                     {"lat": 40.447, "lon": -80.006, "cf_bearing": 60,  "roof": "open"},
    "Busch Stadium":                {"lat": 38.622, "lon": -90.193, "cf_bearing": 58,  "roof": "open"},
    "Chase Field":                  {"lat": 33.445, "lon": -112.067,"cf_bearing": 2,   "roof": "retractable"},
    "Coors Field":                  {"lat": 39.756, "lon": -104.994,"cf_bearing": 0,   "roof": "open"},
    "Dodger Stadium":               {"lat": 34.074, "lon": -118.240,"cf_bearing": 25,  "roof": "open"},
    "Petco Park":                   {"lat": 32.707, "lon": -117.157,"cf_bearing": 32,  "roof": "open"},
    "Oracle Park":                  {"lat": 37.778, "lon": -122.389,"cf_bearing": 88,  "roof": "open"},
}

import math as _math


def wind_out_component(speed_mph: float, dir_from_deg: float, cf_bearing: float) -> float:
    """
    Signed wind component along the home-plate->CF axis.
      +mph = blowing OUT to center, -mph = blowing IN from center.
    `dir_from_deg` is meteorological (direction wind comes FROM).
    """
    blow_to = (dir_from_deg + 180) % 360          # direction wind blows toward
    return speed_mph * _math.cos(_math.radians(blow_to - cf_bearing))


def weather_event_mult(temp_f: float, wind_out_mph: float, base_temp: float = 73.0) -> dict:
    """
    Per-event weather multipliers {1B,2B,3B,HR}, applied on top of park factors.
    Warm air + wind blowing out help the ball carry (HR most, XBH some, 1B little).

    IMPORTANT: this measures the DEVIATION from the park's seasonal-normal
    conditions, not the absolute. The Savant park factors already bake in each
    park's typical climate, so weather should only move the projection when the
    day is unusual (hotter/colder/windier than normal). `base_temp` is the park's
    normal game-time temperature; wind is centered on 0 (a park's seasonal net
    out/in averages ~0 since direction varies day to day). On a normal day the
    multipliers come out ~1.0, keeping the slate league-neutral.
    Conservative coefficients; tune against results via the accuracy tracker.
    """
    dt = (temp_f - base_temp) if temp_f is not None else 0.0
    w = wind_out_mph or 0.0
    hr = 1 + dt * 0.007 + w * 0.015      # ~0.7%/degF, ~1.5%/mph out
    xb = 1 + dt * 0.003 + w * 0.006      # doubles/triples: smaller
    return {
        "1B": 1.0,                       # singles ~ unaffected
        "2B": max(0.6, min(1.5, xb)),
        "3B": max(0.6, min(1.5, xb)),
        "HR": max(0.5, min(1.7, hr)),
    }


def combine_event_mults(*mults: dict) -> dict:
    """Multiply several per-event multiplier dicts together."""
    out = {"1B": 1.0, "2B": 1.0, "3B": 1.0, "HR": 1.0}
    for m in mults:
        if not m:
            continue
        for k in out:
            out[k] *= m.get(k, 1.0)
    return out


# Approximate seasonal (regular-season, game-time) normal temperatures (F).
# Used to center the weather adjustment; tune or replace with a climatology pull.
PARK_NORMAL_TEMP = {
    "Oriole Park at Camden Yards": 74, "Fenway Park": 70, "Yankee Stadium": 74,
    "Tropicana Field": 72, "Rogers Centre": 72, "Rate Field": 72,
    "Progressive Field": 71, "Comerica Park": 71, "Kauffman Stadium": 78,
    "Target Field": 72, "Daikin Park": 84, "Angel Stadium": 75,
    "Sutter Health Park": 86, "T-Mobile Park": 68, "Globe Life Field": 86,
    "Truist Park": 80, "loanDepot park": 84, "Citi Field": 74,
    "Citizens Bank Park": 75, "Nationals Park": 78, "Wrigley Field": 72,
    "Great American Ball Park": 76, "American Family Field": 72, "PNC Park": 71,
    "Busch Stadium": 80, "Chase Field": 90, "Coors Field": 72,
    "Dodger Stadium": 75, "Petco Park": 70, "Oracle Park": 62,
}


def park_normal_temp(venue: str) -> float:
    """Seasonal-normal game-time temperature for centering weather. Default 73."""
    return PARK_NORMAL_TEMP.get(PARK_ALIASES.get(venue, venue), 73.0)


def weather_applies(venue: str, roof_closed: bool = False) -> bool:
    """Dome -> never; retractable -> only if open (default open); open-air -> yes."""
    geo = PARK_GEO.get(PARK_ALIASES.get(venue, venue))
    if not geo:
        return False
    if geo["roof"] == "dome":
        return False
    if geo["roof"] == "retractable" and roof_closed:
        return False
    return True
