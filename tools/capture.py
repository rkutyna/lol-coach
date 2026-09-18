#!/usr/bin/env python3
"""Capture the planned clips from a running replay.

  python tools/capture.py --dry-run              # show what would happen
  python tools/capture.py NA1_5643506492         # capture one match
  python tools/capture.py NA1_5643506492 --only P1_camp_death --limit 1

  python tools/capture.py NA1_5643506492 --launch # open and close the replay myself

With --launch, the replay is downloaded if needed (via the League client) and
started directly, then closed afterwards. Without it, a replay must already be
open. Already-captured clips are skipped, so a crash is resumable — and with
--launch the driver reopens the replay and carries on by itself.

Order matters and is not arbitrary: seek, then HUD, then play, then aim. A
paused replay's camera never moves, a render clears the selection, and seeking
clears it too. See docs/PHASE2.md for why each step is where it is.
"""

from __future__ import annotations

import argparse
import atexit
import json
import signal
import subprocess
import sys
import time
from pathlib import Path

import media
import replay_api
import replay_launch
from replay_api import Replay, ReplayBusy, ReplayGone
from riot import DATA


# Every replay this process starts, so it can always be closed again. Without
# this, a crash or a Ctrl-C leaves a replay running with nothing driving it —
# which looks exactly like a capture frozen forever.
_LAUNCHED: list[subprocess.Popen] = []


def close_everything() -> None:
    while _LAUNCHED:
        proc = _LAUNCHED.pop()
        try:
            replay_launch.teardown(proc)
        except Exception:
            pass


def _on_signal(signum, _frame):
    print("\n   closing the replay …")
    close_everything()
    raise SystemExit(130)


atexit.register(close_everything)
for _sig in (signal.SIGINT, signal.SIGTERM, signal.SIGHUP):
    signal.signal(_sig, _on_signal)


def load_plan() -> dict:
    f = DATA / "capture_plan.json"
    if not f.exists():
        sys.exit("no capture plan — run tools/capture_plan.py first")
    return json.loads(f.read_text())


def manifest_path(match_id: str) -> Path:
    return DATA / "matches" / match_id / "clips.json"


def load_manifest(match_id: str) -> dict:
    f = manifest_path(match_id)
    return json.loads(f.read_text()) if f.exists() else {}


def save_manifest(match_id: str, data: dict) -> None:
    manifest_path(match_id).write_text(json.dumps(data, indent=2))


def check_replay_matches(api: Replay, match_id: str) -> tuple[bool, str]:
    """Confirm the open replay is the match we planned for.

    There's no match id in the Replay API, so compare the roster and duration
    against what we fetched. Capturing the wrong game would be silent otherwise.
    """
    meta = json.loads((DATA / "matches" / match_id / "meta.json").read_text())
    match = json.loads((DATA / "matches" / match_id / "match.json").read_text())
    want = {p["championName"] for p in match["info"]["participants"]}

    try:
        roster = api.roster()
    except Exception as e:
        return False, f"could not read the roster ({e})"
    have = {r["champion"] for r in roster if r["champion"]}
    if have and len(want & have) < 8:
        return False, f"different game is open (champions {sorted(have)[:4]}…)"

    length = api.length()
    if length and abs(length - meta["duration_s"]) > 90:
        return False, (f"open replay is {length / 60:.0f} min, "
                       f'expected {meta["duration_s"] / 60:.0f} min')
    return True, f'{len(want & have)}/10 champions match, {length / 60:.0f} min'


def selection_name(api: Replay, champion: str) -> str:
    """The internal champion name the client will accept as a selection."""
    try:
        for r in api.roster():
            if r["champion"].lower() == champion.lower() and r["selection"]:
                return r["selection"]
    except Exception:
        pass
    return champion.replace(" ", "").replace("'", "")


# Camera pitch in degrees. 90 looks straight down, so the ground point in the
# middle of frame is exactly the camera's x/z. Any less and the view lands
# altitude/tan(pitch) away — at pitch 65 and altitude 1500 that was ~700 units,
# enough to put a death just off screen.
PITCH = 90.0


def expected_frames(job: dict, end_s: float) -> int:
    return int((end_s - job["start_s"]) * job["fps"])


def tmp_dir_for(scratch: Path, job: dict) -> Path:
    return scratch / job["name"]


def quietly(fn, *args) -> None:
    """Run a cleanup call that is allowed to fail.

    Straight after a capture the client is still writing frames and will time
    out or drop the connection. The clip is already on disk by then, so a
    failed pause or sequence-clear must not lose it.
    """
    try:
        fn(*args)
    except (ReplayBusy, ReplayGone, RuntimeError):
        pass


def covering_clip(done: dict, job: dict) -> str | None:
    """An existing good clip that already contains this job's moment.

    Windows change as profiles are tuned, so matching on the moment rather
    than the filename avoids re-shooting footage we already have.
    """
    anchor = job.get("anchor_s", job["start_s"])
    for key, entry in done.items():
        if entry.get("status") != "ok":
            continue
        start, end = entry.get("window", [None, None])
        if start is not None and start - 1 <= anchor <= end + 1:
            return key
    return None


def salvage(tmp: Path, job: dict, out_root: Path, end_s: float,
            note: str | None = None) -> dict:
    """Convert whatever frames exist, even if the client died mid-run."""
    want = expected_frames(job, end_s)
    have = len(list(tmp.rglob("*.png")))
    if not have:
        return {"status": "failed", "reason": "no frames were written"}

    mp4 = out_root / "clips" / f'{job["start_s"]}.mp4'
    frames_dir = out_root / "frames" / str(job["start_s"])
    try:
        info = media.process(tmp, mp4, frames_dir, fps=job["fps"])
    except RuntimeError as e:
        return {"status": "failed", "reason": str(e)}

    complete = have >= want * 0.95
    out = {
        "status": "ok" if complete else "partial",
        "clip": str(mp4.relative_to(DATA)),
        "review_frames": info["review_frames"],
        "frames_captured": info["frames"],
        "frames_expected": want,
        "mp4_bytes": info["bytes"],
        "profile": job["profile"],
        "clock": job["clock"],
        "why": job["why"],
        "window": [job["start_s"], end_s],
        "fps": job["fps"],
        "fog_of_war": job["fog_of_war"],
    }
    if note:
        out["note"] = note
    return out


def capture_job(api: Replay, job: dict, out_root: Path, scratch: Path,
                verify_camera: bool = False) -> dict:
    """Capture one clip.

    A client death is only fatal if the frames are incomplete: the capture
    itself often finishes and the client dies immediately afterwards, and those
    frames are perfectly good.
    """
    start, end = job["start_s"], api.safe_end(job["end_s"])
    if end <= start + 1:
        return {"status": "skipped", "reason": "window falls inside the end margin"}

    cam = job["camera"]
    hud = job["hud"]
    tmp = scratch / job["name"]
    tmp.mkdir(parents=True, exist_ok=True)

    # 1. Park early. Never seek right before recording.
    api.wait_responsive()
    api.seek(max(0, start - replay_api.WARMUP_S))

    # 2. HUD and fog before the camera, since a render resets the selection.
    api.set_hud(minimap=hud.get("minimap", True),
                scoreboard=hud.get("scoreboard", False),
                fog_of_war=job["fog_of_war"])
    time.sleep(replay_api.SETTLE_S)

    # 3. Play: the camera does not move while paused.
    api.play()
    time.sleep(1.0)

    # 4. Selection, for the player's own HUD (health, abilities, smite).
    api.select_champion(selection_name(api, cam["hud_selection"]))

    # 5. Framing, from coordinates the timeline already gave us.
    if cam["keyframes"]:
        keys, rots = [], []
        for k in cam["keyframes"]:
            keys.append({"time": k["time"],
                         "value": {"x": k["x"], "y": k["altitude"], "z": k["z"]},
                         "blend": "linear"})
            rots.append({"time": k["time"],
                         "value": {"x": 0.0, "y": PITCH, "z": 0.0},
                         "blend": "linear"})
        api.set_sequence(keys, rots)
        time.sleep(replay_api.SETTLE_S)

    moving = api.camera_is_moving() if verify_camera else None

    # The player's own HUD does not render for an API-asserted selection, so
    # capture the state as data instead of hoping to read it off the screen.
    state = None
    try:
        state = api.live_state()
    except Exception:
        pass
    if state:
        state_dir = out_root / "state"
        state_dir.mkdir(parents=True, exist_ok=True)
        (state_dir / f'{job["start_s"]}.json').write_text(json.dumps(state))

    # 6. Record. Playback is started inside record_frames, as League Director does.
    want = expected_frames(job, end)
    print(f"\n      capturing {want} frames (~{round(want * 0.5)}s). The game will "
          f"stop responding while it writes them — macOS may even offer to force "
          f"quit it. That is normal: let it finish.", flush=True)

    def tick(count: int) -> None:
        print(f"\r      frames {count}/{want}", end="", flush=True)

    api.record_frames(str(tmp), start, end, fps=job["fps"])
    try:
        finished = api.wait_for_recording(out_dir=str(tmp), progress=tick,
                                          expected=want)
    except ReplayGone:
        # The client died. If the frames are all there, the clip is fine.
        result = salvage(tmp, job, out_root, end,
                         note="client died at the end of the capture")
        if result["status"] == "ok":
            result["camera_moving"] = moving
            return result
        raise

    quietly(api.pause)
    if cam["keyframes"]:
        quietly(api.clear_sequence)

    if not finished:
        api.stop_recording()
        return {"status": "timeout", "reason": "recording never reported done"}

    print()
    result = salvage(tmp, job, out_root, end)
    result["camera_moving"] = moving
    return result


def open_replay(match_id: str) -> subprocess.Popen | None:
    """Launch the replay for a match, downloading it first if the client is up.

    Returns the process so it can be torn down, or None if the replay is
    already playable and a launch isn't needed.
    """
    meta = json.loads((DATA / "matches" / match_id / "meta.json").read_text())
    platform, game_id = match_id.split("_", 1)
    rofl = replay_launch.replay_file(platform, game_id)

    if not rofl.exists():
        # Only the client can fetch a replay we don't have.
        try:
            lcu = replay_launch.Lcu()
        except replay_launch.LcuError as e:
            raise SystemExit(
                f"{rofl.name} is not downloaded and {e}. Open the League client "
                "(or download the replay from Match History) and re-run.")
        match = json.loads((DATA / "matches" / match_id / "match.json").read_text())
        info = match["info"]
        print(f"   downloading replay {game_id} …", flush=True)
        replay_launch.ensure_downloaded(lcu, game_id, {
            "gameVersion": info["gameVersion"],
            "gameType": info.get("gameType", "MATCHED_GAME"),
            "queueId": info["queueId"],
            "gameCreation": info["gameStartTimestamp"],
            "gameDuration": info["gameDuration"],
        })
        lcu.scan()

    # Patch check before launching: a stale replay simply won't open.
    patch = replay_launch.rofl_patch(rofl)
    if replay_launch.patch_series(patch) != replay_launch.patch_series(meta["patch"]):
        print(f'   replay is patch {patch}, match was {meta["patch"]} — may not open')

    print(f"   launching {rofl.name} …", flush=True)
    proc = replay_launch.launch(rofl, platform_id=platform)
    _LAUNCHED.append(proc)
    try:
        replay_launch.wait_ready(proc)
    except Exception:
        close_everything()          # never leave a half-loaded replay behind
        raise
    print("   replay ready")
    return proc


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("match_ids", nargs="*", help="default: whatever the plan holds")
    ap.add_argument("--dry-run", action="store_true")
    ap.add_argument("--only", help="capture a single profile, e.g. P1_camp_death")
    ap.add_argument("--limit", type=int, help="stop after N clips")
    ap.add_argument("--redo", action="store_true", help="recapture existing clips")
    ap.add_argument("--verify-camera", action="store_true",
                    help="check the camera actually moves (costs ~2s per clip)")
    ap.add_argument("--launch", action="store_true",
                    help="open each match's replay myself, and close it afterwards")
    args = ap.parse_args()

    plan = load_plan()
    plans = [p for p in plan["plans"]
             if not args.match_ids or p["match_id"] in args.match_ids]
    if not plans:
        sys.exit("no matching plans")

    if args.dry_run:
        for p in plans:
            done = load_manifest(p["match_id"])
            print(f'{p["match_id"]}  ({"capturable" if p["replay_capturable"] else "EXPIRED"})')
            for j in p["jobs"]:
                if args.only and j["profile"] != args.only:
                    continue
                covered = covering_clip(done, j)
                state = f"have clip {covered}" if covered else \
                    f'{round((j["end_s"] - j["start_s"]) * j["fps"] * 0.5)}s freeze'
                print(f'   {j["profile"]:<17} {j["clock"]:<12} '
                      f'{j["start_s"]}-{j["end_s"]}s {j["fps"]}fps  [{state}]')
        return 0

    api = Replay()
    if not api.alive() and not args.launch:
        sys.exit("no replay answering on :2999 — open a replay, or pass --launch "
                 "to have me open it. Either way, keep hands off the mouse: "
                 "moving it hands the camera to Manual Camera and the API is ignored.")

    scratch = Path("/tmp/lol-coach-capture")
    scratch.mkdir(exist_ok=True)
    captured = 0

    for p in plans:
        match_id = p["match_id"]
        if not p["replay_capturable"]:
            print(f"{match_id}: replay expired, skipping")
            continue

        print(f"{match_id}:")
        proc = None
        if args.launch:
            try:
                proc = open_replay(match_id)
            except (RuntimeError, TimeoutError, FileNotFoundError,
                    replay_launch.LcuError) as e:
                print(f"   could not open the replay: {e}")
                continue

        ok, detail = check_replay_matches(api, match_id)
        if not ok:
            print(f"   {detail} — skipping")
            close_everything()
            continue
        print(f"   {detail}")

        out_root = DATA / "matches" / match_id
        done = load_manifest(match_id)

        jobs = [j for j in p["jobs"]
                if not args.only or j["profile"] == args.only]
        i = 0
        while i < len(jobs):
            job = jobs[i]
            retried = job.get("_retried", False)
            key = str(job["start_s"])
            covered = covering_clip(done, job)
            if not args.redo and covered:
                print(f'   {job["clock"]:<12} {job["profile"]:<17} '
                      f'already covered by clip {covered}')
                i += 1
                continue
            if args.limit and captured >= args.limit:
                print("   limit reached")
                break

            print(f'   {job["clock"]:<12} {job["profile"]:<17} '
                  f'{job["start_s"]}-{job["end_s"]}s at {job["fps"]}fps … ', end="", flush=True)
            try:
                result = capture_job(api, job, out_root, scratch,
                                     verify_camera=args.verify_camera)
            except ReplayBusy as e:
                print(f"client stopped answering ({e})")
                rescued = salvage(tmp_dir_for(scratch, job), job, out_root,
                                  job["end_s"], note="rescued; client was busy")
                if rescued["status"] in ("ok", "partial"):
                    print(f'      rescued {rescued["frames_captured"]} frames from disk')
                    done[key] = rescued
                else:
                    done[key] = {"status": "busy", "reason": str(e)}
                save_manifest(match_id, done)
                captured += 1
                i += 1
                continue
            except ReplayGone:
                print("client died mid-capture")
                # The frames may be complete regardless — the client often
                # stops answering exactly as a capture finishes.
                rescued = salvage(tmp_dir_for(scratch, job), job, out_root,
                                  job["end_s"], note="rescued after client died")
                if rescued["status"] in ("ok", "partial"):
                    print(f'      rescued {rescued["frames_captured"]} frames from disk')
                    done[key] = rescued
                    save_manifest(match_id, done)
                    captured += 1
                    i += 1
                    if args.launch:
                        try:
                            proc = open_replay(match_id)
                        except Exception as e:
                            print(f"   could not reopen the replay: {e}")
                            return 1
                    continue
                if args.launch and not retried:
                    # Reopen and retry this same job once before giving up.
                    print("      relaunching the replay and retrying this clip "
                          "(retry 1 of 1 — this is not a loop)")
                    retried = True
                    try:
                        proc = open_replay(match_id)
                        job["_retried"] = True
                        continue          # same job, fresh client
                    except Exception as e:
                        print(f"   could not reopen the replay: {e}")
                done[key] = {"status": "client_died"}
                save_manifest(match_id, done)
                print("\nCaptured clips are kept and will be skipped on the next run.")
                return 1

            # Never downgrade a clip that already captured cleanly.
            if done.get(key, {}).get("status") == "ok" and result["status"] != "ok":
                print(f'      keeping the existing clip; this attempt was '
                      f'{result["status"]}')
            else:
                done[key] = result
            save_manifest(match_id, done)
            captured += 1
            i += 1

            # On this machine the client never answers again after a capture:
            # it writes its frames and then hangs for good. So one clip per
            # launch is the real constraint — relaunch straight away rather
            # than waiting out a recovery that never comes.
            more_to_do = any(not covering_clip(done, j) for j in jobs[i:])
            if more_to_do and args.launch:
                print("      client is spent after a capture; relaunching")
                close_everything()
                try:
                    proc = open_replay(match_id)
                except Exception as e:
                    print(f"   could not reopen the replay: {e}")
                    return 1
            elif more_to_do:
                # Without --launch there is nothing to relaunch; give the
                # client the benefit of the doubt before the next clip.
                try:
                    api.wait_responsive()
                except ReplayGone:
                    print("   the replay closed; re-run with --launch")
                    return 1
            if result["status"] == "ok":
                print(f'{result["frames_captured"]} frames -> '
                      f'{result["mp4_bytes"] / 1e6:.1f} MB, '
                      f'{result["review_frames"]} review frames'
                      + ("" if result["camera_moving"] is None
                         else f', camera {"moving" if result["camera_moving"] else "PARKED"}'))
            else:
                print(f'{result["status"]}: {result.get("reason", "")}')

        close_everything()

    print(f"\n{captured} clips captured. "
          "Run tools/build_review.py to put them in the viewer.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
