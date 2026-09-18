"""Download, launch and tear down replays on macOS.

Two APIs, each for what it's actually good at:

- **The LCU** (the League client's own API, port from its lockfile) owns replay
  *availability*: whether a replay exists, is downloaded, or has expired.
- **Direct process launch** owns *running* it. `open file.rofl` fails on macOS
  because nothing claims the extension, but the game binary plays a replay
  standalone. It logs an LCU remoting error every 60s because no remoting args
  are passed — that is cosmetic, and the replay plays fine.

Direct launch also gives us a PID we own, so teardown is SIGTERM then SIGKILL.
Never use the LCU's `/process-control/v1/process/quit`: that quits the *client*.
"""

from __future__ import annotations

import base64
import json
import os
import signal
import ssl
import subprocess
import time
import urllib.error
import urllib.request
from pathlib import Path

INSTALL = Path("/Applications/League of Legends.app/Contents/LoL")
LOCKFILE = INSTALL / "lockfile"
GAME_BIN = INSTALL / "Game/LeagueOfLegends.app/Contents/MacOS/LeagueofLegends"
GAME_DIR = INSTALL / "Game"
REPLAY_DIR = Path.home() / "Documents/League of Legends/Replays"

# Riot's published riotgames.pem fails to verify under OpenSSL 3 ("Missing
# Authority Key Identifier"), so loopback traffic goes unverified.
_CTX = ssl._create_unverified_context()

# States that mean the replay is never coming.
DEAD_STATES = ("incompatible", "missingOrExpired", "lost", "unsupported", "error")

# The Replay API answers a few seconds before the game is ready to be seeked.
# Measured across sessions: API up at 9-11s, safe to seek at 12-14s.
GAMELOOP_GRACE_S = 8.0


class LcuError(RuntimeError):
    pass


class Lcu:
    """The League client's API. Only needed to download replays."""

    def __init__(self, lockfile: Path = LOCKFILE):
        if not lockfile.exists():
            raise LcuError("League client is not running (no lockfile)")
        # name:pid:port:password:protocol
        _, pid, port, password, protocol = lockfile.read_text().strip().split(":")
        self.pid, self.port = int(pid), int(port)
        self.base = f"{protocol}://127.0.0.1:{port}"
        self._auth = base64.b64encode(f"riot:{password}".encode()).decode()

    def req(self, method: str, path: str, body: dict | None = None) -> tuple[int, dict | None]:
        headers = {"Authorization": "Basic " + self._auth}
        data = None
        if body is not None:
            data = json.dumps(body).encode()
            headers["Content-Type"] = "application/json"
        req = urllib.request.Request(self.base + path, data=data,
                                     headers=headers, method=method)
        try:
            with urllib.request.urlopen(req, context=_CTX, timeout=10) as resp:
                raw = resp.read()
                return resp.status, (json.loads(raw) if raw else None)
        except urllib.error.HTTPError as e:
            raw = e.read()
            try:
                return e.code, (json.loads(raw) if raw else None)
            except json.JSONDecodeError:
                return e.code, None

    def config(self) -> dict:
        return self.req("GET", "/lol-replays/v1/configuration")[1] or {}

    def rofls_path(self) -> Path:
        body = self.req("GET", "/lol-replays/v1/rofls/path")[1]
        return Path(body) if isinstance(body, str) else REPLAY_DIR

    def install_paths(self) -> dict:
        return self.req(
            "GET", "/lol-patch/v1/products/league_of_legends/install-location")[1] or {}

    def metadata(self, game_id: int | str) -> dict | None:
        """None means no local metadata yet — not that the replay is missing."""
        code, body = self.req("GET", f"/lol-replays/v1/metadata/{game_id}")
        return body if code == 200 else None

    def create_metadata(self, game_id, game_version: str, game_type: str,
                        queue_id: int, game_end_ms: int) -> int:
        return self.req("POST", f"/lol-replays/v2/metadata/{game_id}/create", {
            "gameVersion": game_version, "gameType": game_type,
            "queueId": queue_id, "gameEnd": game_end_ms})[0]

    def request_download(self, game_id, component: str = "lol-coach") -> int:
        """Fire and forget: 204 even for a nonexistent game. Poll metadata."""
        return self.req("POST", f"/lol-replays/v1/rofls/{game_id}/download",
                        {"componentType": component})[0]

    def scan(self) -> int:
        return self.req("POST", "/lol-replays/v1/rofls/scan", {})[0]

    def watch_phase(self) -> str | None:
        """None | WatchStarted | WatchInProgress | WatchFailedToLaunch."""
        return self.req("GET", "/lol-gameflow/v1/watch")[1]


def rofl_patch(path: Path) -> str:
    """Read the patch out of a .rofl header — no client, no launch.

    ROFL2 layout (patch 14.11+): "RIOT" magic, a length byte at 0x0E, then the
    version string. Public docs describe the older v1 layout and don't match.
    The cheapest way to know a replay is unopenable before trying.
    """
    head = path.read_bytes()[:64]
    if head[:4] != b"RIOT":
        raise ValueError(f"{path.name} is not a .rofl file")
    n = head[0x0E]
    return head[0x0F:0x0F + n].decode("ascii", "replace")


def patch_series(version: str) -> str:
    """`16.18.817.5716` -> `16.18`, the part that has to match to open."""
    return ".".join(version.split(".")[:2])


def replay_file(platform_id: str, game_id: int | str,
                rofls: Path = REPLAY_DIR) -> Path:
    return rofls / f"{platform_id}-{game_id}.rofl"


def ensure_downloaded(lcu: Lcu, game_id: int | str, match_info: dict,
                      poll: float = 1.0, timeout: float = 180) -> bool:
    """Get a replay to state `watch`, asking for the download at most once.

    A failed download never becomes an error state, so this must time out
    rather than wait for one.
    """
    if lcu.metadata(game_id) is None:
        lcu.create_metadata(
            game_id,
            match_info["gameVersion"],
            match_info.get("gameType", "MATCHED_GAME"),
            match_info["queueId"],
            match_info["gameCreation"] + match_info["gameDuration"] * 1000,
        )

    deadline = time.monotonic() + timeout
    asked = False
    while time.monotonic() < deadline:
        state = (lcu.metadata(game_id) or {}).get("state")
        if state == "watch":
            return True
        if state in DEAD_STATES:
            raise LcuError(f"replay unavailable: {state}")
        if state in ("download", "retryDownload") and not asked:
            lcu.request_download(game_id)
            asked = True
        time.sleep(poll)
    raise TimeoutError(f"replay {game_id} never reached state=watch")


def launch(rofl: Path, region: str = "NA", platform_id: str = "NA1",
           locale: str = "en_US", game_bin: Path = GAME_BIN,
           game_dir: Path = GAME_DIR, base_dir: Path = INSTALL,
           log: Path | None = None) -> subprocess.Popen:
    """Start the game on a replay file. Args are the ones the client itself uses.

    `-GameBaseDir` must be the **LoL** directory, not `LoL/Game`. There is a
    second `game.cfg` under `LoL/Game/Config` without `EnableReplayApi`, so
    pointing at the wrong one starts the replay with the API switched off: the
    port answers, every `/replay/*` path 404s, and the log quietly omits
    "Replay API is enabled".
    """
    if not rofl.exists():
        raise FileNotFoundError(rofl)
    if not (base_dir / "Config/game.cfg").exists():
        raise RuntimeError(f"no Config/game.cfg under {base_dir}")

    sink = open(log, "wb") if log else subprocess.DEVNULL
    return subprocess.Popen(
        [str(game_bin), str(rofl),
         f"-GameBaseDir={base_dir}",
         f"-Region={region}", f"-PlatformID={platform_id}", f"-Locale={locale}",
         "-SkipBuild", "-EnableCrashpad=true", "-UseMetal=1:1"],
        cwd=str(game_dir),
        stdout=sink, stderr=subprocess.STDOUT if log else subprocess.DEVNULL,
    )


def wait_ready(proc: subprocess.Popen, timeout: float = 180,
               poll: float = 1.0) -> None:
    """Wait for the Replay API, then for the game to be seekable.

    Checks the process each tick, so an early exit raises instead of hanging
    until the timeout.
    """
    deadline = time.monotonic() + timeout
    api_disabled_since = None

    while time.monotonic() < deadline:
        if proc.poll() is not None:
            raise RuntimeError(
                f"game exited (rc={proc.returncode}) before the Replay API came up")
        try:
            urllib.request.urlopen("https://127.0.0.1:2999/replay/playback",
                                   context=_CTX, timeout=2).read()
            time.sleep(GAMELOOP_GRACE_S)   # API answers before seeking is safe
            return
        except urllib.error.HTTPError as e:
            # A 404 means the HTTP server is up but the replay endpoints are
            # not registered — the API is off for this session. Waiting won't
            # fix that, so fail with the actual cause rather than a timeout.
            if e.code == 404:
                api_disabled_since = api_disabled_since or time.monotonic()
                if time.monotonic() - api_disabled_since > 20:
                    raise RuntimeError(
                        "the replay is running but the Replay API is disabled for "
                        "this session (every /replay/* path 404s). Check that "
                        f"EnableReplayApi=1 is in {INSTALL}/Config/game.cfg and "
                        "that -GameBaseDir points at that directory."
                    ) from e
            time.sleep(poll)
        except Exception:
            api_disabled_since = None      # still loading
            time.sleep(poll)
    raise TimeoutError("Replay API never came up on 127.0.0.1:2999")


def teardown(proc: subprocess.Popen, grace: float = 10) -> int | None:
    """Close a replay we launched. A directly-launched replay never connects to
    the client, so there is no client state to corrupt by killing it."""
    if proc.poll() is not None:
        return proc.returncode
    proc.send_signal(signal.SIGTERM)
    start = time.monotonic()
    while time.monotonic() - start < grace:
        if proc.poll() is not None:
            return proc.returncode
        time.sleep(0.25)
    proc.kill()
    proc.wait()
    return proc.returncode


def crashed_since(marker_time: float) -> bool:
    """True if the game left a fresh crash report — a crash, not a clean close."""
    crashes = INSTALL / "Logs/GameCrashes"
    if not crashes.exists():
        return False
    for p in crashes.iterdir():
        if p.name in ("pending", "completed") and p.is_dir():
            if any(f.stat().st_mtime > marker_time for f in p.iterdir()):
                return True
    return False
