#!/usr/bin/env python3
"""Fetch recent Summoner's Rift games for the tracked player.

  python tools/fetch_match.py                 # 5 most recent SR games
  python tools/fetch_match.py --count 10
  python tools/fetch_match.py --match NA1_5643527353

Writes data/matches/<match_id>/{match.json,timeline.json,meta.json,ranks.json}.
Ranks are the lobby's ranks *now*, so fetch soon after playing.
"""

from __future__ import annotations

import argparse
import json
from datetime import datetime, timezone
from pathlib import Path

import ranks
from riot import DATA, Riot, RiotError

# Summoner's Rift 5v5 only. ARAM (450), Swiftplay (480), Arena (1700) and bot
# games teach nothing about jungle pathing, so they never enter the dataset.
SR_QUEUES = {400: "draft", 420: "ranked solo", 430: "blind", 440: "ranked flex"}

# A replay can only be opened while its patch is live, so ingestion that needs
# video is time-boxed. Patches run about two weeks.
PATCH_DAYS = 14


def profile() -> dict:
    """Who we're coaching. Kept out of the repo: it names a real account."""
    f = DATA / "player.json"
    if not f.exists():
        raise RiotError(
            "No data/player.json. Copy data/player.example.json to "
            "data/player.json and put your Riot ID in it."
        )
    return json.loads(f.read_text())


def fetch(api: Riot, match_id: str, puuid: str) -> dict | None:
    match = api.match(match_id)
    info = match["info"]
    queue = info["queueId"]
    if queue not in SR_QUEUES:
        return None

    timeline = api.timeline(match_id)
    me = next((p for p in info["participants"] if p["puuid"] == puuid), None)
    if me is None:
        return None

    out = DATA / "matches" / match_id
    out.mkdir(parents=True, exist_ok=True)
    (out / "match.json").write_text(json.dumps(match))
    (out / "timeline.json").write_text(json.dumps(timeline))

    played = datetime.fromtimestamp(info["gameStartTimestamp"] / 1000, timezone.utc)
    age_days = (datetime.now(timezone.utc) - played).days
    meta = {
        "match_id": match_id,
        "queue": SR_QUEUES[queue],
        "queue_id": queue,
        "patch": info.get("gameVersion", "?"),
        "played_utc": played.isoformat(timespec="seconds"),
        "age_days": age_days,
        "duration_s": info["gameDuration"],
        "champion": me["championName"],
        "position": me.get("teamPosition") or "?",
        "win": me["win"],
        "kda": f'{me["kills"]}/{me["deaths"]}/{me["assists"]}',
        "cs": me["totalMinionsKilled"] + me["neutralMinionsKilled"],
        "participant_id": me["participantId"],
        "summoner_name": me.get("riotIdGameName", ""),
        "replay_capturable": age_days <= PATCH_DAYS,
    }
    (out / "meta.json").write_text(json.dumps(meta, indent=2))
    return meta


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--count", type=int, default=5, help="how many SR games (default 5)")
    ap.add_argument("--match", action="append", help="specific match id(s)")
    args = ap.parse_args()

    try:
        p = profile()
        api = Riot(p["platform"])
        acct = api.account(p["game_name"], p["tag_line"])
    except RiotError as e:
        print(f"error: {e}")
        return 1
    puuid = acct["puuid"]
    print(f'{acct["gameName"]}#{acct["tagLine"]} on {p["platform"]}')

    if args.match:
        ids = args.match
    else:
        # Queue can only be filtered one value at a time, so pull recent ids
        # unfiltered and keep the SR ones until we have enough.
        ids = api.match_ids(puuid, count=min(args.count * 6, 100))

    kept: list[dict] = []
    rank_cache: dict[str, list[dict]] = {}
    for mid in ids:
        if len(kept) >= args.count and not args.match:
            break
        try:
            meta = fetch(api, mid, puuid)
        except RiotError as e:
            print(f"  {mid}: {e}")
            continue
        if meta is None:
            continue
        match_dir = DATA / "matches" / mid
        if not (match_dir / "ranks.json").exists():
            try:
                ranks.save(api, match_dir, rank_cache)
            except RiotError as e:
                print(f"  {mid}: ranks not fetched ({e}); run tools/ranks.py later")
        kept.append(meta)
        flag = "" if meta["replay_capturable"] else "  [replay expired]"
        print(f'  {mid}  {meta["played_utc"][:10]}  {meta["queue"]:<12}'
              f'{meta["champion"]:<10} {meta["position"]:<7} '
              f'{"W" if meta["win"] else "L"}  {meta["kda"]:<8} '
              f'{meta["duration_s"] // 60}min{flag}')

    if not kept:
        print("no Summoner's Rift games found")
        return 1

    index = DATA / "matches" / "index.json"
    index.write_text(json.dumps(kept, indent=2))
    print(f'\n{len(kept)} games in data/matches/  '
          f'({sum(m["replay_capturable"] for m in kept)} still capturable)')
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
