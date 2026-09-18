# Phase 3 — video review

Phase 2 proved capture works. Phase 3 is what capture was *for*: a handful of
coaching questions the Riot API cannot answer on its own, settled by actually
looking at the footage.

## Why this exists at all

Most of a review comes free from the API — `find_moments.py` already resolves
deaths, objective fights and gold swings from timeline data with no video. A
small number of moments stay open questions no matter how the JSON is sliced:
whether a kill was seen coming, whether smite was pressed, who arrived at a
pit and when. Those get `needs_video: true` and a captured clip. This phase
is the loop that turns that clip into an answer.

## The loop

```bash
python tools/video_review.py                  # -> data/video_queue.json + worklist
# Claude reads each packet: the frames, the distilled state, the moment,
# the questions — and writes data/matches/<id>/video_findings.json
python tools/video_review.py --check           # validate every findings file
python tools/build_review.py                   # findings flow into web/data.json
```

`video_review.py` does no vision work itself. It assembles what Claude needs
to look at one clip (`--match`/`--pending` narrow the batch) and validates
what Claude wrote back (`--check`), plus a coverage table (`--status`) and a
one-off state dump (`--state <id> <clip_key>`) for spot-checking a snapshot
without opening the queue file.

Each queue packet carries `match_id`, `clip_key`, `clock`, `profile`, `why`,
`window`, `fps`, `fog_of_war`, the ordered `frames` (repo-root-relative JPEG
paths — a missing frame dir shows up as a `problem` field, not a crash), the
distilled `state` (or `null`), the matching `moment` from `moments.json`, a
`questions` list, and `answer_into` — the exact findings path to write to.

## The findings schema

Fixed — `build_review.py` reads it directly, so field names don't move:

```json
{
  "match_id": "NA1_5643462001",
  "reviewed_utc": "2026-09-18T00:00:00+00:00",
  "findings": {
    "559": {
      "clock": "9:37",
      "profile": "P1_camp_death",
      "verdict": "one sentence, the thing the stats could not settle",
      "observations": [
        {"frames": "f08-f12", "clock": "9:41", "saw": "what is literally visible"}
      ],
      "answers": [
        {"question": "Was the killer visible before they arrived?", "answer": "..."}
      ],
      "changes_stats_verdict": true,
      "confidence": "high",
      "unreadable": ["what the footage could not settle"]
    }
  }
}
```

Keyed by clip key (the clip's start second, same key as `clips.json`), not by
moment type or clock — a game can have two moments seconds apart in one
window, and the clip key is the one unambiguous handle. `--check` verifies:
the file parses; `match_id` matches its directory; every findings key is a
captured ok/partial clip; every finding has a non-empty `verdict` and at
least one observation; every observation has `frames`, `clock` and `saw`;
every `frames` spec (`f08` or `f08-f12`) names frames that exist on disk;
`confidence` is high/medium/low; `changes_stats_verdict` is a bool. It reports
every problem it finds, not just the first, and exits non-zero on any of
them.

## What the footage can answer

- **Killer visibility** — was the champion on screen or the minimap before
  the kill, or did they show up from fog.
- **Smite timing** — pressed early, late, contested and lost, or never
  pressed at all.
- **Who arrived when** — at a pit, at a fight, in what order, on which side.
- **Reaction vs. drift** — whether a dead minute was a response to something
  on screen or just movement with nothing happening.

The clips legibly carry the game clock, the minimap, floating health bars
over champions, the scoreboard, kill callouts, and the chat log — which
carries real signal: enemy ability callouts and the team's own pings are
often the actual explanation for a decision.

## What it cannot

- **No bottom HUD.** The player's own health bar and ability/summoner row do
  not render for an API-asserted camera selection (docs/PHASE2.md's "Player
  HUD not shown" finding) — that gap is exactly why the state snapshots
  exist alongside the frames. Don't expect to read the player's own HP or
  cooldowns off the video.
- **No HP or cooldowns in state, either.** The Live Client Data snapshot
  gives level, items, summoner spells, scores and dead/alive — never health
  or ability/summoner cooldowns for anyone. A finding that claims "smite was
  on cooldown" or "they were at 20% HP" from the state file alone is
  fabricating a value the schema does not carry; that has to come from what
  is visible in the frames (floating health bars, on-screen indicators), or
  be listed under `unreadable`.
