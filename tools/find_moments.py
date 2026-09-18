#!/usr/bin/env python3
"""Rank the moments in a game that are worth coaching, jungle-first.

  python tools/find_moments.py                  # every fetched match
  python tools/find_moments.py NA1_5643527353

Writes data/matches/<id>/moments.json. Each moment carries the game state
around it, so Claude can judge most of them without video; `needs_video`
marks the ones where seeing it should change the verdict.
"""

from __future__ import annotations

import argparse
import json
from pathlib import Path

import lolmap
from riot import DATA

# How many moments to keep per game. Capture time and review effort both scale
# with this, and a 12-item list is already more than one session can absorb.
MAX_MOMENTS = 12

# Seconds of lead-in and follow-through for a clip of each moment type.
WINDOWS = {
    "death": (10, 4),
    "objective_lost": (35, 5),
    "objective_taken": (25, 5),
    "objective_spawn": (20, 10),
    "clear_stall": (0, 0),
    "gold_swing": (30, 10),
    "counter_jungle": (20, 10),
}


def clock(ms: int) -> str:
    s = ms // 1000
    return f"{s // 60}:{s % 60:02d}"


class Game:
    """One match plus timeline, indexed for lookups."""

    def __init__(self, match: dict, timeline: dict, meta: dict):
        self.info = match["info"]
        self.meta = meta
        self.frames = timeline["info"]["frames"]
        self.events = [e for f in self.frames for e in f["events"]]
        self.pid = meta["participant_id"]

        self.players = {p["participantId"]: p for p in self.info["participants"]}
        self.me = self.players[self.pid]
        self.team = self.me["teamId"]
        self.name = {i: p["championName"] for i, p in self.players.items()}
        self.enemy_jungler = next(
            (i for i, p in self.players.items()
             if p["teamId"] != self.team and p.get("teamPosition") == "JUNGLE"), None)

    # --- lookups ---------------------------------------------------------

    def frame_at(self, ms: int) -> dict:
        """The last frame at or before `ms` (frames are one per minute)."""
        best = self.frames[0]
        for f in self.frames:
            if f["timestamp"] <= ms:
                best = f
            else:
                break
        return best

    def pf(self, ms: int, pid: int) -> dict:
        return self.frame_at(ms)["participantFrames"][str(pid)]

    def pos(self, ms: int, pid: int) -> tuple[int, int]:
        p = self.pf(ms, pid)["position"]
        return (p["x"], p["y"])

    def team_gold_delta(self, ms: int) -> int:
        """Our team's gold minus theirs at the frame covering `ms`."""
        f = self.frame_at(ms)
        ours = theirs = 0
        for pid_s, pf in f["participantFrames"].items():
            g = pf["totalGold"]
            if self.players[int(pid_s)]["teamId"] == self.team:
                ours += g
            else:
                theirs += g
        return ours - theirs

    def jungle_cs(self, ms: int, pid: int) -> int:
        pf = self.pf(ms, pid)
        return pf["jungleMinionsKilled"]

    def snapshot(self, ms: int) -> dict:
        """Everyone's zone and level at `ms`, for judging decisions.

        Positions come from the once-a-minute frames, so they can be up to 60s
        stale. Callers record `state_as_of` next to this so a review never
        claims someone "was" somewhere they had already left.
        """
        out = {}
        for pid, p in self.players.items():
            pos = self.pos(ms, pid)
            pf = self.pf(ms, pid)
            out[self.name[pid]] = {
                "team": "ally" if p["teamId"] == self.team else "enemy",
                "zone": lolmap.zone(pos, self.team),
                "level": pf["level"],
                "pos": list(pos),
            }
        return out


def deaths(g: Game) -> list[dict]:
    """Every death, with who did it and what the map looked like."""
    out = []
    for e in g.events:
        if e["type"] != "CHAMPION_KILL" or e.get("victimId") != g.pid:
            continue
        ms = e["timestamp"]
        pos = (e["position"]["x"], e["position"]["y"])
        killers = [g.name[i] for i in
                   [e.get("killerId")] + list(e.get("assistingParticipantIds", []))
                   if i in g.name]
        # victimDamageReceived names everything that hurt us in the last seconds.
        dmg: dict[str, int] = {}
        for d in e.get("victimDamageReceived", []):
            src = d.get("name") or "unknown"
            dmg[src] = dmg.get(src, 0) + d.get("physicalDamage", 0) + \
                d.get("magicDamage", 0) + d.get("trueDamage", 0)

        out.append({
            "type": "death",
            "t_ms": ms,
            "clock": clock(ms),
            "zone": lolmap.zone(pos, g.team),
            "pos": list(pos),
            "killed_by": killers,
            "damage_sources": dict(sorted(dmg.items(), key=lambda kv: -kv[1])),
            "my_level": g.pf(ms, g.pid)["level"],
            "bounty_given": e.get("bounty", 0),
            "shutdown": e.get("shutdownBounty", 0),
            "team_gold_delta": g.team_gold_delta(ms),
            "solo_death": len(killers) == 1,
            "score": 100 + (10 if len(killers) == 1 else 0),
            "why": f'died to {", ".join(killers) or "unknown"} in {lolmap.zone(pos, g.team)}',
            "needs_video": True,
        })
    return out


def objectives(g: Game) -> list[dict]:
    """Elite monster kills: did we take it, and where were we if we didn't?

    Attribution beats geometry. `killerId` and the assist list are exact, while
    distance-to-pit is measured against a frame up to 59s old — a jungler who
    solos a dragon at 6:57 still shows up "8.5k away" at the 6:00 frame. So a
    credited takedown means present, full stop, and distance is only consulted
    when we weren't credited at all.
    """
    out = []
    for e in g.events:
        if e["type"] != "ELITE_MONSTER_KILL":
            continue
        ms = e["timestamp"]
        mtype = e.get("monsterType", "?")
        killer = e.get("killerId")
        assists = list(e.get("assistingParticipantIds", []))
        ours = e.get("killerTeamId") == g.team
        credited = killer == g.pid or g.pid in assists

        my_pos = g.pos(ms, g.pid)
        pit = lolmap.PITS.get(mtype.upper())
        d = lolmap.dist(my_pos, pit) if pit else None
        stale = (ms - g.frame_at(ms)["timestamp"]) // 1000

        # Only guess from position when the event itself doesn't credit us.
        if credited:
            present, basis = True, "credited by the kill event"
        elif d is None:
            present, basis = False, "no pit location for this monster"
        else:
            present = d < 2500
            basis = f"position {stale}s stale, {round(d / 1000, 1)}k from pit"

        why = f'we took {mtype}' if ours else f'they took {mtype}'
        if credited:
            why += " — you got the takedown"
        elif d is not None:
            why += f', you were {round(d / 1000, 1)}k from the pit ' \
                   f'({lolmap.zone(my_pos, g.team)}, from the {clock(g.frame_at(ms)["timestamp"])} frame)'

        out.append({
            "type": "objective_taken" if ours else "objective_lost",
            "t_ms": ms,
            "clock": clock(ms),
            "monster": mtype + (f' ({e["monsterSubType"]})' if e.get("monsterSubType") else ""),
            "taken_by": "us" if ours else "them",
            "i_secured": killer == g.pid,
            "i_assisted": g.pid in assists,
            "my_zone": lolmap.zone(my_pos, g.team),
            "my_distance_to_pit": round(d) if d else None,
            "was_i_there": present,
            "presence_basis": basis,
            "team_gold_delta": g.team_gold_delta(ms),
            # A takedown needs no coaching; an objective lost while we were far
            # away is the one worth looking at.
            "score": (15 if credited else 40 if ours else 70) +
                     (25 if d and d > 6000 and not ours and not credited else 0),
            "why": why,
            "needs_video": not ours and not credited,
        })
    return out


def objective_spawns(g: Game) -> list[dict]:
    """Where were we just before each objective spawned?

    At Iron the common miss is not a lost fight but simply not being there:
    arriving after the enemy jungler has already started it.
    """
    out = []
    dur = g.info["gameDuration"]
    for label, t in lolmap.SPAWNS.items():
        if t + 30 > dur:
            continue
        ms = t * 1000
        my_pos = g.pos(ms, g.pid)
        target = "DRAGON" if "dragon" in label else "BARON_NASHOR"
        d = lolmap.dist(my_pos, lolmap.PITS[target])
        near = d < 4000
        out.append({
            "type": "objective_spawn",
            "t_ms": ms,
            "clock": clock(ms),
            "objective": label,
            "my_zone": lolmap.zone(my_pos, g.team),
            "my_distance_to_pit": round(d),
            "in_position": near,
            "score": 30 if near else 60,
            "why": f'{label.replace("_", " ")} spawning, I was in '
                   f'{lolmap.zone(my_pos, g.team)} ({round(d / 1000, 1)}k from pit)',
            "needs_video": False,
        })
    return out


def clear_stalls(g: Game) -> list[dict]:
    """Minutes where no farm, no kill and no objective happened.

    Dead time is the cheapest thing for a low-elo jungler to fix: the camps
    are already there, nobody has to be outplayed.
    """
    out = []
    busy_ms = [e["timestamp"] for e in g.events
               if e["type"] in ("CHAMPION_KILL", "ELITE_MONSTER_KILL", "BUILDING_KILL")
               and (e.get("killerId") == g.pid or e.get("victimId") == g.pid
                    or g.pid in e.get("assistingParticipantIds", []))]

    for a, b in zip(g.frames, g.frames[1:]):
        t0, t1 = a["timestamp"], b["timestamp"]
        if t1 < 120_000:  # the first clear is judged separately
            continue
        pa, pb = a["participantFrames"][str(g.pid)], b["participantFrames"][str(g.pid)]
        farm = (pb["jungleMinionsKilled"] + pb["minionsKilled"]) - \
               (pa["jungleMinionsKilled"] + pa["minionsKilled"])
        gold = pb["totalGold"] - pa["totalGold"]
        if farm >= 4 or any(t0 - 15_000 <= t <= t1 + 15_000 for t in busy_ms):
            continue
        out.append({
            "type": "clear_stall",
            "t_ms": t0,
            "clock": f"{clock(t0)}-{clock(t1)}",
            "farm_gained": farm,
            "gold_gained": gold,
            "zone_start": lolmap.zone(g.pos(t0, g.pid), g.team),
            "zone_end": lolmap.zone(g.pos(t1, g.pid), g.team),
            # Exact frame positions, so a capture can keyframe the camera
            # across the minute instead of relying on champion follow.
            "pos_start": list(g.pos(t0, g.pid)),
            "pos_end": list(g.pos(t1, g.pid)),
            "score": 45,
            "why": f'only {farm} CS and {gold}g in this minute — dead time',
            "needs_video": False,
        })
    return out


def gold_swings(g: Game) -> list[dict]:
    """Two-minute windows where the team gold lead moved hard against us."""
    out = []
    for i in range(len(g.frames) - 2):
        t0 = g.frames[i]["timestamp"]
        t2 = g.frames[i + 2]["timestamp"]
        swing = g.team_gold_delta(t2) - g.team_gold_delta(t0)
        if swing > -1500:
            continue
        out.append({
            "type": "gold_swing",
            "t_ms": t0,
            "clock": f"{clock(t0)}-{clock(t2)}",
            "swing_gold": swing,
            "my_zone_start": lolmap.zone(g.pos(t0, g.pid), g.team),
            "score": 50,
            "why": f"team gold swung {swing}g against us over these two minutes",
            "needs_video": False,
        })
    return out


def counter_jungle(g: Game) -> list[dict]:
    """Frames where the enemy jungler was farming inside our jungle."""
    if g.enemy_jungler is None:
        return []
    out = []
    ej = g.enemy_jungler
    for a, b in zip(g.frames, g.frames[1:]):
        t1 = b["timestamp"]
        pos = g.pos(t1, ej)
        if not lolmap.own_half(pos, g.team):
            continue
        gained = g.jungle_cs(t1, ej) - g.jungle_cs(a["timestamp"], ej)
        if gained < 2:
            continue
        my_pos = g.pos(t1, g.pid)
        out.append({
            "type": "counter_jungle",
            "t_ms": t1,
            "clock": clock(t1),
            "enemy_jungler": g.name[ej],
            "their_zone": lolmap.zone(pos, g.team),
            "camps_taken": gained,
            "my_zone": lolmap.zone(my_pos, g.team),
            "distance": round(lolmap.dist(my_pos, pos)),
            "score": 40,
            "why": f'{g.name[ej]} took {gained} camps in our jungle while I was '
                   f'in {lolmap.zone(my_pos, g.team)}',
            "needs_video": False,
        })
    return out


def analyze(match_dir: Path) -> dict:
    match = json.loads((match_dir / "match.json").read_text())
    timeline = json.loads((match_dir / "timeline.json").read_text())
    meta = json.loads((match_dir / "meta.json").read_text())
    g = Game(match, timeline, meta)

    found: list[dict] = []
    for fn in (deaths, objectives, objective_spawns, clear_stalls,
               gold_swings, counter_jungle):
        found.extend(fn(g))

    found.sort(key=lambda m: (-m["score"], m["t_ms"]))
    kept = found[:MAX_MOMENTS]
    kept.sort(key=lambda m: m["t_ms"])

    for m in kept:
        pre, post = WINDOWS.get(m["type"], (15, 5))
        if pre or post:
            start = max(0, m["t_ms"] // 1000 - pre)
            m["capture"] = {
                "start_s": start,
                "end_s": m["t_ms"] // 1000 + post,
                "follow": meta["summoner_name"],
            }
        m["state"] = g.snapshot(m["t_ms"])
        frame_ms = g.frame_at(m["t_ms"])["timestamp"]
        m["state_as_of"] = clock(frame_ms)
        m["state_staleness_s"] = (m["t_ms"] - frame_ms) // 1000

    out = {
        "match_id": meta["match_id"],
        "champion": meta["champion"],
        "position": meta["position"],
        "queue": meta["queue"],
        "result": "win" if meta["win"] else "loss",
        "duration_s": meta["duration_s"],
        "enemy_jungler": g.name.get(g.enemy_jungler, "?"),
        "moments_found": len(found),
        "moments": kept,
    }
    (match_dir / "moments.json").write_text(json.dumps(out, indent=2))
    return out


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("match_ids", nargs="*", help="default: every fetched match")
    args = ap.parse_args()

    dirs = [DATA / "matches" / m for m in args.match_ids] if args.match_ids else \
        sorted(d for d in (DATA / "matches").glob("*") if (d / "timeline.json").exists())
    if not dirs:
        print("no matches fetched yet — run tools/fetch_match.py first")
        return 1

    for d in dirs:
        out = analyze(d)
        counts: dict[str, int] = {}
        for m in out["moments"]:
            counts[m["type"]] = counts.get(m["type"], 0) + 1
        summary = ", ".join(f"{v} {k}" for k, v in sorted(counts.items()))
        print(f'{out["match_id"]}  {out["result"]:<5} {out["duration_s"] // 60}min  '
              f'{out["moments_found"]:>2} found -> {len(out["moments"])} kept  ({summary})')
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
