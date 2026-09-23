"""Riot API client: rate limiting, retries, and on-disk caching.

Match and timeline payloads never change once a game ends, so they are cached
forever. Everything else is fetched fresh.
"""

from __future__ import annotations

import json
import os
import time
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
DATA = ROOT / "data"
CACHE = DATA / "cache"

# Riot routes accounts and matches by super-region, not by platform.
REGIONAL = {"NA1": "americas", "BR1": "americas", "LA1": "americas", "LA2": "americas",
            "EUW1": "europe", "EUN1": "europe", "TR1": "europe", "RU": "europe",
            "KR": "asia", "JP1": "asia", "OC1": "sea", "PH2": "sea", "SG2": "sea",
            "TH2": "sea", "TW2": "sea", "VN2": "sea"}

# Dev keys allow 20 requests/second and 100 per 2 minutes. We stay well under.
MIN_INTERVAL = 0.08


class RiotError(RuntimeError):
    pass


def api_key() -> str:
    key = os.environ.get("RIOT_API_KEY")
    if not key:
        env = ROOT / ".env"
        if env.exists():
            for line in env.read_text().splitlines():
                line = line.strip()
                if line.startswith("RIOT_API_KEY="):
                    key = line.split("=", 1)[1].strip().strip("'\"")
    if not key:
        raise RiotError(
            "No API key. Put RIOT_API_KEY=RGAPI-... in lol-coach/.env "
            "(get one at https://developer.riotgames.com)"
        )
    return key


class Riot:
    def __init__(self, platform: str = "NA1"):
        self.platform = platform.upper()
        self.region = REGIONAL.get(self.platform, "americas")
        self.key = api_key()
        self._last = 0.0
        CACHE.mkdir(parents=True, exist_ok=True)

    def _get(self, host: str, path: str, params: dict | None = None) -> dict | list:
        url = f"https://{host}{path}"
        if params:
            url += "?" + urllib.parse.urlencode(params)

        for attempt in range(5):
            wait = MIN_INTERVAL - (time.monotonic() - self._last)
            if wait > 0:
                time.sleep(wait)
            # Riot sits behind Cloudflare, which rejects urllib's default
            # user agent with "error code: 1010". Any normal UA passes.
            req = urllib.request.Request(url, headers={
                "X-Riot-Token": self.key,
                "User-Agent": "lol-coach/0.1",
                "Accept": "application/json",
            })
            try:
                with urllib.request.urlopen(req, timeout=20) as resp:
                    self._last = time.monotonic()
                    return json.loads(resp.read())
            except urllib.error.HTTPError as e:
                self._last = time.monotonic()
                if e.code == 429:
                    retry = int(e.headers.get("Retry-After", "5"))
                    print(f"  rate limited, waiting {retry}s")
                    time.sleep(retry + 1)
                    continue
                if e.code == 401:
                    raise RiotError(
                        "401 Unknown apikey — development keys expire after 24h. "
                        "Regenerate at https://developer.riotgames.com and update .env"
                    ) from e
                if e.code == 404:
                    raise RiotError(f"404 not found: {path}") from e
                if 500 <= e.code < 600 and attempt < 4:
                    time.sleep(2 ** attempt)
                    continue
                raise RiotError(f"HTTP {e.code} on {path}: {e.read()[:200]!r}") from e
            except urllib.error.URLError as e:
                if attempt < 4:
                    time.sleep(2 ** attempt)
                    continue
                raise RiotError(f"network error on {path}: {e.reason}") from e
        raise RiotError(f"gave up on {path}")

    def _cached(self, name: str, host: str, path: str) -> dict:
        f = CACHE / f"{name}.json"
        if f.exists():
            return json.loads(f.read_text())
        data = self._get(host, path)
        f.write_text(json.dumps(data))
        return data

    # --- endpoints -------------------------------------------------------

    def account(self, game_name: str, tag_line: str) -> dict:
        host = f"{self.region}.api.riotgames.com"
        path = f"/riot/account/v1/accounts/by-riot-id/{urllib.parse.quote(game_name)}/{urllib.parse.quote(tag_line)}"
        return self._get(host, path)

    def match_ids(self, puuid: str, count: int = 20, queue: int | None = None,
                  start: int = 0) -> list[str]:
        host = f"{self.region}.api.riotgames.com"
        params: dict = {"count": min(count, 100), "start": start}
        if queue:
            params["queue"] = queue
        return self._get(host, f"/lol/match/v5/matches/by-puuid/{puuid}/ids", params)

    def match(self, match_id: str) -> dict:
        host = f"{self.region}.api.riotgames.com"
        return self._cached(f"{match_id}_match", host, f"/lol/match/v5/matches/{match_id}")

    def timeline(self, match_id: str) -> dict:
        host = f"{self.region}.api.riotgames.com"
        return self._cached(f"{match_id}_timeline", host,
                            f"/lol/match/v5/matches/{match_id}/timeline")

    def league_entries(self, puuid: str) -> list[dict]:
        """Current ranked entries (solo and flex). Not cached: ranks move."""
        host = f"{self.platform.lower()}.api.riotgames.com"
        return self._get(host, f"/lol/league/v4/entries/by-puuid/{puuid}")
