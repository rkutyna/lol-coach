---
name: league-review
description: Run the lol-coach post-game League of Legends coaching pipeline for one Iron Lillia jungler — fetch recent games from the Riot API, detect coaching moments, write per-game and batch reviews, capture replay clips, and build the review viewer. Use this whenever the user wants their League games reviewed, coached, or analyzed; mentions VOD review, replay capture, jungle coaching, or their last games; asks "how did I play", "review my games", "what should I work on"; or types /league-review. Also use it for any work inside the lol-coach repo, including changing the tools, the detection heuristics, or the capture driver.
---

# League review (lol-coach)

Post-game coaching for one player, built around a simple split: **stats answer most
questions for free; video answers the few that need a screen.** The Riot API gives
exact events and once-a-minute positions; the replay client gives footage but is
slow and fragile, so it is used sparingly and only where seeing the moment changes
the verdict.

Repo: the `lol-coach` checkout — work there, not in the parent
directory. Read its `CLAUDE.md` first; it holds the coaching rules and the data
caveats. `docs/SCOPE.md` has the design and `docs/PHASE2.md` the replay-API rules.

Player: **Iron, Lillia jungle** — the Riot ID lives in `data/player.json`,
which is gitignored. Current goals live in
`data/profile.md` and the running history in `data/patterns.md`. Read both before
coaching anything — the point is continuity across sessions, not a fresh opinion
each time.

## The pipeline

```bash
cd /path/to/lol-coach
python tools/fetch_match.py --count 5    # Summoner's Rift only; skips ARAM/Swiftplay/remakes; snapshots lobby ranks
python tools/find_moments.py             # -> moments.json per match
python tools/digest.py                   # -> stats.json per match (incl. lobby) + data/batch.json
python tools/build_review.py             # -> web/data.js
python3 -I tools/serve.py 8777           # viewer at http://127.0.0.1:8777
```

Then write the reviews: `data/reviews/<match_id>.md` per game and
`data/reviews/batch-<date>.md` for the set, and append new entries to
`data/patterns.md`. Re-run `build_review.py` afterwards so the viewer picks them up.

A **development Riot API key expires every 24h**, so a 401 means the key in `.env`
needs regenerating at developer.riotgames.com. Ask the user for a fresh one; never
put a key anywhere but `.env`.

## Writing a review

The reviews already in `data/reviews/` are the template — match their depth and
evidence standard rather than inventing a new format. What makes them work:

- **Coach the 1-2 active goals** from `profile.md`. Everything else gets a line
  under "also seen". A player at Iron cannot act on fifteen findings.
- **Every claim cites a match id and a game clock.** A review that can't be
  checked can't be trusted.
- **Fixes are physical**: "clear raptors then walk to dragon at 4:40", never "play
  better around objectives".
- **Judge a game against its lobby.** The player queues with Gold and Diamond friends,
  so opponents range from Iron to Diamond. Put the `lobby` summary from `stats.json` on
  a **Lobby** line under each review's header, and never read a loss to a Gold+ lobby
  as proof a habit failed. `python tools/ranks.py` backfills any match missing it.
- **Wins get reviewed like losses.** A 3/0/4 win with one objective taken has a
  lesson in it.
- **Promote a goal only when the batch numbers support it**, and record why in the
  goal-history table in `patterns.md`.

The richest fields, learned the hard way:

- `victimDamageReceived` on a death names every damage source. A neutral monster in
  that list means the fight was entered with a camp already eating health — six of
  ten deaths in the first batch looked like this.
- `currentGold` + `championStats.health` per frame reveals **resets**. Counting
  shopping trips that weren't right after a death ranked the first four games
  5/3/2/1, matching deaths 0/1/5/4 exactly.
- Timeline frames are **once per minute**; kill and objective positions are exact.
  Each moment carries `state_as_of` and `state_staleness_s` — check them before
  saying where anyone "was".
- **Attribution beats geometry.** Use `ELITE_MONSTER_KILL.killerId` and its assist
  list for objective credit. Distance-to-pit compares an exact event against a
  stale frame and understated objective presence by 40% until this was fixed.
- `WARD_PLACED` has **no position**, so ward timing and counts are knowable and
  placement quality is not. Don't coach what the data can't see.

## Capturing clips

Only worth it where footage changes a verdict — deaths, contested objectives, and
dead time. Gold swings, counter-jungle totals, item and skill orders, and
objectives the player is credited with are fully answered by the JSON.

```bash
python tools/capture_plan.py                     # decide what to film
python tools/capture.py --dry-run                # review the plan and what's done
python tools/capture.py --launch                 # capture everything outstanding
python tools/capture.py NA1_123 --only P1_camp_death --launch --limit 1
```

`--launch` downloads the replay if needed, opens it, captures, and closes it. The
driver closes its own replays on exit, crash, or Ctrl-C.

**Budget about 2.5 minutes per clip**, nearly all of it replay loading. Replays
also **expire when the patch changes**, so capture within ~2 weeks of a game;
`meta.json` carries `replay_capturable` and `rofl_patch()` checks a file offline.

### What to tell the user before a capture run

The client **freezes for roughly half a second per frame** while it writes them, so
21-35s per clip, and macOS may offer to force quit it. Then — on this machine —
**the client never answers again after a single capture**: it writes its frames and
hangs for good. That is not a crash and not a bug to fix; the driver relaunches the
replay for each clip because one clip per launch is the real limit.

This matters because a working capture and a hung client look identical. Tell the
user the estimate up front, and give them the check:

```bash
find /tmp/lol-coach-capture -name '*.png' | wc -l   # rising = working
pgrep -f tools/capture.py                            # nothing = orphan, kill the game
```

A frozen game with **no driver running** is an orphan from a crashed run — kill it.

### Rules that cost real crashes to learn

`docs/PHASE2.md` has the full list with sources; these are the ones that bite:

- Only ever send `cameraMode` as `"top"` or `"path"`. `"fps"`/`"tps"` kill the
  client instantly; `"path"` is *required* for camera moves to apply at all.
- Never let the playhead reach the replay's end — that takes the API down with it.
- Aim after the seek and while playing. A paused replay's camera never moves, and
  both a render POST and a seek clear the selection.
- Frame the shot with `/replay/sequence` keyframes at known coordinates (death
  positions, pit locations) with pitch 90. Champion follow through the API does not
  work — Riot's own League Director doesn't attempt it.
- **Believe the files, not the client.** The API stops answering and drops
  connections exactly when a capture finishes. Every capture failure in this
  project was the driver disbelieving completed frames sitting on disk.
- Don't touch the mouse during a run: it hands the camera to Manual Camera and the
  API is ignored from then on.

## Phase 3 — the video review (not built yet)

The remaining piece, and the reason the clips exist. Claude reads a clip's review
frames (`data/matches/<id>/frames/<start>/*.jpg`, already downscaled) plus its
state snapshot (`data/matches/<id>/state/<start>.json`, every player's exact HP,
items and spells at that moment) and answers what the stats could not:

- Was the killer visible before they arrived, or did they appear unseen?
- Was smite on cooldown, pressed early, or never pressed?
- Was a dead minute a reaction to something, or drifting?

The clips are legible enough for this: game clock, minimap, floating health bars,
the scoreboard, kill callouts, and the chat — which turned out to carry real
signal, including enemy ability callouts and the player's own team's pings. The
player's own bottom HUD does **not** render for an API-asserted selection, which is
why the state snapshots exist.

When building it, write findings into the per-game reviews next to the stats
evidence rather than as a separate video report — one verdict per moment, with both
kinds of evidence behind it.
