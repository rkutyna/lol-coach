"""Tiny static server for web/ — run via `python3 -I tools/serve.py [port]`.

Uses -I and an explicit chdir because the desktop app can launch a server with
an unreadable working directory, which breaks http.server's own --directory.
"""

import http.server
import os
import socketserver
import sys
from pathlib import Path

PORT = int(sys.argv[1]) if len(sys.argv) > 1 else 8777
os.chdir(Path(__file__).resolve().parent.parent / "web")
socketserver.TCPServer.allow_reuse_address = True
print(f"serving web/ at http://127.0.0.1:{PORT}", flush=True)
socketserver.TCPServer(("127.0.0.1", PORT), http.server.SimpleHTTPRequestHandler).serve_forever()
