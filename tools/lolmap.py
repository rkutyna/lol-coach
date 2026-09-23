"""Summoner's Rift geometry: zones, distances, and objective pits.

Map coordinates run roughly 0..14870 on both axes. Blue side (team 100) spawns
near the origin; red side (team 200) near the far corner. The lanes lie along
the two edges and the diagonal, so zone tests are mostly simple geometry.
"""

from __future__ import annotations

import math

MAP_MAX = 14870
BLUE, RED = 100, 200

# Approximate pit centers, good enough for "was I near this objective".
PITS = {
    "DRAGON": (9866, 4414),
    "RIFTHERALD": (4950, 10400),
    "BARON_NASHOR": (4950, 10400),
    "HORDE": (4950, 10400),      # void grubs share the baron pit
    "ATAKHAN": (9866, 4414),     # spawns on one side; pit center varies
}

# Objective spawn times in seconds. PATCH DEPENDENT — verify when a patch
# changes jungle timers. Used only to ask "where were you just before this?".
# Checked 2026-09-21 against 20 games on 16.18: no grub kill before 8:10 and no
# herald kill before 15:34 by either team, so 6:00/14:00 were stale.
SPAWNS = {
    "first_dragon": 300,
    "void_grubs": 480,
    "rift_herald": 900,
    "baron": 1200,
}

DRAGON_RESPAWN = 300
BARON_RESPAWN = 360


def dist(a: tuple[float, float], b: tuple[float, float]) -> float:
    return math.hypot(a[0] - b[0], a[1] - b[1])


def pit(monster_type: str, subtype: str = "") -> tuple[int, int] | None:
    return PITS.get(monster_type.upper())


def own_half(pos: tuple[float, float], team: int) -> bool:
    """True if the position is on `team`'s side of the diagonal."""
    below = pos[0] + pos[1] < MAP_MAX
    return below if team == BLUE else not below


def zone(pos: tuple[float, float], team: int) -> str:
    """Coarse zone label from a player's perspective (own vs enemy side)."""
    x, y = pos
    side = "own" if own_half(pos, team) else "enemy"

    if x < 2200 and y < 2200:
        return "own base" if team == BLUE else "enemy base"
    if x > MAP_MAX - 2200 and y > MAP_MAX - 2200:
        return "own base" if team == RED else "enemy base"

    # Mid runs along the diagonal.
    if abs(x - y) < 1600:
        return "mid lane"
    # Top lane hugs the left edge then the top edge.
    if (x < 2600 and y > 2600) or (y > MAP_MAX - 2600 and x < MAP_MAX - 2600):
        return "top lane"
    # Bot lane hugs the bottom edge then the right edge.
    if (y < 2600 and x > 2600) or (x > MAP_MAX - 2600 and y < MAP_MAX - 2600):
        return "bot lane"

    for name, center in (("dragon pit", PITS["DRAGON"]), ("baron pit", PITS["BARON_NASHOR"])):
        if dist(pos, center) < 2000:
            return name

    return f"{side} jungle"


def near_pit(pos: tuple[float, float], monster_type: str, radius: float = 2500) -> bool:
    center = PITS.get(monster_type.upper())
    return center is not None and dist(pos, center) < radius
