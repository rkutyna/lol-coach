"""Client for the in-game Replay API (https://127.0.0.1:2999).

Every rule below was paid for — either by crashing this Mac's client, or by
reading projects that already crashed theirs. See docs/PHASE2.md for sources.

Hard rules encoded here:

- POSTs need `Accept: application/json` or they 406.
- **Only `cameraMode` "top" or "path".** "tps" killed the client instantly and
  "fps" is reported fatal elsewhere, but "path" is *required* for camera moves
  to take effect at all (Riot issue developer-relations#877).
- **Never let the playhead reach the replay's end** — that closes the replay and
  takes the API down with it. Captures stop `END_MARGIN_S` short.
- A paused replay's camera does not move, a render POST clears the selection and
  snaps the camera, and seeking clears the selection too. So: seek first, start
  playing, *then* aim, and re-assert while playing.
- Selection readback lies: the client echoes a name it isn't holding. The only
  proof of a real follow is `cameraPosition` changing over time.
- `selectionName` wants the internal champion name ("Lillia", "MissFortune"),
  not a summoner name.
- Bulk render POSTs silently drop `selectionName`/`cameraAttached`; send the
  critical fields in their own small POSTs with a settle between them.
- `codec: "webm"` is unusable here: the audio encoder fails and no API field can
  disable audio. PNG frames only, muxed with ffmpeg.
- The API stops answering for tens of seconds while writing frames, and drops
  the connection outright when a capture ends. Neither means the client died —
  check for the process to tell them apart.
"""

from __future__ import annotations

import http.client
import json
import ssl
import subprocess
import time
import urllib.error
import urllib.request

BASE = "https://127.0.0.1:2999"
GAME_PROCESS = "Game/LeagueOfLegends.app"

# The only camera modes that may be sent.
#
# "path" is REQUIRED for camera moves to take effect at all — without it the API
# accepts a new cameraPosition, echoes it back, and the game ignores it. That is
# Riot issue developer-relations#877, whose reporter confirmed the fix. "top" is
# the client default, and toggling top -> path clears a stuck camera.
#
# "fps" and "tps" are client-killers: "tps" killed this machine's client
# instantly, and another project's log records "fps" doing the same. "focus" is
# untested and unnecessary. None of them are ever sent.
SAFE_CAMERA_MODES = ("top", "path")

# Pause between render POSTs. Rapid seek/mode changes are unstable on macOS.
SETTLE_S = 0.35
# Stay this far from the end of the replay; reaching it kills the API.
END_MARGIN_S = 10.0
# The Directed Camera needs several seconds of undisturbed playback to engage,
# and a capture should start from where playback already is rather than seeking
# right before the render.
WARMUP_S = 9.0

_CTX = ssl.create_default_context()
_CTX.check_hostname = False
_CTX.verify_mode = ssl.CERT_NONE


class ReplayGone(RuntimeError):
    """The replay client died (crashed or was closed)."""


class ReplayBusy(RuntimeError):
    """The API did not answer in time, but the client is still alive."""


def game_running() -> bool:
    return subprocess.run(["pgrep", "-f", GAME_PROCESS],
                          capture_output=True).returncode == 0


def internal_champion_name(raw: str) -> str:
    """`game_character_displayname_MissFortune` -> `MissFortune`."""
    return raw.rsplit("_", 1)[-1] if raw else ""


class Replay:
    def __init__(self, timeout: float = 15.0):
        self.timeout = timeout

    def _once(self, method: str, path: str, body: dict | None, timeout: float) -> dict:
        data = json.dumps(body).encode() if body is not None else None
        req = urllib.request.Request(BASE + path, data=data, method=method, headers={
            "Accept": "application/json",
            "Content-Type": "application/json",
            "User-Agent": "lol-coach/0.1",
        })
        with urllib.request.urlopen(req, timeout=timeout, context=_CTX) as resp:
            raw = resp.read()
            return json.loads(raw) if raw else {}

    def _call(self, method: str, path: str, body: dict | None = None,
              timeout: float | None = None, retry: bool = True) -> dict:
        """One retry: the client drops the odd request while it is seeking."""
        t = timeout or self.timeout
        try:
            return self._once(method, path, body, t)
        except urllib.error.HTTPError as e:
            # A render POST 400s on any key it doesn't know, failing the whole
            # request — worth saying so loudly rather than silently degrading.
            raise RuntimeError(f"HTTP {e.code} on {path}: {e.read()[:200]!r}") from e
        except (urllib.error.URLError, TimeoutError, ssl.SSLError,
                # The client drops the connection outright at the end of a
                # capture. RemoteDisconnected is a ConnectionResetError, not a
                # URLError, so it needs OSError/HTTPException to be caught here
                # — without them it escapes as an unhandled crash.
                OSError, http.client.HTTPException) as e:
            if retry and game_running():
                time.sleep(1.0)
                try:
                    return self._once(method, path, body, t)
                except Exception:
                    pass
            raise (ReplayGone(f"replay client gone ({e})") if not game_running()
                   else ReplayBusy(f"no answer from {path} ({e})")) from e

    # --- state -----------------------------------------------------------

    def alive(self) -> bool:
        try:
            self._call("GET", "/replay/playback", timeout=5, retry=False)
            return True
        except ReplayBusy:
            return True
        except ReplayGone:
            return False

    def wait_until_ready(self, timeout: float = 240) -> bool:
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            try:
                self._call("GET", "/replay/playback", timeout=5, retry=False)
                return True
            except (ReplayBusy, ReplayGone):
                time.sleep(3)
        return False

    def playback(self) -> dict:
        return self._call("GET", "/replay/playback")

    def wait_responsive(self, timeout: float = 150, poll: float = 5.0) -> bool:
        """Block until the client answers again after a capture.

        A capture leaves the client busy for far longer than a single request
        timeout, so the next clip's seek fails unless we wait this out. Raises
        ReplayGone if the client actually died.
        """
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            try:
                self._call("GET", "/replay/playback", timeout=8, retry=False)
                return True
            except ReplayBusy:
                time.sleep(poll)
            except ReplayGone:
                raise
        return False

    def length(self) -> float:
        return self.playback().get("length", 0.0)

    def safe_end(self, end_s: float) -> float:
        """Clamp a capture's end so the playhead never reaches the replay's end."""
        return min(end_s, max(0.0, self.length() - END_MARGIN_S))

    def live_state(self) -> dict:
        """All ten players' items, scores and HP at the seeked instant."""
        return self._call("GET", "/liveclientdata/allgamedata", timeout=20)

    def roster(self) -> list[dict]:
        """Player list with internal champion names for use as selections."""
        players = self._call("GET", "/liveclientdata/playerlist", timeout=20)
        out = []
        for p in players if isinstance(players, list) else []:
            out.append({
                "summoner": p.get("riotIdGameName") or p.get("summonerName", ""),
                "champion": p.get("championName", ""),
                # The selection wants this, not the display name.
                "selection": internal_champion_name(p.get("rawChampionName", "")),
                "team": p.get("team", ""),
            })
        return out

    # --- playback --------------------------------------------------------

    def seek(self, seconds: float, settle: float = 25.0) -> float:
        """Jump to a game time, paused, and wait for the seek to finish.

        Seeking clears any selection, so aim the camera after this, not before.
        """
        self._call("POST", "/replay/playback", {"time": seconds, "paused": True})
        deadline = time.monotonic() + settle
        while time.monotonic() < deadline:
            p = self.playback()
            if not p.get("seeking") and abs(p.get("time", -1) - seconds) < 2:
                return p["time"]
            time.sleep(0.5)
        return self.playback().get("time", -1)

    def play(self, speed: float = 1.0) -> None:
        self._call("POST", "/replay/playback", {"paused": False, "speed": speed})

    def pause(self) -> None:
        self._call("POST", "/replay/playback", {"paused": True})

    # --- render ----------------------------------------------------------

    def render(self) -> dict:
        return self._call("GET", "/replay/render")

    def set_render(self, **fields) -> dict:
        mode = fields.get("cameraMode")
        if mode is not None and mode not in SAFE_CAMERA_MODES:
            raise ValueError(
                f'cameraMode {mode!r} is unsafe — "fps"/"tps" kill the client. '
                f"Only {SAFE_CAMERA_MODES} may be sent; use set_camera_mode()."
            )
        return self._call("POST", "/replay/render", fields)

    def set_camera_mode(self, mode: str = "path") -> dict:
        """Set the camera mode, guarded to the two safe values.

        "path" must be active for position changes to take effect at all.
        """
        if mode not in SAFE_CAMERA_MODES:
            raise ValueError(f"refusing cameraMode {mode!r}; "
                             f"only {SAFE_CAMERA_MODES} are safe")
        return self._call("POST", "/replay/render", {"cameraMode": mode})

    def unstick_camera(self) -> None:
        """top -> path, the documented fix for a camera that stops responding."""
        self.set_camera_mode("top")
        time.sleep(SETTLE_S)
        self.set_camera_mode("path")
        time.sleep(SETTLE_S)

    def set_hud(self, *, minimap=True, scoreboard=False, timeline=False,
                replay_controls=False, kill_callouts=True, health_bars=True,
                fog_of_war=True) -> dict:
        """HUD and fog per the capture spec. Sent before aiming the camera.

        Fog on renders only what our team could see, which is the whole point
        for "what did you know" moments; off shows what actually happened.
        """
        return self.set_render(
            fogOfWar=fog_of_war,
            interfaceAll=True,
            interfaceMinimap=minimap,
            interfaceScoreboard=scoreboard,
            interfaceTimeline=timeline,
            interfaceReplay=replay_controls,
            interfaceKillCallouts=kill_callouts,
            healthBarChampions=health_bars,
        )

    def select_champion(self, selection: str) -> None:
        """Assert a selection, mainly to get that champion's HUD on screen.

        Framing is not reliable this way — the camera often stays parked — so
        the camera itself is placed with a sequence. Sent as separate small
        POSTs because a bulk render silently drops these two fields.
        """
        self.set_render(cameraAttached=False)
        time.sleep(SETTLE_S)
        self.set_render(selectionName=selection)
        time.sleep(SETTLE_S)
        self.set_render(cameraAttached=True, selectionOffset={"x": 0, "y": 0, "z": 0})
        time.sleep(SETTLE_S)

    def camera_is_moving(self, gap: float = 1.5) -> bool:
        """Proof of a real follow: the position changes. Readback can't be trusted."""
        a = self.render().get("cameraPosition", {})
        time.sleep(gap)
        b = self.render().get("cameraPosition", {})
        return any(abs(a.get(k, 0) - b.get(k, 0)) > 1 for k in ("x", "y", "z"))

    # --- sequence (the reliable way to aim) -------------------------------

    def set_sequence(self, camera_positions: list[dict],
                     camera_rotations: list[dict] | None = None) -> dict:
        """Keyframe the camera over game time.

        Each keyframe is {"time": <game seconds>, "value": {...}, "blend": ...}.
        Positions are world coordinates with **x and z on the ground plane and y
        as altitude**; rotations are degrees as (yaw, pitch, roll).

        A sequence is authoritative across time, so unlike a render it isn't
        undone by the camera snap. Use "snap" to cut between positions, and
        offset a snap keyframe's time slightly or the client interpolates oddly.

        Camera mode is switched to "path" first: without it the client accepts
        positions and then ignores them.
        """
        self.set_camera_mode("path")
        time.sleep(SETTLE_S)
        body: dict = {"cameraPosition": camera_positions}
        if camera_rotations:
            body["cameraRotation"] = camera_rotations
        return self._call("POST", "/replay/sequence", body, timeout=20)

    def clear_sequence(self) -> None:
        self._call("POST", "/replay/sequence", {}, timeout=20)

    def park_camera(self, x: float, z: float, altitude: float = 1800,
                    pitch: float = 65.0, at_time: float = 0.0,
                    until: float | None = None) -> dict:
        """Hold a fixed overhead camera on a known spot for a whole window.

        Deaths and objective pits have exact coordinates in the timeline, so
        this frames the action without needing champion follow at all.
        """
        # A y (altitude) of 0 is rejected by the client as invalid input.
        if altitude <= 0:
            raise ValueError("altitude must be > 0; y=0 is invalid input")
        keys = [{"time": at_time, "value": {"x": x, "y": altitude, "z": z},
                 "blend": "linear"}]
        rots = [{"time": at_time, "value": {"x": 0.0, "y": pitch, "z": 0.0},
                 "blend": "linear"}]
        if until is not None:
            keys.append({"time": until, "value": {"x": x, "y": altitude, "z": z},
                         "blend": "linear"})
            rots.append({"time": until, "value": {"x": 0.0, "y": pitch, "z": 0.0},
                         "blend": "linear"})
        return self.set_sequence(keys, rots)

    # --- recording -------------------------------------------------------

    def recording(self) -> dict:
        return self._call("GET", "/replay/recording", timeout=30)

    def record_frames(self, out_dir: str, start_s: float, end_s: float,
                      fps: int = 2) -> dict:
        """Start a PNG capture. Playback is started first, as League Director does.

        `path` is a folder for PNG output (it becomes a numbered sequence).
        Width and height are ignored — the game window is the capture size —
        and `enforceFrameRate` is correct for PNG, where it guarantees frames.
        """
        self.play()
        body = {
            "recording": True,
            "codec": "png",
            "path": out_dir,
            "startTime": start_s,
            "endTime": self.safe_end(end_s),
            "framesPerSecond": fps,
            "enforceFrameRate": True,
            "replaySpeed": 1,
        }
        return self._call("POST", "/replay/recording", body, timeout=30)

    def wait_for_recording(self, out_dir: str | None = None, poll: float = 4.0,
                           timeout: float = 420, stable_polls: int = 3,
                           progress=None, expected: int | None = None) -> bool:
        """Wait out a capture, tolerating the long unresponsive stretch.

        Completion is judged from the frames on disk first and the API second.
        The API frequently stops answering right as a capture finishes, so
        requiring its confirmation means waiting out the whole timeout for a
        capture that is already complete. `expected` is the frame count the
        window should produce; reaching it, and holding, is proof enough.

        A file being written plateaus between chunks, so "held still" means
        several consecutive polls — not one.
        """
        from pathlib import Path

        deadline = time.monotonic() + timeout
        last_count, stable = -1, 0
        while time.monotonic() < deadline:
            done = False
            try:
                done = not self.recording().get("recording", False)
            except ReplayBusy:
                pass                       # busy writing; frames still tell us
            except ReplayGone:
                raise

            if out_dir:
                count = len(list(Path(out_dir).rglob("*.png")))
                stable = stable + 1 if count == last_count and count > 0 else 0
                if progress and count != last_count:
                    progress(count)
                last_count = count

                have_them_all = expected is not None and count >= expected
                if have_them_all and stable >= 1:
                    return True
                if done and stable >= stable_polls:
                    return True
            elif done:
                return True
            time.sleep(poll)
        return False

    def stop_recording(self) -> None:
        try:
            self._call("POST", "/replay/recording", {"recording": False}, timeout=30)
        except (ReplayBusy, ReplayGone):
            pass
