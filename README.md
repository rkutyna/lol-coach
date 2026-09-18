# lol-coach

Post-game League of Legends coaching for a single player, run by [Claude Code](https://claude.com/claude-code).

The design rests on one split:

> **Stats answer most questions for free. Video answers the few that need a screen.**

The Riot Match-V5 API gives exact events and once-a-minute positions, which is
enough to settle most of what a low-elo jungler does wrong. The League replay
client gives footage, but it is slow and fragile, so it is used sparingly — only
where seeing the moment actually changes the verdict.

Python does the data work. Claude does the coaching. A static page shows the result.

## Status

| Phase | What | State |
|---|---|---|
| 1 | Ingestion — fetch matches, detect coachable moments, compute stats | done |
| 2 | Capture — drive the replay client's Replay API to film those moments | done |
| 3 | Video review — read the frames, answer what the stats could not | done |
| 4 | Viewer — clips, timeline, minimap, progress dashboard | done |
| 5 | Polish — batch runner, prune job, patch-expiry warnings | not started |

## The pipeline

```bash
python tools/fetch_match.py --count 5    # Summoner's Rift only; skips ARAM/Swiftplay/remakes
python tools/find_moments.py             # -> moments.json per match
python tools/digest.py                   # -> stats.json per match + data/batch.json
python tools/capture_plan.py             # decide which moments are worth filming
python tools/capture.py --launch         # drive the replay client; -> clips + frames + state
python tools/video_review.py             # -> the packets Claude reads
python tools/build_review.py             # -> web/data.js
python3 -I tools/serve.py 8777           # viewer at http://127.0.0.1:8777
```

Claude writes the reviews in between: `data/reviews/<match_id>.md` per game, a
batch review for the set, and the video findings in
`data/matches/<id>/video_findings.json`.

## Setup

```bash
cp data/player.example.json data/player.json   # your Riot ID
echo 'RIOT_API_KEY=RGAPI-...' > .env           # developer.riotgames.com
```

A **development Riot API key expires every 24 hours**, so a 401 means the key
needs regenerating. `ffmpeg` is required for clip encoding. Capture needs League
installed on the same machine and the replay still openable — replays expire when
the patch changes, so capture must happen within ~2 weeks of a game.

## What this repo does not contain

Code and docs only. None of the game data is published, because every
`match.json`, `timeline.json` and replay state snapshot carries the **PUUIDs and
Riot IDs of all ten players in the game** — nine of whom did not agree to be in
anyone's repo. The tools recreate all of it from the API.

The tracked account's own Riot ID lives in `data/player.json`, which is
gitignored, and the written reviews stay local too.

## Things that cost real crashes to learn

The Replay API is undocumented in the places that matter. `docs/PHASE2.md` has
the full list with sources; these are the ones that bite:

- Only ever send `cameraMode` as `"top"` or `"path"`. `"fps"` and `"tps"` kill
  the client instantly, and `"path"` is *required* for camera moves to apply.
- Never let the playhead reach the replay's end — that takes the API down with it.
- Aim the camera after the seek and while playing. A paused replay's camera never
  moves, and both a render POST and a seek clear the selection.
- **Believe the files, not the client.** The API stops answering and drops
  connections exactly when a capture finishes. Every capture failure in this
  project was the driver disbelieving completed frames sitting on disk.
- `webm` capture fails on macOS in the audio encoder. PNG frames, then `ffmpeg`.
- Champion follow through the API does not work. Frame shots with
  `/replay/sequence` keyframes at known coordinates instead — Riot's own League
  Director doesn't attempt follow either.

## Things the data cannot tell you

Worth stating plainly, because the temptation is to coach past the evidence:

- **Timeline frames arrive once per minute.** Kill and objective positions are
  exact; anything finer than a minute about *position* is an inference. Every
  detected moment carries `state_as_of` and `state_staleness_s` for this reason.
- **`WARD_PLACED` has no position.** Ward timing and counts are knowable. Ward
  placement quality is not, so it doesn't get coached.
- **Attribution beats geometry.** Objective credit comes from
  `ELITE_MONSTER_KILL.killerId` and its assist list. Comparing an exact event
  against a stale position frame understated objective presence by 40% until
  that was fixed.
- **The player's own HUD does not render** for an API-asserted selection, so
  clips cannot show their health bar or ability cooldowns. That is why Phase 3
  also captures a state snapshot per moment.

## Layout

```
tools/   fetch_match, find_moments, digest, capture_plan, capture,
         replay_api, replay_launch, media, video_review, build_review, serve
docs/    SCOPE.md (design), PHASE2.md (replay API rules), PHASE3.md (video review)
web/     static viewer
data/    gitignored — matches, reviews, profile, patterns
```

## License

No license yet. This is a personal tool published so the replay-API findings and
the review format are useful to someone else; it is not affiliated with Riot Games.
