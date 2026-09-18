#!/usr/bin/env python3
"""Decide what to capture, before touching the game client.

  python tools/capture_plan.py            # plan every fetched match
  python tools/capture_plan.py --max 8    # cap the batch

Writes data/capture_plan.json. Priorities and windows come from the capture
spec in docs/SCOPE.md: only moments where seeing the screen can change a
verdict get frames, and the cheapest useful framerate wins.
"""

from __future__ import annotations

import argparse
import json

import lolmap
from riot import DATA

# priority -> (seconds before, seconds after, fps, fog_of_war, hud)
# fog ON means "render only what our team could see" — an information question.
# fog OFF means "show what actually happened" — a mechanics or who-arrived
# question, where our own knowledge isn't the point.
# Frame rates are kept as low as the question allows. The client stops
# responding for roughly half a second per frame while it writes PNGs, so 112
# frames means the better part of a minute of apparent freeze — which is what
# made a working capture look like a hang.
PROFILES = {
    "P1_camp_death": (18, 3, 2, True, {"minimap": True, "scoreboard": True}),
    "P2_smite_contest": (12, 2, 4, False, {"minimap": True, "scoreboard": False}),
    "P3_post_clear": (0, 70, 1, True, {"minimap": True, "scoreboard": False}),
    "P4_solo_death": (25, 2, 2, True, {"minimap": True, "scoreboard": True}),
    "P5_team_fight": (15, 15, 2, False, {"minimap": True, "scoreboard": True}),
}

# Measured: ~0.5s of unresponsive client per frame written at 1280x720.
SECONDS_PER_FRAME = 0.5

# A neutral monster in the damage log means the fight was entered with a camp
# already eating health — the single most common shape of death in this batch.
MONSTER_PREFIXES = ("sru_", "SRU_")

# Within this distance of a pit, an objective the enemy took was contested,
# which makes it a smite question rather than a positioning one.
SMITE_RANGE = 1200


def is_monster(source: str) -> bool:
    return source.lower().startswith("sru_")


def classify(m: dict) -> str | None:
    """Map a detected moment to a capture profile, or None to skip it."""
    t = m["type"]

    if t == "death":
        if any(is_monster(s) for s in m.get("damage_sources", {})):
            return "P1_camp_death"
        if len(m.get("killed_by", [])) >= 3:
            return "P5_team_fight"
        return "P4_solo_death"

    if t == "objective_lost":
        d = m.get("my_distance_to_pit")
        if d is not None and d < SMITE_RANGE:
            return "P2_smite_contest"     # we were in the pit and lost it
        return "P5_team_fight"

    if t == "clear_stall" and m["t_ms"] <= 6 * 60_000:
        return "P3_post_clear"            # the dead minute after the first clear

    # Everything else is fully answered by the JSON: gold swings are team
    # aggregates, counter-jungle numbers are whole-game totals, objectives we
    # are credited with need no proof, and spawn positioning is derivable.
    return None


# Camera altitude for a parked overhead shot. Higher sees more of a fight.
ALTITUDE = {"tight": 1500, "wide": 2400}


def camera_for(profile: str, m: dict, champion: str,
               start_s: int, end_s: int) -> dict:
    """How to aim: keyframed world positions, plus a selection for the HUD.

    Champion follow through the API is unreliable — the camera often stays
    parked wherever it was — so framing comes from `/replay/sequence` at
    coordinates we already know exactly (death positions, objective pits).
    The selection is still asserted, because that reliably puts the player's
    own health bar and ability row on screen, which P1/P2/P4 need.
    """
    wide = profile == "P5_team_fight"
    alt = ALTITUDE["wide" if wide else "tight"]

    # Dead time is the one case where the player really does travel, so
    # keyframe between the two known frame positions.
    if profile == "P3_post_clear" and m.get("pos_start") and m.get("pos_end"):
        a, b = m["pos_start"], m["pos_end"]
        return {
            "hud_selection": champion,
            "keyframes": [
                {"time": start_s, "x": a[0], "z": a[1], "altitude": ALTITUDE["wide"]},
                {"time": end_s, "x": b[0], "z": b[1], "altitude": ALTITUDE["wide"]},
            ],
        }

    anchor = None
    monster = (m.get("monster") or "").split(" ")[0].upper()
    if monster:
        anchor = lolmap.PITS.get(monster)
    if anchor is None and m.get("pos"):
        anchor = tuple(m["pos"])          # exact death location
    if anchor is None and m.get("pos_start"):
        anchor = tuple(m["pos_start"])

    if anchor is None:
        # Nothing exact to aim at: assert the selection and take what we get.
        return {"hud_selection": champion, "keyframes": []}

    return {
        "hud_selection": champion,
        "keyframes": [
            {"time": start_s, "x": anchor[0], "z": anchor[1], "altitude": alt},
            {"time": end_s, "x": anchor[0], "z": anchor[1], "altitude": alt},
        ],
    }


def plan_match(match_dir, max_clips: int) -> dict:
    moments = json.loads((match_dir / "moments.json").read_text())
    meta = json.loads((match_dir / "meta.json").read_text())
    champion = meta["champion"]          # selectionName wants the champion

    jobs = []
    for m in moments["moments"]:
        profile = classify(m)
        if profile is None:
            continue
        pre, post, fps, fog, hud = PROFILES[profile]
        anchor = m["t_ms"] // 1000
        start = max(0, anchor - pre)
        end = min(meta["duration_s"], anchor + post)
        jobs.append({
            "match_id": meta["match_id"],
            "profile": profile,
            "priority": int(profile[1]),
            "clock": m["clock"],
            "anchor_s": anchor,
            "moment_type": m["type"],
            "why": m["why"],
            "start_s": start,
            "end_s": end,
            "fps": fps,
            "fog_of_war": fog,
            "hud": hud,
            "camera": camera_for(profile, m, champion, start, end),
            # An estimate, at 2.8 MB per 1280x720 PNG frame.
            "est_png_mb": round((end - start) * fps * 2.8, 1),
            "est_freeze_s": round((end - start) * fps * SECONDS_PER_FRAME),
            "name": f'{meta["match_id"]}_{start}',
        })

    jobs.sort(key=lambda j: (j["priority"], j["start_s"]))
    return {
        "match_id": meta["match_id"],
        "replay_capturable": meta["replay_capturable"],
        "patch": meta["patch"],
        "jobs": jobs[:max_clips],
        "skipped": len(moments["moments"]) - len(jobs),
    }


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("match_ids", nargs="*")
    ap.add_argument("--max", type=int, default=6, help="clips per match (default 6)")
    args = ap.parse_args()

    dirs = [DATA / "matches" / m for m in args.match_ids] if args.match_ids else \
        sorted(d for d in (DATA / "matches").glob("*") if (d / "moments.json").exists())

    plans, total, mb = [], 0, 0.0
    for d in dirs:
        p = plan_match(d, args.max)
        if not p["jobs"]:
            continue
        plans.append(p)
        total += len(p["jobs"])
        mb += sum(j["est_png_mb"] for j in p["jobs"])
        flag = "" if p["replay_capturable"] else "  [REPLAY EXPIRED]"
        print(f'{p["match_id"]}  {len(p["jobs"])} clips, {p["skipped"]} skipped{flag}')
        for j in p["jobs"]:
            cam = j["camera"]
            kf = cam["keyframes"]
            if not kf:
                where = "selection only"
            elif kf[0]["x"] == kf[-1]["x"] and kf[0]["z"] == kf[-1]["z"]:
                where = f'parked ({kf[0]["x"]:.0f},{kf[0]["z"]:.0f})'
            else:
                where = f'({kf[0]["x"]:.0f},{kf[0]["z"]:.0f})->({kf[-1]["x"]:.0f},{kf[-1]["z"]:.0f})'
            print(f'   {j["profile"]:<17} {j["clock"]:<12} {j["start_s"]:>4}-{j["end_s"]:<4}s '
                  f'{j["fps"]}fps fog={"on" if j["fog_of_war"] else "off":<3} cam={where}')

    if not plans:
        print("nothing to capture")
        return 1

    (DATA / "capture_plan.json").write_text(json.dumps({"plans": plans}, indent=2))
    secs = sum(j["end_s"] - j["start_s"] for p in plans for j in p["jobs"])
    peak = max(j["est_png_mb"] for p in plans for j in p["jobs"])
    print(f'\n{total} clips across {len(plans)} games · {secs}s of footage · '
          f'{mb / 1024:.1f} GB of PNGs written in total, '
          f'but only ~{peak:.0f} MB at a time (each clip converts and deletes before the next)')
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
