"""Static server for web/, plus a tiny JSON API for the review queue.

  python3 -I tools/serve.py [port]

Uses -I and an explicit chdir because the desktop app can launch a server with
an unreadable working directory, which breaks http.server's own --directory.

The API is one resource: the queue of games ticked for review/video, which
the picker page reads and writes and tools/run_queue.py acts on.

  GET  /api/queue   -> {"updated_utc": ..., "games": {match_id: {review, video}}}
                       (an empty document if data/review_queue.json doesn't exist yet)
  POST /api/queue   -> body is either the games object on its own, or a full
                       {"updated_utc": ..., "games": {...}} document. Only
                       match ids that exist under data/matches/ are accepted,
                       so a typo in the page can't write junk to disk. On
                       success, writes data/review_queue.json (with a fresh
                       updated_utc) and returns the saved document. Anything
                       else is a 400 with a JSON {"error": "..."}.

Bound to 127.0.0.1 only — this is a local tool, not a service.
"""

from __future__ import annotations

import http.server
import json
import socketserver
import sys
from datetime import datetime, timezone
from pathlib import Path

# -I (isolated mode) is how this is meant to be run, but it also drops the
# script's own directory from sys.path, which breaks the plain `import riot`
# every other tool gets for free.
sys.path.insert(0, str(Path(__file__).resolve().parent))
from riot import DATA, ROOT

WEB = ROOT / "web"
QUEUE_F = DATA / "review_queue.json"

PORT = int(sys.argv[1]) if len(sys.argv) > 1 else 8777

# The queue is a few hundred games at most; anything near this size is either
# a mistake or an attempt to fill the disk, not a real request.
MAX_BODY_BYTES = 1_000_000


def known_match_ids() -> set[str]:
    """Match ids that actually exist on disk, so a typo can't inject junk."""
    matches = DATA / "matches"
    if not matches.exists():
        return set()
    return {d.name for d in matches.iterdir() if d.is_dir()}


def load_queue() -> dict:
    """The saved queue, or an empty one if the file is missing or broken."""
    if not QUEUE_F.exists():
        return {"updated_utc": None, "games": {}}
    try:
        doc = json.loads(QUEUE_F.read_text())
        games = doc.get("games", {})
        return {"updated_utc": doc.get("updated_utc"),
                "games": games if isinstance(games, dict) else {}}
    except Exception:
        return {"updated_utc": None, "games": {}}


def validate_games(body: object, valid_ids: set[str]) -> tuple[dict | None, str | None]:
    """(clean games dict, None) on success, or (None, error message)."""
    if not isinstance(body, dict):
        return None, "body must be a JSON object"

    if "games" in body:
        games = body["games"]
        if not isinstance(games, dict):
            return None, "'games' must be an object"
    else:
        games = body

    clean: dict[str, dict[str, bool]] = {}
    for match_id, sel in games.items():
        if match_id not in valid_ids:
            return None, f"unknown match id: {match_id}"
        if not isinstance(sel, dict):
            return None, f"{match_id}: value must be an object"
        extra = set(sel) - {"review", "video"}
        if extra:
            return None, f"{match_id}: unknown field(s) {sorted(extra)}"
        review = sel.get("review", False)
        video = sel.get("video", False)
        if not isinstance(review, bool) or not isinstance(video, bool):
            return None, f"{match_id}: 'review' and 'video' must be booleans"
        clean[match_id] = {"review": review, "video": video}
    return clean, None


class Handler(http.server.SimpleHTTPRequestHandler):
    def do_GET(self):
        if self.path == "/api/queue":
            self._json(200, load_queue())
            return
        super().do_GET()

    def do_POST(self):
        if self.path != "/api/queue":
            self.send_error(404)
            return

        try:
            length = int(self.headers.get("Content-Length", 0))
        except ValueError:
            length = 0
        if length <= 0 or length > MAX_BODY_BYTES:
            self._json(400, {"error": "body missing or too large"})
            return
        raw = self.rfile.read(length)

        try:
            body = json.loads(raw)
        except json.JSONDecodeError as e:
            self._json(400, {"error": f"invalid JSON: {e}"})
            return

        games, err = validate_games(body, known_match_ids())
        if err:
            self._json(400, {"error": err})
            return

        doc = {"updated_utc": datetime.now(timezone.utc).isoformat(timespec="seconds"),
               "games": games}
        # QUEUE_F is a fixed path under data/; nothing here is built from the
        # request, so there's no way this write escapes data/.
        QUEUE_F.parent.mkdir(parents=True, exist_ok=True)
        QUEUE_F.write_text(json.dumps(doc, indent=2))
        self._json(200, doc)

    def _json(self, status: int, payload: dict) -> None:
        body = json.dumps(payload).encode()
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)


if __name__ == "__main__":
    import os
    os.chdir(WEB)
    socketserver.TCPServer.allow_reuse_address = True
    print(f"serving web/ at http://127.0.0.1:{PORT}", flush=True)
    socketserver.TCPServer(("127.0.0.1", PORT), Handler).serve_forever()
