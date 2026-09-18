# lol-coach — scope

Post-game VOD + stats coaching for one player, reviewed in batches by Claude Code.

- **Player:** one Iron Lillia jungler on NA1. The Riot ID lives in `data/player.json`, which is gitignored.
- **Goal:** build the habits that matter at Iron — not challenger polish. Claude keeps 1–2 active goals per batch and only promotes when the numbers hold across games.
- **Machine:** macOS (M3 Pro, 19 GB RAM), League 16.18, Claude Code and League on the same Mac.
- **Non-goals:** no live/in-game advice (never during a game), no other roles or champs until the Lillia loop works, no accounts but this one.

## Phase 0 findings (verified 2026-09-17)

Replay API enabled via `EnableReplayApi=1` in `/Applications/League of Legends.app/Contents/LoL/Config/game.cfg`
(CRLF line endings; backup at `game.cfg.bak-20260916`).

| Capability | Status | Notes |
|---|---|---|
| Replay API on macOS | ✅ works | `https://127.0.0.1:2999`, self-signed cert (`curl -k`) |
| Writes (POST) | ✅ works | **must send `Accept: application/json`** or 406 `BAD_RESPONSE_FORMAT` |
| Seek to game time | ✅ works | `POST /replay/playback {"time": 600, "paused": true}`; `seeking` flag clears when ready |
| Camera follow | ✅ works | `POST /replay/render {"cameraAttached": true, "selectionName": "<name>"}` — see Phase 2: this wants the **champion** name |
| Fog of war | ✅ settable | `{"fogOfWar": true}` — critical: clips must show only what our team could see |
| HUD toggles | ✅ settable | `interfaceAll`, `interfaceMinimap`, `interfaceScoreboard`, `interfaceTimeline`, … |
| PNG frame capture | ✅ works | `POST /replay/recording {"codec": "png", ...}` → numbered frames in an auto-created dir `<patch>_<matchid>_NN/` |
| webm capture | ❌ broken on Mac | game log: `Video encoding error = Audio Encoding` → aborts and deletes the `.tmp`; crashed the client once |
| Capture resolution | ⚠️ not settable | `width`/`height` in the request are ignored; output matches the game window (3024×1890 retina here). Run League windowed ~1600×1000 to cut load, and downscale with ffmpeg |
| Recording needs playback | ⚠️ gotcha | a recording makes no progress while paused — unpause via API right after starting it |
| `enforceFrameRate` | ⚠️ slows playback | guarantees every frame; capture takes longer than real time |
| Live Client Data in replay | ✅ works | `/liveclientdata/allgamedata` returns all 10 players' items, scores, runes and events **at the currently seeked moment** — far richer than 1/min timeline frames |
| Riot Match-V5 | ❌ blocked | the supplied dev key returns 401 `Unknown apikey` — needs a fresh key |

**Decided capture path:** seek → aim camera → capture PNG frames → `ffmpeg` into a silent mp4 + a downscaled frame set for Claude.

## Phase 2 findings (2026-09-17)

Capture mechanics work; camera aiming does not, yet.

| Finding | Detail |
|---|---|
| PNG capture at 720p | ✅ Captured all 72 frames of an exact 12s window, started precisely on the requested game clock. 135 MB of PNG → **1.4 MB mp4**, frames fully legible |
| Window size matters | Windowed 1280x720 gives ~2.8 MB per PNG frame; retina fullscreen was 3024x1890. Requested width/height are ignored, so the game window *is* the capture size |
| API goes quiet mid-capture | Unresponsive for ~45s while writing frames at 6 fps. **A timeout is not a crash** — check for the game process to tell them apart |
| Directed camera hijacks playback | With `[Replay] EnableDirectedCamera=1`, resuming playback **clears `selectionName`** and the auto-director takes the camera. Set it to `0` in `game.cfg` (done); selection then persists |
| `cameraMode` is a landmine | Setting it to `"tps"` killed the client instantly. `replay_api.py` refuses the field outright |
| Camera follow: **unsolved** | `cameraAttached: true` + `selectionName: "<summoner name>"` + `selectionOffset` is accepted, but `cameraPosition` reads a fixed (300, −770) and never tracks the champion. `"focus"` mode gave (−1232, −214). Either `cameraPosition` is in a different space than world coordinates, or attachment needs something else. Under research |
| Player HUD: one frame only | ⚠️ Partly solved. The bottom HUD (health, mana, level, ability ranks, summoner spells with cooldowns) renders in the **first captured frame** and is gone by the third — the asserted selection survives the seek just long enough to draw once. Read it from `f01`. See docs/PHASE3.md |
| Replay launching | `open file.rofl` fails on macOS — nothing claims the extension. The LCU API (`/lol-replays/v1/...`) is the likely route. Under research |
| Crashes so far | 4, all during or right after capture. Retry, crash detection and resume are mandatory, not nice-to-have |

**Clip windows, fog of war and HUD per moment type** live in `tools/capture_plan.py`
(`PROFILES`). Fog ON means "render only what our team could see" for information
questions; fog OFF means "show what really happened" for mechanics and
who-arrived questions. Current batch plans 14 clips over 3 games, ~460s of
footage, peak ~314 MB of PNGs at any one time.

**Free before any capture:** `/liveclientdata/allgamedata` works inside a replay
and returns all ten players' items, scores and exact HP *at the seeked instant*.
Poll it at `t−10` and `t` for every moment; it may answer the smite and health
questions with no frames at all.

## Architecture

Python CLI tools write files; Claude Code reads them, writes the review; a static page renders it.

```
lol-coach/
  tools/            # python CLI, each does one thing
    fetch_match.py      # Match-V5 + timeline -> data/matches/<id>/
    find_moments.py     # timeline + heuristics -> moments.json
    capture.py          # drives Replay API -> clips + frames
    build_review.py     # review.json + assets -> web/
  data/
    matches/<match_id>/ # match.json, timeline.json, moments.json, state/, clips/, frames/
    profile.md          # role, champs, rank, current goals
    patterns.md         # recurring mistakes, what's been mastered
    reviews/<id>.md     # Claude's per-game review
  web/                  # static viewer (plain HTML/JS)
  docs/SCOPE.md
```

**Pipeline per batch**

1. `fetch_match` — pull recent ranked games for the puuid, store match + timeline JSON.
2. `find_moments` — rank candidate moments by heuristics (below), cap at ~12 per game.
3. `capture` — for each moment: seek, set fog of war to our team, follow the tracked player (or a named target), capture PNGs from `t-8s` to `t+4s`; stitch mp4; emit frames at 2 fps downscaled to ~1280px. Also poll `/liveclientdata/allgamedata` at each moment for exact state.
4. **Claude reviews** — reads `moments.json`, the state snapshots and frames; writes `reviews/<id>.md` and appends to `patterns.md`.
5. `build_review` — renders review + clips into `web/`.

Steps 1–3 must run while the patch is current (replays stop opening after a patch). Step 4 can happen any time after.

## Moment detection (jungle-specific)

From the timeline, scored and capped:

- **Deaths** — all of them, with 8s of lead-in. Highest priority at Iron.
- **Objective windows** — 60s before each dragon/grub/herald/baron spawn: where were we, and where was the enemy jungler?
- **Clear tempo** — first clear finish time, gaps where jungle CS stalls with no kill or objective nearby.
- **Gank attempts** — our position in a lane with no kill following (a wasted trip).
- **Counter-jungle** — enemy jungler in our jungle, and whether we reacted.
- **Vision** — ward placements and their timing vs. objective spawns.
- **Gold swings** — any 90s window where the team gold delta moves by more than ~1.5k.

## Coaching model

`profile.md` holds the skill ladder and the 1–2 active goals. `patterns.md` is the running record: recurring mistakes, and the habits already mastered.

Ladder for Iron Lillia jungle, in order:
1. **Don't die for nothing** — avoidable deaths, what the minimap showed, whether a ward existed.
2. **Clear and tempo** — a full clear on time, farming instead of forcing plays, recall timing.
3. **Objective timers** — be near dragon/grubs before they spawn, not after.
4. **Gank selection and enemy tracking** — only gank where the wave and enemy position allow; guess the enemy jungler's position.
5. **Vision and map reading** — ward before objectives, act on what the minimap shows.

Claude promotes a goal only when a batch's stats support it, and records that decision in `patterns.md`.

## Frontend

Static page, opened locally, reading `review.json`:

- **Clips + commentary** — each moment: mp4 beside what happened, why, and what to do instead.
- **Game timeline** — gold/XP/CS curves with moments marked; click to jump to a clip.
- **Minimap replay** — per-minute positions and event locations drawn on a map image.
- **Progress dashboard** — trends across batches against the active ladder goals.

## Storage

PNG frames at retina resolution are large (~160 MB for a few seconds). Rules:

- Keep: mp4 clips, downscaled frames Claude actually looked at, all JSON.
- Delete: raw PNG sequences, immediately after stitching.
- Cap: ~200 MB per game. Clips older than ~20 games get pruned; reviews and JSON are kept forever.

## Build phases

- **Phase 1 — ingestion.** `fetch_match` + `find_moments`, plus a SQLite or JSON store. Verifiable without video.
- **Phase 2 — capture.** `capture.py` driving the Replay API, including replay launch, seek-settle waits, fog of war, PNG→mp4, cleanup. The riskiest part; the Mac client has already crashed once under capture.
- **Phase 3 — review loop.** Claude reads a batch, writes reviews, maintains `profile.md`/`patterns.md`.
- **Phase 4 — viewer.** The four-part page.
- **Phase 5 — polish.** Batch runner, prune job, patch-expiry warnings.

## Open risks

- **Client stability under capture.** Needs retry, crash detection and resume. Windowed mode and fewer frames per moment reduce the load.
- **Patch expiry.** Ingestion must run within ~2 weeks of a game. A warning when a stored match can no longer be captured.
- **Replay launch automation.** Launching a `.rofl` can be scripted (`open` on the file in `~/Documents/League of Legends/Replays`), but downloading a replay may still need a click in the client. To be tested in Phase 2.
- **Frame budget.** ~12 moments × ~25 frames per game is a lot of images. Start with fewer frames per moment and raise it only where it changes the verdict.
