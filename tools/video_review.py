#!/usr/bin/env python3
"""Prepare and validate the Phase 3 video review loop.

  python tools/video_review.py                        # build data/video_queue.json
  python tools/video_review.py --match NA1_5643462001  # one game only
  python tools/video_review.py --pending               # only clips with no finding yet
  python tools/video_review.py --state NA1_5643462001 559   # pretty-print distilled state
  python tools/video_review.py --check                 # validate every video_findings.json
  python tools/video_review.py --status                # coverage table

This tool does not look at frames — Claude does, by reading the JPEGs a
packet points at. What it does is cheap prep and validation: build the
packet (frames + distilled state + moment + questions) Claude reads, and
check the findings.json Claude writes back against the fixed schema in
docs/PHASE3.md, so a malformed write is caught before build_review.py
trusts it.
"""

from __future__ import annotations

import argparse
import json
import re
from pathlib import Path

from build_review import clip_covering
from riot import DATA, ROOT

QUEUE_F = DATA / "video_queue.json"

# State snapshots are polled at `start - WARMUP_S` (docs/PHASE2.md seeks
# early and parks there before recording), so a snapshot a few seconds ahead
# of the window is expected, not a bug. Measured offset across real clips is
# ~6s; this is a generous tolerance around that, not a tight one, so it only
# flags a snapshot that's actually for the wrong moment.
STATE_SLACK_S = 20

CONFIDENCE_VALUES = {"high", "medium", "low"}
FRAME_SPEC_RE = re.compile(r"^f(\d+)(?:-f(\d+))?$")
CLOCK_RE = re.compile(r"(\d{1,2}):(\d{2})")

DEATH_QUESTIONS = [
    "Was the killer visible on screen or minimap before they arrived, or did they appear unseen?",
    "Was the player's health already spent on a camp when the fight started?",
    "Was there an escape and was it taken?",
]
OBJECTIVE_QUESTIONS = [
    "Was smite pressed, and was it pressed early, late, or never?",
    "Who was present at the pit on each team, and when did they arrive?",
    "Was vision of the pit established before the contest?",
]
TEMPO_QUESTIONS = [
    "Was this minute a reaction to something visible on screen, or drifting with nothing happening?",
    "What was on the minimap that could have been acted on?",
]
GENERIC_QUESTION = ["What does the footage show that the stats could not?"]


# --- state distiller -------------------------------------------------------

def distill_state(raw: dict, ally_champion: str, window: list[int]) -> dict:
    """Shrink a ~24KB liveclientdata snapshot to what a reviewer needs.

    Keeps champion/team/level/position/dead/items/spells/kda/cs/ward_score
    per player. Drops every PII field (riotId, riotIdGameName, riotIdTagLine,
    summonerName) and everything else with no coaching use (runes, raw*,
    screenPosition*, skinID, skinName, isBot). There is no HP or cooldown
    data in this snapshot at all — that's what the frames are for.
    """
    all_players = raw.get("allPlayers", [])
    ally_team = next((p.get("team") for p in all_players
                      if p.get("championName") == ally_champion), None)

    players = []
    for p in all_players:
        scores = p.get("scores", {})
        spells = p.get("summonerSpells", {})
        dead = bool(p.get("isDead", False))
        entry = {
            "champion": p.get("championName"),
            "team": "ally" if p.get("team") == ally_team else "enemy",
            "level": p.get("level"),
            "position": p.get("position"),
            "dead": dead,
            "items": [i.get("displayName") for i in p.get("items", [])],
            "spells": [spells.get("summonerSpellOne", {}).get("displayName"),
                       spells.get("summonerSpellTwo", {}).get("displayName")],
            "kda": f'{scores.get("kills", 0)}/{scores.get("deaths", 0)}/{scores.get("assists", 0)}',
            "cs": scores.get("creepScore", 0),
            "ward_score": round(scores.get("wardScore", 0), 1),
        }
        if dead:
            entry["respawn_s"] = round(p.get("respawnTimer", 0), 1)
        players.append(entry)

    game_time_s = round(raw.get("gameData", {}).get("gameTime", 0))
    lo, hi = window
    return {
        "game_time_s": game_time_s,
        "game_time_matches_window": lo - STATE_SLACK_S <= game_time_s <= hi + STATE_SLACK_S,
        "players": players,
    }


def load_state(match_dir: Path, clip_key: str, ally_champion: str,
               window: list[int]) -> dict | None:
    f = match_dir / "state" / f"{clip_key}.json"
    if not f.exists():
        return None
    return distill_state(json.loads(f.read_text()), ally_champion, window)


# --- packet building --------------------------------------------------------

def frame_paths(match_dir: Path, clip_key: str) -> tuple[list[str], str | None]:
    """JPEGs for this clip, repo-root-relative, in order. (paths, problem)."""
    d = match_dir / "frames" / clip_key
    if not d.exists():
        return [], f"no frames dir at {d.relative_to(ROOT)}"
    jpgs = sorted(d.glob("*.jpg"))
    if not jpgs:
        return [], f"frames dir {d.relative_to(ROOT)} is empty"
    return [str(p.relative_to(ROOT)) for p in jpgs], None


def matching_moment(moments: list[dict], window: list[int], clock: str | None) -> dict | None:
    """The moment this clip's window covers, using build_review's own rule.

    A window can cover two moments seconds apart (e.g. an objective taken at
    9:06 and lost back at 9:12, both inside one ~14s window) — clip_covering
    would silently take whichever comes first in the list. An exact clock
    match against the clip's own `clock` (set from the one moment that made
    capture_plan build this clip) resolves the ambiguity; the window rule is
    the fallback for when clocks don't line up exactly.
    """
    exact = [m for m in moments if m.get("clock") == clock]
    if exact:
        return exact[0]
    pseudo_clip = [{"start_s": window[0], "end_s": window[1]}]
    for m in moments:
        if clip_covering(pseudo_clip, m["t_ms"]):
            return m
    return None


def trim_moment(m: dict) -> dict:
    return {
        "type": m["type"],
        "clock": m["clock"],
        "why": m["why"],
        "state_as_of": m.get("state_as_of"),
        "state_staleness_s": m.get("state_staleness_s"),
        "state": m.get("state", {}),
    }


def profile_questions(profile: str) -> list[str]:
    p = profile.lower()
    if "death" in p:
        return list(DEATH_QUESTIONS)
    if "smite" in p or "objective" in p:
        return list(OBJECTIVE_QUESTIONS)
    if "dead" in p or "tempo" in p or "idle" in p:
        return list(TEMPO_QUESTIONS)
    return list(GENERIC_QUESTION)


def needs_video_bullets(review_md: str) -> list[str]:
    """Bullets under '## Needs video', with wrapped continuation lines joined."""
    marker = "## Needs video"
    if marker not in review_md:
        return []
    section = review_md.split(marker, 1)[1].split("\n## ", 1)[0]

    bullets: list[str] = []
    current: str | None = None
    for line in section.splitlines():
        if line.startswith("- "):
            if current is not None:
                bullets.append(current.strip())
            current = line[2:].strip()
        elif line.strip():
            if current is not None:
                current += " " + line.strip()
        elif current is not None:
            bullets.append(current.strip())
            current = None
    if current is not None:
        bullets.append(current.strip())
    return bullets


def review_questions(review_md: str, window: list[int]) -> list[str]:
    """Needs-video bullets whose cited clock falls inside this clip's window."""
    lo, hi = window
    out = []
    for bullet in needs_video_bullets(review_md):
        times = [int(mm) * 60 + int(ss) for mm, ss in CLOCK_RE.findall(bullet)]
        if any(lo - 1 <= t <= hi + 1 for t in times):
            out.append(bullet.replace("**", ""))
    return out


def build_packet(match_dir: Path, clip_key: str, entry: dict, moments: list[dict],
                  champion: str, review_md: str) -> dict:
    match_id = match_dir.name
    window = entry["window"]
    frames, problem = frame_paths(match_dir, clip_key)
    moment = matching_moment(moments, window, entry.get("clock"))

    packet = {
        "match_id": match_id,
        "clip_key": clip_key,
        "clock": entry.get("clock"),
        "profile": entry.get("profile"),
        "why": entry.get("why"),
        "window": window,
        "fps": entry.get("fps"),
        "fog_of_war": entry.get("fog_of_war"),
        "frames": frames,
        "state": load_state(match_dir, clip_key, champion, window),
        "moment": trim_moment(moment) if moment else None,
        "questions": profile_questions(entry.get("profile", "")) + review_questions(review_md, window),
        "answer_into": f"data/matches/{match_id}/video_findings.json",
    }
    if problem:
        packet["problem"] = problem
    return packet


def captured_match_dirs(only: str | None) -> list[Path]:
    if only:
        d = DATA / "matches" / only
        return [d] if (d / "clips.json").exists() else []
    return sorted(d for d in (DATA / "matches").glob("*") if (d / "clips.json").exists())


def existing_findings(match_dir: Path) -> dict:
    f = match_dir / "video_findings.json"
    if not f.exists():
        return {}
    try:
        data = json.loads(f.read_text())
        findings = data.get("findings", {})
        return findings if isinstance(findings, dict) else {}
    except json.JSONDecodeError:
        return {}


def build_queue(only: str | None, pending: bool) -> list[dict]:
    packets = []
    for d in captured_match_dirs(only):
        moments_f = d / "moments.json"
        if not moments_f.exists():
            continue
        clips = json.loads((d / "clips.json").read_text())
        moments_doc = json.loads(moments_f.read_text())
        champion = moments_doc.get("champion", "")
        review_f = DATA / "reviews" / f"{d.name}.md"
        review_md = review_f.read_text() if review_f.exists() else ""
        findings = existing_findings(d)

        for key, entry in sorted(clips.items(), key=lambda kv: int(kv[0])):
            if entry.get("status") not in ("ok", "partial"):
                continue
            if pending and key in findings:
                continue
            packets.append(build_packet(d, key, entry, moments_doc["moments"], champion, review_md))
    return packets


def cmd_build(only: str | None, pending: bool) -> int:
    packets = build_queue(only, pending)
    QUEUE_F.write_text(json.dumps({"packets": packets}, indent=2))

    by_match: dict[str, list[dict]] = {}
    for p in packets:
        by_match.setdefault(p["match_id"], []).append(p)

    for match_id, ps in by_match.items():
        print(match_id)
        for p in sorted(ps, key=lambda p: int(p["clip_key"])):
            flag = f'  [{p["problem"]}]' if p.get("problem") else ""
            print(f'  {p["clip_key"]:>5}  {p["clock"]:<12} {p["profile"]:<17} '
                  f'{len(p["frames"])} frames  {p["why"]}{flag}')

    if not packets:
        print("nothing queued — run capture.py first, or check --match/--pending")
        return 1
    print(f'\n{len(packets)} packets across {len(by_match)} games -> {QUEUE_F.relative_to(ROOT)}')
    return 0


# --- state pretty-printer ----------------------------------------------------

def cmd_state(match_id: str, clip_key: str) -> int:
    d = DATA / "matches" / match_id
    clips_f = d / "clips.json"
    if not clips_f.exists():
        print(f"no clips.json for {match_id}")
        return 1
    entry = json.loads(clips_f.read_text()).get(clip_key)
    if entry is None:
        print(f"no clip {clip_key} in {match_id}/clips.json")
        return 1

    champion = json.loads((d / "moments.json").read_text()).get("champion", "")
    state = load_state(d, clip_key, champion, entry["window"])
    if state is None:
        print(f"no state file at {d / 'state' / (clip_key + '.json')}")
        return 1

    print(f'{match_id} {clip_key}  ({entry.get("clock")})  window={entry["window"]}  '
          f'game_time={state["game_time_s"]}s  matches_window={state["game_time_matches_window"]}')
    for p in state["players"]:
        status = f'DEAD (respawn {p["respawn_s"]}s)' if p["dead"] else "alive"
        print(f'  [{p["team"]:<5}] {p["champion"]:<14} {p["position"]:<8} lvl {p["level"]:<2} '
              f'{status:<20} {p["kda"]:<7} cs={p["cs"]:<3} ward={p["ward_score"]}')
        print(f'      items: {", ".join(i for i in p["items"] if i) or "(none)"}')
        print(f'      spells: {", ".join(s for s in p["spells"] if s)}')
    return 0


# --- findings validation -----------------------------------------------------

def check_frames_field(frames_dir: Path, spec: str) -> str | None:
    """None if `spec` (e.g. 'f08' or 'f08-f12') names frames that exist."""
    m = FRAME_SPEC_RE.match(spec.strip())
    if not m:
        return f"frames {spec!r} isn't fNN or fNN-fMM"
    missing = [f"f{n}.jpg" for n in {m.group(1), m.group(2) or m.group(1)}
               if not (frames_dir / f"f{n}.jpg").exists()]
    return f'missing {", ".join(sorted(missing))}' if missing else None


def check_match(d: Path) -> list[str]:
    match_id = d.name
    f = d / "video_findings.json"
    if not f.exists():
        return []

    try:
        doc = json.loads(f.read_text())
    except json.JSONDecodeError as e:
        return [f"{match_id} -: video_findings.json doesn't parse: {e}"]

    problems = []
    if doc.get("match_id") != match_id:
        problems.append(f'{match_id} -: match_id {doc.get("match_id")!r} does not match its directory')

    clips = json.loads((d / "clips.json").read_text()) if (d / "clips.json").exists() else {}
    valid_keys = {k for k, v in clips.items() if v.get("status") in ("ok", "partial")}

    findings = doc.get("findings", {})
    if not isinstance(findings, dict):
        problems.append(f"{match_id} -: 'findings' is not an object")
        return problems

    for key, finding in findings.items():
        tag = f"{match_id} {key}"
        if key not in valid_keys:
            problems.append(f"{tag}: not a captured ok/partial clip in clips.json")
            continue
        if not isinstance(finding, dict):
            problems.append(f"{tag}: finding is not an object")
            continue

        if not finding.get("verdict"):
            problems.append(f"{tag}: missing or empty verdict")

        observations = finding.get("observations") or []
        if not observations:
            problems.append(f"{tag}: no observations")

        frames_dir = d / "frames" / key
        for i, obs in enumerate(observations):
            otag = f"{tag} observation[{i}]"
            if not isinstance(obs, dict):
                problems.append(f"{otag}: not an object")
                continue
            for field in ("frames", "clock", "saw"):
                if not obs.get(field):
                    problems.append(f"{otag}: missing {field}")
            if obs.get("frames"):
                err = check_frames_field(frames_dir, obs["frames"])
                if err:
                    problems.append(f"{otag}: {err}")

        if finding.get("confidence") not in CONFIDENCE_VALUES:
            problems.append(f'{tag}: confidence {finding.get("confidence")!r} not one of high/medium/low')

        if not isinstance(finding.get("changes_stats_verdict"), bool):
            problems.append(f"{tag}: changes_stats_verdict is not a bool")

    return problems


def cmd_check() -> int:
    dirs = sorted(d for d in (DATA / "matches").glob("*") if (d / "video_findings.json").exists())
    problems = [p for d in dirs for p in check_match(d)]
    for p in problems:
        print(p)
    if not problems:
        print("all video_findings.json files check out" if dirs else "no video_findings.json files yet")
    return 1 if problems else 0


# --- coverage status ---------------------------------------------------------

def cmd_status() -> int:
    dirs = captured_match_dirs(None)
    if not dirs:
        print("no captured clips yet")
        return 0

    for d in dirs:
        clips = json.loads((d / "clips.json").read_text())
        captured = sorted((k for k, v in clips.items() if v.get("status") in ("ok", "partial")), key=int)
        findings = existing_findings(d)
        outstanding = [k for k in captured if k not in findings]
        done = len(captured) - len(outstanding)
        line = f'{d.name}: {done}/{len(captured)} reviewed'
        if outstanding:
            line += f'  outstanding: {", ".join(outstanding)}'
        print(line)
    return 0


def main() -> int:
    ap = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--match", help="only this match id")
    ap.add_argument("--pending", action="store_true", help="only clips with no finding recorded yet")
    ap.add_argument("--state", nargs=2, metavar=("MATCH_ID", "CLIP_KEY"),
                     help="pretty-print the distilled state for one clip")
    ap.add_argument("--check", action="store_true",
                     help="validate every video_findings.json, exit non-zero on any problem")
    ap.add_argument("--status", action="store_true",
                     help="coverage table: clips captured vs. findings recorded")
    args = ap.parse_args()

    if args.state:
        return cmd_state(*args.state)
    if args.check:
        return cmd_check()
    if args.status:
        return cmd_status()
    return cmd_build(args.match, args.pending)


if __name__ == "__main__":
    raise SystemExit(main())
