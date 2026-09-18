#!/usr/bin/env python3
"""Bundle reviews, stats, moments and position tracks into web/data.json.

  python tools/build_review.py

The viewer is a static page: open web/index.html after running this. Clips are
picked up from data/matches/<id>/clips/ when Phase 2 capture has produced them,
and the page falls back to "capture pending" when it hasn't.
"""

from __future__ import annotations

import json
import shutil
from pathlib import Path

from riot import DATA, ROOT

WEB = ROOT / "web"

# Map coordinates are ~0..14870 on each axis; the viewer works in 0..1.
MAP_MAX = 14870


def tracks(timeline: dict, match: dict, pid: int) -> dict:
    """Per-minute series the viewer plots: gold delta, CS, and positions."""
    frames = timeline["info"]["frames"]
    players = {p["participantId"]: p for p in match["info"]["participants"]}
    team = players[pid]["teamId"]
    enemy_jungler = next((i for i, p in players.items()
                          if p["teamId"] != team and p.get("teamPosition") == "JUNGLE"), None)

    minutes, gold_delta, my_cs, my_gold = [], [], [], []
    positions = []  # one entry per minute: every champion's normalized position

    for f in frames:
        t = f["timestamp"] / 1000
        pf = f["participantFrames"]
        ours = sum(v["totalGold"] for k, v in pf.items()
                   if players[int(k)]["teamId"] == team)
        theirs = sum(v["totalGold"] for k, v in pf.items()
                     if players[int(k)]["teamId"] != team)
        mine = pf[str(pid)]

        minutes.append(round(t / 60, 2))
        gold_delta.append(ours - theirs)
        my_cs.append(mine["jungleMinionsKilled"] + mine["minionsKilled"])
        my_gold.append(mine["totalGold"])

        positions.append({
            "t": round(t / 60, 2),
            "champs": [{
                "name": players[int(k)]["championName"],
                "ally": players[int(k)]["teamId"] == team,
                "me": int(k) == pid,
                "jungler": int(k) == enemy_jungler,
                "x": round(v["position"]["x"] / MAP_MAX, 4),
                "y": round(v["position"]["y"] / MAP_MAX, 4),
                "level": v["level"],
            } for k, v in sorted(pf.items(), key=lambda kv: int(kv[0]))],
        })

    return {
        "minutes": minutes,
        "gold_delta": gold_delta,
        "my_cs": my_cs,
        "my_gold": my_gold,
        "positions": positions,
    }


def event_marks(timeline: dict, match: dict, pid: int) -> list[dict]:
    """Exact-position events for the map and the timeline rail."""
    players = {p["participantId"]: p for p in match["info"]["participants"]}
    team = players[pid]["teamId"]
    names = {i: p["championName"] for i, p in players.items()}
    out = []

    for f in timeline["info"]["frames"]:
        for e in f["events"]:
            t = e["timestamp"] / 1000
            if e["type"] == "CHAMPION_KILL":
                victim, killer = e.get("victimId"), e.get("killerId")
                if victim != pid and killer != pid and pid not in e.get("assistingParticipantIds", []):
                    continue
                pos = e.get("position", {})
                out.append({
                    "kind": "my_death" if victim == pid else "my_kill",
                    "t": round(t / 60, 2),
                    "clock": f"{int(t) // 60}:{int(t) % 60:02d}",
                    "label": (f'died to {names.get(killer, "?")}' if victim == pid
                              else f'killed {names.get(victim, "?")}'),
                    "x": round(pos.get("x", 0) / MAP_MAX, 4),
                    "y": round(pos.get("y", 0) / MAP_MAX, 4),
                })
            elif e["type"] == "ELITE_MONSTER_KILL":
                pos = e.get("position", {})
                ours = e.get("killerTeamId") == team
                out.append({
                    "kind": "objective_ours" if ours else "objective_theirs",
                    "t": round(t / 60, 2),
                    "clock": f"{int(t) // 60}:{int(t) % 60:02d}",
                    "label": ("we took " if ours else "they took ") +
                             e.get("monsterType", "?").replace("_", " ").title(),
                    "x": round(pos.get("x", 0) / MAP_MAX, 4),
                    "y": round(pos.get("y", 0) / MAP_MAX, 4),
                })
    return out


def clips_for(match_dir: Path) -> list[dict]:
    """Captured clips with the game-time window each one covers.

    Clips are matched to moments by the window they cover, not by filename:
    the suggested window in moments.json and the profile window the capture
    actually used are allowed to differ.
    """
    manifest = match_dir / "clips.json"
    clip_dir = match_dir / "clips"
    if not clip_dir.exists() or not manifest.exists():
        return []

    dest = WEB / "clips" / match_dir.name
    dest.mkdir(parents=True, exist_ok=True)

    out = []
    for key, entry in json.loads(manifest.read_text()).items():
        if entry.get("status") not in ("ok", "partial"):
            continue
        mp4 = clip_dir / f"{key}.mp4"
        if not mp4.exists():
            continue
        shutil.copy2(mp4, dest / mp4.name)
        start, end = entry.get("window", [int(key), int(key)])
        out.append({
            "key": key,
            "url": f"clips/{match_dir.name}/{mp4.name}",
            "start_s": start,
            "end_s": end,
            "status": entry["status"],
            "fps": entry.get("fps"),
            "fog_of_war": entry.get("fog_of_war"),
            "frames": entry.get("frames_captured"),
            "note": entry.get("note"),
        })
    return out


def clip_covering(clips: list[dict], t_ms: int) -> dict | None:
    """The clip whose window contains this moment, if one was captured."""
    t = t_ms / 1000
    for c in clips:
        if c["start_s"] - 1 <= t <= c["end_s"] + 1:
            return c
    return None


def video_findings_for(match_dir: Path) -> dict:
    """Phase 3 video findings, keyed by clip key (the clip's start second).

    The file is optional and written by a separate tool; a missing or
    malformed one must never break the build.
    """
    f = match_dir / "video_findings.json"
    if not f.exists():
        return {}
    try:
        data = json.loads(f.read_text())
        findings = data["findings"]
        if not isinstance(findings, dict):
            raise TypeError(f"'findings' is a {type(findings).__name__}, not an object")
        return findings
    except Exception as e:
        print(f"warning: skipping {f} — {e}")
        return {}


def player_label() -> str:
    """Who the page says it is about. Read at runtime: the repo names nobody."""
    f = DATA / "player.json"
    if not f.exists():
        return "player"
    p = json.loads(f.read_text())
    return f'{p["game_name"]}#{p["tag_line"]}'


def main() -> int:
    match_dirs = sorted(d for d in (DATA / "matches").glob("*")
                        if (d / "moments.json").exists())
    if not match_dirs:
        print("nothing to build — run fetch_match.py, find_moments.py, digest.py first")
        return 1

    games = []
    for d in match_dirs:
        moments = json.loads((d / "moments.json").read_text())
        if not moments["moments"]:
            continue  # remake or nothing worth coaching
        match = json.loads((d / "match.json").read_text())
        timeline = json.loads((d / "timeline.json").read_text())
        meta = json.loads((d / "meta.json").read_text())
        stats_f = d / "stats.json"
        review_f = DATA / "reviews" / f"{d.name}.md"

        clips = clips_for(d)
        findings = video_findings_for(d)
        for m in moments["moments"]:
            hit = clip_covering(clips, m["t_ms"])
            if hit:
                m["clip"] = hit["url"]
                m["clip_info"] = hit
                # A clip window can span two moments (e.g. an objective taken
                # and the one lost 6s later). The finding belongs to the one
                # whose clock the capture was built around, not to both.
                finding = findings.get(hit["key"])
                if finding and finding.get("clock", m["clock"]) == m["clock"]:
                    m["video"] = finding

        games.append({
            "meta": meta,
            "stats": json.loads(stats_f.read_text()) if stats_f.exists() else {},
            "moments": moments["moments"],
            "enemy_jungler": moments.get("enemy_jungler", "?"),
            "review_md": review_f.read_text() if review_f.exists() else "",
            "tracks": tracks(timeline, match, meta["participant_id"]),
            "events": event_marks(timeline, match, meta["participant_id"]),
            "video_findings": findings,
        })

    games.sort(key=lambda g: g["meta"]["played_utc"], reverse=True)

    batch_f = DATA / "batch.json"
    batch = json.loads(batch_f.read_text()) if batch_f.exists() else {}
    reviews = sorted((DATA / "reviews").glob("batch-*.md"), reverse=True)

    payload = {
        "player": player_label(),
        "games": games,
        "batch": batch.get("batch", {}),
        "batch_review_md": reviews[0].read_text() if reviews else "",
        "profile_md": (DATA / "profile.md").read_text(),
        "patterns_md": (DATA / "patterns.md").read_text(),
        # Targets come from profile.md; kept here so the dashboard can draw them.
        "targets": {
            "deaths_before_15": {"target": 2.0, "lower_is_better": True},
            "solo_deaths": {"target": 1.0, "lower_is_better": True},
            "cs_per_min": {"target": 7.0, "lower_is_better": False},
            "objectives_i_was_present_for": {"target": 4.0, "lower_is_better": False},
            "kill_participation": {"target": 0.5, "lower_is_better": False},
            "wards_per_min": {"target": 0.5, "lower_is_better": False},
        },
    }

    WEB.mkdir(exist_ok=True)
    (WEB / "data.json").write_text(json.dumps(payload))
    # fetch() is blocked on file:// URLs, so the page loads its data as a
    # script instead. This is what lets index.html open with a double-click.
    (WEB / "data.js").write_text("window.LOLCOACH = " + json.dumps(payload) + ";\n")
    clip_count = sum(1 for g in games for m in g["moments"] if m.get("clip"))
    video_count = sum(1 for g in games for m in g["moments"] if m.get("video"))
    total_moments = sum(len(g["moments"]) for g in games)
    print(f'web/data.json: {len(games)} games, {total_moments} moments, '
          f'{clip_count} with clips, {video_count} with video findings')
    print(f'open {WEB / "index.html"}')
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
