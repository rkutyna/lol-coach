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

- **What the enemy team was saying.** See below — this one was a surprise.

The clips legibly carry the game clock, the minimap, floating health bars
over champions, the scoreboard, kill callouts, and the chat log.

### The replay shows *both* teams' pings (2026-09-18)

Not obvious and easy to miss: a replay is a spectator client, so the chat log
renders the **enemy team's** pings and callouts in red alongside your own.
That is information the player did not have during the game, and it can name
the cause of a death outright.

Found on NA1_5643483873 clip `474`, frame f26, at 8:06 — six seconds before
the player died at blue buff, the enemy mid laner pings **"Lillia - Alive"**
twice and **"signals that enemies are missing"** twice. The enemy team was
actively tracking the player's position and calling it out while she kept
clearing.

Use it for *diagnosis*, never as something the player should have known:
a finding may say "the enemy team had called her position six seconds
earlier", but the coaching that follows has to be about what was on **her**
screen — her own health bar, her own minimap.

## What it cannot

- **The bottom HUD renders in the first frame only.** Refining PHASE2.md's
  "Player HUD not shown": it *is* there in `f01` — champion portrait, exact
  HP and mana, level, ability ranks and both summoner spell icons with their
  cooldown numbers — and it is gone by `f03`. The API-asserted selection
  survives the seek just long enough to draw one frame, then clears (the same
  mechanism as PHASE2's "a render POST and a seek both clear the selection").
  So read the HUD from `f01` and expect nothing after it; the state snapshots
  still exist because one frame is not a time series.
  At this capture width the two summoner spell icons are **not reliably
  distinguishable from each other**, so a cooldown number can be read but not
  always attributed to Flash or to Smite. Verified on NA1_5643462001 clips
  375 and 540. Capturing one extra frame at the very start, before the
  selection clears, would make the HUD read more dependable.
- **No HP or cooldowns in state, either.** The Live Client Data snapshot
  gives level, items, summoner spells, scores and dead/alive — never health
  or ability/summoner cooldowns for anyone. A finding that claims "smite was
  on cooldown" or "they were at 20% HP" from the state file alone is
  fabricating a value the schema does not carry; that has to come from what
  is visible in the frames (floating health bars, on-screen indicators), or
  be listed under `unreadable`.
