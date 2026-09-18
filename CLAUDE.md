# lol-coach

Post-game League coaching for one player: Iron, Lillia jungle. The account is
named in `data/player.json`, which is gitignored.
Python tools pull the data; Claude does the coaching; a static page shows it.

Read [docs/SCOPE.md](docs/SCOPE.md) for the design and the verified Replay API
findings before touching the capture path.

## Reviewing a batch

```bash
python tools/fetch_match.py --count 5     # Summoner's Rift games only
python tools/find_moments.py              # -> moments.json per match
python tools/digest.py                    # -> stats.json per match + batch.json
```

Then, for the review itself:

1. Read `data/profile.md` (the active goals) and `data/patterns.md` (history).
2. Read `data/batch.json` and each match's `moments.json`.
3. Write `data/reviews/<match_id>.md` per game, then `data/reviews/batch-<date>.md`.
4. Append new patterns to `data/patterns.md`; update `profile.md` only when a
   goal actually changes, and log why in the goal history table.

## Rules for reviews

- **Coach the active goals.** Note anything else in one line under "also seen".
- **Cite evidence.** Every claim names a match id and a game clock.
- **Say what to do instead**, concretely: a place to walk, a camp to take, a
  timer to respect. Never "play safer" or "improve macro".
- **Don't invent what the data doesn't show.** Ward *positions* are not in the
  API (only who placed what, and when), and player positions come once a
  minute. If a verdict needs to see the screen, say so and mark the moment for
  capture instead of guessing.
- **Wins are reviewed like losses.** The result is not the lesson.
- **Iron-level standards.** Compare against the targets in `profile.md`, not
  against high-elo play.

## Data facts worth remembering

- Timeline frames arrive **once per minute**; event positions (kills, objectives)
  are exact. Anything finer than a minute is an inference.
- `jungleMinionsKilled` counts **monsters, not camps** (~16 for a full clear).
- `victimDamageReceived` on a death names every damage source, which often
  explains a death on its own (e.g. taking void grub damage during a gank).
- `WARD_PLACED` has no position. Ward timing is knowable; ward placement is not.
- Replays expire when the patch changes, so capture must run within ~2 weeks of
  a game. `meta.json` carries `replay_capturable`.

## Layout

```
tools/      fetch_match.py, find_moments.py, digest.py, lolmap.py, riot.py
data/       profile.md, patterns.md, batch.json, matches/<id>/, reviews/
docs/       SCOPE.md
```

`.env` holds `RIOT_API_KEY` and is gitignored. Development keys expire every 24h;
a 401 means regenerate it at developer.riotgames.com.

## Viewer

```bash
python tools/build_review.py          # -> web/data.json + web/data.js
python3 -I tools/serve.py 8777        # then open http://127.0.0.1:8777
```

The page needs `data.js` (not `data.json`) because `fetch()` is blocked on
`file://`. Serve it rather than double-clicking when the app's own preview
server is used — macOS blocks that server from reading `~/Documents`.

## Capturing clips (Phase 2)

```bash
python tools/capture_plan.py              # decide what's worth filming
python tools/capture.py --dry-run         # review the plan + what's already done
python tools/capture.py NA1_5643506492 --launch --limit 1 --verify-camera
python tools/build_review.py              # clips appear in the viewer
```

Read [docs/PHASE2.md](docs/PHASE2.md) before changing anything that talks to the
replay client. The short version: only `cameraMode` `top`/`path` may ever be
sent (`fps`/`tps` kill the client, `path` is required for camera moves to
apply), never let the playhead reach the replay's end, aim the camera only
after seeking and while playing, and don't touch the mouse during a run.

### If a replay looks frozen

Capture freezes the client for ~0.5s per frame, so 21-35s per clip, and the
driver prints the estimate first. To tell a working capture from a stuck one,
don't judge by the window — count the frames:

```bash
find /tmp/lol-coach-capture -name '*.png' | wc -l   # rising = working
pgrep -f tools/capture.py                            # nothing = orphaned replay, kill the game
```

A frozen game with **no driver running** is an orphan from a crashed run; kill
it. The driver now closes its own replays on exit, crash, or Ctrl-C.
