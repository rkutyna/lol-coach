#!/usr/bin/env python3
"""Jungle stat digest for a batch of games — the numbers behind the review.

  python tools/digest.py            # every fetched match + batch averages

Writes data/matches/<id>/stats.json and data/batch.json. Metrics are chosen
for a low-elo jungler: tempo, dead time, objective presence, and deaths.
"""

from __future__ import annotations

import argparse
import json
from pathlib import Path

import lolmap
from riot import DATA

# A remake or an early surrender teaches nothing.
MIN_DURATION_S = 300

# jungleMinionsKilled counts individual monsters, not camps: blue 1, gromp 1,
# wolves 3, raptors 5, red 1, krugs 5-6. A full six-camp clear is ~16.
FULL_CLEAR_CS = 16


def clock(s: float) -> str:
    return f"{int(s) // 60}:{int(s) % 60:02d}"


def game_stats(match_dir: Path) -> dict | None:
    match = json.loads((match_dir / "match.json").read_text())
    timeline = json.loads((match_dir / "timeline.json").read_text())
    meta = json.loads((match_dir / "meta.json").read_text())

    info = match["info"]
    if info["gameDuration"] < MIN_DURATION_S:
        return None

    frames = timeline["info"]["frames"]
    pid = str(meta["participant_id"])
    players = {p["participantId"]: p for p in info["participants"]}
    me = players[meta["participant_id"]]
    team = me["teamId"]
    minutes = info["gameDuration"] / 60
    ch = me.get("challenges", {})

    ej = next((i for i, p in players.items()
               if p["teamId"] != team and p.get("teamPosition") == "JUNGLE"), None)

    def at(minute: int, key: str, who: str = pid) -> int | None:
        """A participant frame value at a given minute, if the game lasted."""
        if minute >= len(frames):
            return None
        return frames[minute]["participantFrames"][who][key]

    # Tempo. Frames land once a minute, so clear timing is only good to the
    # minute — treat it as "done by", not an exact time.
    first_clear_s = None
    for f in frames:
        if f["participantFrames"][pid]["jungleMinionsKilled"] >= FULL_CLEAR_CS:
            first_clear_s = f["timestamp"] / 1000
            break

    events = [e for f in frames for e in f["events"]]
    my_deaths = [e for e in events
                 if e["type"] == "CHAMPION_KILL" and e.get("victimId") == meta["participant_id"]]
    deaths_before_15 = sum(1 for e in my_deaths if e["timestamp"] < 900_000)

    elite = [e for e in events if e["type"] == "ELITE_MONSTER_KILL"]
    ours = [e for e in elite if e.get("killerTeamId") == team]
    theirs = [e for e in elite if e.get("killerTeamId") != team]
    # Presence comes from the kill event's own credit list; position frames are
    # up to 59s stale and badly understate a jungler who solos objectives.
    present = 0
    secured = 0
    for e in ours:
        credited = (e.get("killerId") == meta["participant_id"]
                    or meta["participant_id"] in e.get("assistingParticipantIds", []))
        if e.get("killerId") == meta["participant_id"]:
            secured += 1
        if credited:
            present += 1
            continue
        f = max((fr for fr in frames if fr["timestamp"] <= e["timestamp"]),
                key=lambda fr: fr["timestamp"], default=frames[0])
        p = f["participantFrames"][pid]["position"]
        pit = lolmap.PITS.get(e.get("monsterType", "").upper())
        if pit and lolmap.dist((p["x"], p["y"]), pit) < 2500:
            present += 1

    cs10 = None
    if len(frames) > 10:
        pf = frames[10]["participantFrames"][pid]
        cs10 = pf["jungleMinionsKilled"] + pf["minionsKilled"]

    gold_diff_10 = None
    if ej is not None and len(frames) > 10:
        gold_diff_10 = (frames[10]["participantFrames"][pid]["totalGold"]
                        - frames[10]["participantFrames"][str(ej)]["totalGold"])

    stats = {
        "match_id": meta["match_id"],
        "result": "win" if meta["win"] else "loss",
        "duration_min": round(minutes, 1),
        "champion": meta["champion"],
        "enemy_jungler": players[ej]["championName"] if ej else "?",

        # Tempo
        "full_clear_by": clock(first_clear_s) if first_clear_s else "never",
        "jungle_cs_at_4": at(4, "jungleMinionsKilled"),
        "jungle_cs_at_6": at(6, "jungleMinionsKilled"),
        "cs_at_10": cs10,
        "cs_per_min": round((me["totalMinionsKilled"] + me["neutralMinionsKilled"]) / minutes, 1),
        "jungle_cs": me["neutralMinionsKilled"],
        "gold_diff_vs_jungler_at_10": gold_diff_10,

        # Deaths
        "deaths": me["deaths"],
        "deaths_before_15": deaths_before_15,
        "solo_deaths": sum(1 for e in my_deaths
                           if not e.get("assistingParticipantIds")),
        "gold_given_by_deaths": sum(e.get("bounty", 0) for e in my_deaths),

        # Objectives
        "objectives_team_took": len(ours),
        "objectives_i_was_present_for": present,
        "objectives_i_secured": secured,
        "objectives_enemy_took": len(theirs),
        "dragon_takedowns": ch.get("dragonTakedowns", 0),
        "objectives_within_30s_of_spawn": ch.get("epicMonsterKillsWithin30SecondsOfSpawn", 0),

        # Fighting and vision
        "kill_participation": round(ch.get("killParticipation", 0), 2),
        "kda": meta["kda"],
        "wards_placed": me.get("wardsPlaced", 0),
        "wards_per_min": round(me.get("wardsPlaced", 0) / minutes, 2),
        "control_wards": ch.get("controlWardsPlaced", 0),
        "vision_score": me.get("visionScore", 0),
        "enemy_jungle_cs": ch.get("enemyJungleMonsterKills", 0),
        "damage_per_min": round(ch.get("damagePerMinute", 0)),
    }
    (match_dir / "stats.json").write_text(json.dumps(stats, indent=2))
    return stats


def batch(rows: list[dict]) -> dict:
    """Averages across the batch — what a goal is judged against."""
    n = len(rows)

    def avg(key: str) -> float | None:
        vals = [r[key] for r in rows if isinstance(r.get(key), (int, float))]
        return round(sum(vals) / len(vals), 2) if vals else None

    return {
        "games": n,
        "record": f'{sum(r["result"] == "win" for r in rows)}W-'
                  f'{sum(r["result"] == "loss" for r in rows)}L',
        "avg_deaths": avg("deaths"),
        "avg_deaths_before_15": avg("deaths_before_15"),
        "avg_solo_deaths": avg("solo_deaths"),
        "avg_gold_given_by_deaths": avg("gold_given_by_deaths"),
        "avg_cs_per_min": avg("cs_per_min"),
        "avg_cs_at_10": avg("cs_at_10"),
        "avg_gold_diff_vs_jungler_at_10": avg("gold_diff_vs_jungler_at_10"),
        "avg_objective_presence": avg("objectives_i_was_present_for"),
        "avg_objectives_team_took": avg("objectives_team_took"),
        "avg_kill_participation": avg("kill_participation"),
        "avg_wards_per_min": avg("wards_per_min"),
        "avg_vision_score": avg("vision_score"),
        "full_clear_times": [r["full_clear_by"] for r in rows],
    }


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("match_ids", nargs="*")
    args = ap.parse_args()

    dirs = [DATA / "matches" / m for m in args.match_ids] if args.match_ids else \
        sorted(d for d in (DATA / "matches").glob("*") if (d / "timeline.json").exists())

    rows = []
    for d in dirs:
        s = game_stats(d)
        if s is None:
            print(f"  {d.name}: skipped (under {MIN_DURATION_S // 60} min)")
            continue
        rows.append(s)
        print(f'{s["match_id"]}  {s["result"]:<5} vs {s["enemy_jungler"]:<10} '
              f'clear {s["full_clear_by"]:<5} cs/m {s["cs_per_min"]:<5} '
              f'deaths {s["deaths"]} ({s["deaths_before_15"]} pre-15)  '
              f'obj present {s["objectives_i_was_present_for"]}/{s["objectives_team_took"]}')

    if not rows:
        print("nothing to digest")
        return 1

    b = batch(rows)
    (DATA / "batch.json").write_text(json.dumps({"batch": b, "games": rows}, indent=2))
    print(f'\nbatch: {b["games"]} games, {b["record"]}')
    for k in ("avg_deaths", "avg_deaths_before_15", "avg_cs_per_min", "avg_cs_at_10",
              "avg_objective_presence", "avg_kill_participation", "avg_wards_per_min"):
        print(f'  {k:<28} {b[k]}')
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
