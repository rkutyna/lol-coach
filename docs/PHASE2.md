# Phase 2 — capture

How to drive the Replay API without crashing the client, based on reading six
community projects plus Riot's own League Director, and on four crashes here.

## Rules (violating these is what broke things)

1. **Only ever send `cameraMode` as `"top"` or `"path"`.** `"tps"` killed the
   client here instantly and `"fps"` is recorded killing another project's.
   But `"path"` is **required** for camera moves to take effect at all —
   without it the API accepts a position, echoes it back, and the game ignores
   it ([developer-relations#877](https://github.com/RiotGames/developer-relations/issues/877),
   fix confirmed by the reporter). `top -> path` also unsticks a frozen camera,
   and `y: 0` is rejected as invalid. `replay_api.py` allows only those two
   values and switches to `path` before any sequence.
2. **Never let the playhead reach the replay's end** — that closes the replay
   and takes the API down with it. Stop ~10s short (`END_MARGIN_S`).
3. **Aim after the seek, while playing.** A paused replay's camera does not move
   at all, a render POST clears the selection and snaps the camera to a fixed
   spot, and seeking clears the selection too. The frozen `(300, 1912, -770)`
   seen here was that snap — the map's bottom-left corner, not a bad transform.
4. **Send critical render fields in their own small POSTs**, with ~0.35s
   between them. A bulk render silently drops `selectionName`/`cameraAttached`.
5. **Never trust selection readback.** The client echoes a name it isn't
   holding. The only proof of a real follow is `cameraPosition` changing over
   time (`camera_is_moving()`).
6. **`selectionName` wants the internal champion name** — `Lillia`,
   `MissFortune` (no spaces). Get it from `/liveclientdata/playerlist`'s
   `rawChampionName`. A summoner name may be accepted as a string and resolve
   to nothing, which is what happened here when a summoner name was sent.
7. **PNG only.** `webm` fails here in the audio encoder
   (`Video encoding error = Audio Encoding`) and the `Recording` schema has no
   audio field to turn it off. **Our exact error string appears nowhere
   public** — a thorough search of Riot's tracker, GitHub and the web found no
   other report of it, so treat it as our own finding, not a documented bug.
   What *is* documented is that webm's audio and video legs fail independently
   and often: [#881](https://github.com/RiotGames/developer-relations/issues/881)
   (A/V desync, still open) and
   [#1091](https://github.com/RiotGames/developer-relations/issues/1091) (audio
   silently dropped for a patch, then a 1080p→720p regression). PNG plus our
   own ffmpeg mux avoids that whole surface.
8. **One retry per call**, generous timeouts. The client drops the odd request
   while seeking, and stops answering entirely for tens of seconds while
   writing frames — a timeout is not a crash, so check for the process.
9. **Don't touch the mouse during a run.** Moving it flips the client to Manual
   Camera and the API is ignored from then on.
10. **"Done" means the frame count held still for several polls.** A file being
    written plateaus between chunks; trusting one poll produced a 6.3s clip
    from a 15s window in another project.

## Aiming: sequences, not follow

Champion follow through the API is unreliable. Riot's own League Director never
aims at champions at all — it only sets absolute camera positions and relies on
the user clicking a champion. The one project that shipped a `--follow` flag made
it opt-in after a real run produced "twenty pictures of the same rock."

So this project aims with **`/replay/sequence`**, which keyframes the camera
across game time and is authoritative — unlike a render, it isn't undone by the
camera snap. We already know every coordinate we need: `CHAMPION_KILL` events
carry exact death positions, and objective pits are fixed.

- Positions are world coordinates: **`x` and `z` are the ground plane, `y` is
  altitude**. Rotations are degrees, (yaw, pitch, roll).
- Keyframes are `{"time": <game seconds>, "value": {...}, "blend": "linear"}`.
- `POST /replay/sequence {}` clears the sequence.
- For a hard cut, use `blend: "snap"` and offset that keyframe's time by a
  fraction of a second, or the client interpolates oddly.

**Selection is still asserted, but only for the HUD** — it reliably puts that
champion's health bar and ability row on screen, which the smite and health
questions need, even when it does nothing for framing.

What each planned clip aims at now: deaths park on the exact death coordinate,
smite contests park on the pit, dead-time clips keyframe between the two known
frame positions. If framing turns out poor, the next refinement is a start
keyframe at the player's last known frame position so the camera covers the
approach.

## Capture recipe

```
seek(start - WARMUP_S)        # park early; don't seek right before recording
set_hud(fog=..., minimap=...) # HUD and fog first
play()                        # camera only moves while playing
select_champion("Lillia")     # separate POSTs, for the HUD
set_sequence(keyframes)       # authoritative framing
record_frames(dir, start, end, fps)   # play() first, then POST recording
wait_for_recording(dir)       # tolerate silence; require a stable frame count
media.process(...)            # PNG -> mp4 + review JPEGs, delete PNGs
```

Recording body that works: `codec: "png"` with `path` as a **folder**,
`enforceFrameRate: true`, `replaySpeed: 1`. Width and height are ignored — the
spec itself says output tracks the game window — so keep League windowed and small.

**`enforceFrameRate` deserves a note**, because the advice elsewhere is the
opposite. For **webm** it must be false: it makes the client drop frames and
then tag the container at the requested rate anyway, so nine seconds of game
came back as a 3.5-second video playing triple speed, and Riot's own
CreatorSuite author names it as the cause of the #881 desync. For **PNG
sequences it should be true**, because there is no container to mislabel — we
set the frame rate ourselves at mux time, so what we need is every frame
present and evenly spaced. With it false, the client drops frames and our
fixed-rate mux would silently distort time. The cost is that capture runs
slower than realtime.

## Launching and closing replays (solved)

`open file.rofl` is a dead end on macOS — nothing claims the extension. The
answer is a **hybrid**, in `tools/replay_launch.py`:

- **The LCU handles availability.** Auth comes from the lockfile at
  `<install>/lockfile`, format `name:pid:port:password:protocol`, basic auth
  with the literal username `riot`. Read the lockfile rather than scraping
  `ps`: the *Riot* client also advertises `--app-port`/`--remoting-auth-token`
  on a different port with a different API, and mixing them gives 401s.
  Riot's own `riotgames.pem` fails to verify under OpenSSL 3, so loopback
  traffic goes unverified.
- **Direct process launch handles running it.** The game binary plays a `.rofl`
  standalone with the same arguments the client uses (`-UseMetal=1:1` on this
  Mac). With no remoting args it initialises LCU remoting on port 0 and logs a
  connection error every 60s — cosmetic; the replay plays for as long as you
  like. This also hands us a PID we own.
- **Teardown is SIGTERM, 10s, SIGKILL.** A directly-launched replay never
  connects to the client, so there is no client state to corrupt. Never use the
  LCU's `/process-control/v1/process/quit` — that quits the *client*.

Replay state machine (`/lol-replays/v1/metadata/{gameId}`):
`checking, found, watch, download, downloading, incompatible, missingOrExpired,
retryDownload, lost, unsupported, error`. Traps, all verified against a live
client:

- **404 means "no local metadata yet", not "no replay"** — fix with
  `POST /lol-replays/v2/metadata/{gameId}/create`.
- **POST bodies are required and must be JSON objects**; no body gives a 400.
- **`download` and `watch` return 204 even for a nonexistent game**, and a
  doomed download never reaches an error state. Poll metadata with a timeout;
  ask for the download once.
- **`downloadProgress` is uninitialised garbage** unless the state is
  `downloading`.
- **There is no replay phase in `gameflow-phase`.** Use
  `GET /lol-gameflow/v1/watch`: `None | WatchStarted | WatchInProgress |
  WatchFailedToLaunch`.

**Timings, measured across five replay sessions here:** the Replay API answers
at 9–11s, but the game isn't seekable until the first gameloop at 12–14s. So
wait for `/replay/playback` to return JSON, *then* a few seconds more.

**Patch checks are free and offline.** A ROFL2 file (patch 14.11+) carries its
version in the header: a length byte at `0x0E`, then the ASCII version. Public
docs describe the older v1 layout and don't match. `rofl_patch()` reads it with
no client running, so an expired replay is caught before launching. The file's
trailing metadata also holds a `statsJson` blob with full end-of-game stats.

## Answered by the first reviewed clips (2026-09-18)

- **Does a parked sequence actually frame the action?** *Sometimes.* Parking on a
  pit works: NA1_5643462001 clips `375` (dragon) and `540` (grubs) frame the whole
  contest cleanly, because the fight stays in the pit. Parking on a **death
  coordinate** does not: clip `559` points at a rock face for most of its 21
  seconds, because the fight that produced the death moved before it ended. The
  approach that killed the player is off-screen, so the one question the clip
  existed to answer — was the killer visible first — is unanswerable from it.
  Death clips want the camera on where the player *was*, not where they fell.
- **Does asserting a selection show the player's HUD here?** *Yes, for exactly one
  frame.* `f01` carries the full bottom HUD — portrait, HP and mana numbers, level,
  ability ranks, and both summoner spell icons with cooldown numbers — and it is
  gone by `f03`. The selection survives the seek long enough to draw one frame and
  then clears. At the current capture width the two summoner icons cannot be told
  apart reliably, so a cooldown number is readable but not always attributable.

## Still open
- The download transitions and a scripted launch are **written but unrun** —
  the research verified every read-only LCU call against the live client, but
  triggering a real download and launch was left for a supervised run.

## Where our evidence is first-hand

A second, independent search found **no public report** of: our `Audio
Encoding` error string, the `:2999` server going unresponsive during a write,
`cameraMode` crashing a client (outside the one project log above), or anyone
using PNG to dodge an encoding failure. We observed the first three directly on
this machine, repeatedly. So these are our own findings — reproducible here,
but not corroborated elsewhere, and worth re-testing after a patch.

Also worth correcting: League Director's README says "Windows Only", but that
line predates its own merged macOS support
([PR #17](https://github.com/RiotGames/leaguedirector/pull/17), 2019) and was
never updated. `enable.py` still ships macOS install detection. The README line
is stale rather than a statement that the API can't work here — which matches
what we found, since everything except webm works on this Mac.

## Sources

Riot's League Director (`leaguedirector/api.py`, `app.py`, `sequencer.py`) and
the LeagueDirectorNG fork; `AlsoSylv/Irelia`'s dump of the game's own OpenAPI
spec; `skilledDev96/League-team-comp`'s engineering log (the most useful single
document — measured numbers and a record of what they gave up on);
`fletch-spec/lol-path-mapper`; `iamacoffeepot/scry`; `pyLoL`;
`SimonKmr/LolCameraSequenceImporter`; Riot issue `developer-relations#881` for
the webm audio bug.

## Lessons from the first real captures

Every capture attempt so far actually produced its frames. Every failure was in
the code around the capture, and all three had the same root cause: **trusting
the client's word over the files on disk.**

1. **Completion comes from the frames.** The API routinely stops answering at
   the exact moment a capture ends, so waiting for it to confirm means sitting
   through the whole timeout with a finished clip on disk. `wait_for_recording`
   takes an `expected` frame count and returns as soon as it is reached.
2. **Post-capture cleanup must be best-effort.** `pause()` and
   `clear_sequence()` are called while the client is still flushing frames, and
   they time out. Wrapped in `quietly()`; a failed pause must never lose a clip.
3. **Busy is not dead.** `ReplayBusy` now rescues frames from disk rather than
   failing, and `ReplayGone` does the same before marking anything failed.
4. **A good clip can never be overwritten** by a later failed attempt, and
   clips are matched to moments by the window they cover, not by filename — so
   tuning a profile's window doesn't orphan footage already captured.
5. **`RemoteDisconnected` is a `ConnectionResetError`, not a `URLError`.** It
   escaped the handler entirely and crashed the driver twice.

## The freeze is the mechanism, not a fault

While capturing, the client stops responding for roughly **half a second per
frame** at 1280x720, and macOS may offer to force quit it. That is
`enforceFrameRate` deliberately slowing the game so no frame is dropped, plus
the client blocking on PNG writes. It cannot be removed, only shortened:

- Fewer frames. Deaths run at 2fps over a 21s window (~21s of freeze) rather
  than 4fps over 28s (~56s).
- A smaller game window, since capture resolution follows it.

The driver prints the estimate before each clip and says not to force quit.
