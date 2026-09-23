#!/usr/bin/env python3
"""Who was in the lobby: every player's rank, so a game can be judged against
the opponents it was played against.

  python tools/ranks.py                  # every fetched match without ranks.json
  python tools/ranks.py NA1_5647258830   # specific match(es)
  python tools/ranks.py --refresh        # re-fetch even where ranks.json exists

Writes data/matches/<id>/ranks.json. fetch_match.py calls this for new games.

The Riot API only gives a player's rank *now*, not at the time of the game, so
each snapshot records when it was taken. Fetch soon after playing and it is
close enough. Normal draft has no visible rating, so a player's solo queue rank
stands in for their strength, then flex; many players are simply unranked.
"""

from __future__ import annotations

import argparse
import json
from datetime import datetime, timezone
from pathlib import Path

from riot import DATA, Riot, RiotError

TIERS = ["IRON", "BRONZE", "SILVER", "GOLD", "PLATINUM", "EMERALD", "DIAMOND"]
APEX = {"MASTER": 28, "GRANDMASTER": 29, "CHALLENGER": 30}
DIVISIONS = {"IV": 0, "III": 1, "II": 2, "I": 3}
QUEUE_PREFERENCE = ("RANKED_SOLO_5x5", "RANKED_FLEX_SR")


def score(tier: str, division: str) -> int:
    """Iron IV = 0, one point per division, four per tier; Master and up 28-30."""
    if tier in APEX:
        return APEX[tier]
    return TIERS.index(tier) * 4 + DIVISIONS.get(division, 0)


def label(s: float | None) -> str:
    """A score (possibly an average) back to a readable rank, e.g. 'Silver II'."""
    if s is None:
        return "unranked"
    s = round(s)
    for name, v in sorted(APEX.items(), key=lambda kv: -kv[1]):
        if s >= v:
            return name.title()
    tier = TIERS[min(s // 4, len(TIERS) - 1)]
    div = {v: k for k, v in DIVISIONS.items()}[s % 4]
    return f"{tier.title()} {div}"


def best_entry(entries: list[dict]) -> dict | None:
    by_queue = {e.get("queueType"): e for e in entries}
    for q in QUEUE_PREFERENCE:
        if q in by_queue:
            return by_queue[q]
    return None


def lobby(api: Riot, match_dir: Path, cache: dict[str, list[dict]]) -> dict:
    """Rank snapshot for all ten players. `cache` is per run: friends repeat."""
    info = json.loads((match_dir / "match.json").read_text())["info"]
    players = []
    for p in info["participants"]:
        puuid = p["puuid"]
        if puuid not in cache:
            cache[puuid] = api.league_entries(puuid)
        e = best_entry(cache[puuid])
        players.append({
            # No PUUIDs or Riot IDs: the match file already maps participantId
            # to a person, and this file only needs to say how strong they are.
            "participant_id": p["participantId"],
            "team_id": p["teamId"],
            "position": p.get("teamPosition") or "?",
            "champion": p["championName"],
            "summoner_level": p.get("summonerLevel"),
            "queue": e["queueType"] if e else None,
            "tier": e["tier"] if e else None,
            "division": e.get("rank") if e else None,
            "lp": e.get("leaguePoints") if e else None,
            "games": (e.get("wins", 0) + e.get("losses", 0)) if e else 0,
            "score": score(e["tier"], e.get("rank", "IV")) if e else None,
            "rank": label(score(e["tier"], e.get("rank", "IV"))) if e else "unranked",
        })
    return {
        "match_id": match_dir.name,
        "fetched_utc": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "note": "ranks as of fetched_utc, not as of the game",
        "players": players,
    }


def save(api: Riot, match_dir: Path, cache: dict[str, list[dict]]) -> dict:
    snap = lobby(api, match_dir, cache)
    (match_dir / "ranks.json").write_text(json.dumps(snap, indent=2))
    return snap


def summarize(snap: dict, my_pid: int) -> dict:
    """The lobby in the terms a review needs: how strong were the enemies,
    relative to the player, and who was the strongest of them."""
    players = snap["players"]
    me = next(p for p in players if p["participant_id"] == my_pid)
    allies = [p for p in players if p["team_id"] == me["team_id"] and p is not me]
    enemies = [p for p in players if p["team_id"] != me["team_id"]]

    def avg(group: list[dict]) -> float | None:
        vals = [p["score"] for p in group if p["score"] is not None]
        return round(sum(vals) / len(vals), 1) if vals else None

    enemy_avg = avg(enemies)
    ally_avg = avg(allies)
    my_score = me["score"]
    # Relative to the player where possible. An unranked player is treated as
    # Iron IV, which matches the profile and keeps the label usable.
    base = my_score if my_score is not None else 0
    gap = None if enemy_avg is None else round(enemy_avg - base, 1)
    # Tiers, not divisions: an Iron player's normal-draft lobbies average
    # Bronze routinely, so "harder" starts a full two tiers up.
    if gap is None:
        verdict = "unknown"
    elif gap >= 12:
        verdict = "much harder"   # enemies average three tiers up (Gold+ for Iron)
    elif gap >= 8:
        verdict = "harder"        # two tiers up (Silver for Iron)
    else:
        verdict = "near your level"

    ranked_enemies = [p for p in enemies if p["score"] is not None]
    strongest = max(ranked_enemies, key=lambda p: p["score"], default=None)
    ej = next((p for p in enemies if p["position"] == "JUNGLE"), None)

    def brief(p: dict | None) -> dict | None:
        if p is None:
            return None
        return {"champion": p["champion"], "position": p["position"], "rank": p["rank"],
                "summoner_level": p["summoner_level"]}

    return {
        "verdict": verdict,
        "my_rank": me["rank"],
        "enemy_avg_rank": label(enemy_avg),
        "ally_avg_rank": label(ally_avg),
        "enemy_avg_score": enemy_avg,
        "ally_avg_score": ally_avg,
        "gap_vs_me": gap,
        "enemies_ranked": len(ranked_enemies),
        # An average of one or two ranked players says little about the lobby.
        "confident": len(ranked_enemies) >= 3,
        "enemies_two_tiers_up": sum(1 for p in ranked_enemies if p["score"] - base >= 8),
        "enemies_gold_or_above": sum(1 for p in ranked_enemies if p["score"] >= 12),
        "enemy_jungler": brief(ej),
        "strongest_enemy": brief(strongest),
        # Unranked on a young account is how a smurf looks from the API; it is
        # a hint for the reviewer, not a verdict.
        "new_accounts_on_enemy_team": sum(1 for p in enemies
                                          if (p["summoner_level"] or 999) < 50),
        "ranks_as_of": snap["fetched_utc"][:10],
    }


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("match_ids", nargs="*")
    ap.add_argument("--refresh", action="store_true", help="re-fetch existing snapshots")
    args = ap.parse_args()

    root = DATA / "matches"
    dirs = [root / m for m in args.match_ids] if args.match_ids else \
        sorted(d for d in root.glob("NA1_*") if (d / "match.json").exists())
    todo = [d for d in dirs if args.refresh or not (d / "ranks.json").exists()]
    if not todo:
        print("every match already has ranks.json (use --refresh to re-fetch)")
        return 0

    try:
        api = Riot(json.loads((DATA / "player.json").read_text()).get("platform", "NA1"))
    except (RiotError, FileNotFoundError) as e:
        print(f"error: {e}")
        return 1

    cache: dict[str, list[dict]] = {}
    for d in todo:
        try:
            snap = save(api, d, cache)
        except RiotError as e:
            print(f"  {d.name}: {e}")
            if "401" in str(e):
                return 1
            continue
        meta = json.loads((d / "meta.json").read_text())
        s = summarize(snap, meta["participant_id"])
        print(f"  {d.name}  enemies avg {s['enemy_avg_rank']:<12} "
              f"({s['enemies_ranked']}/5 ranked)  you {s['my_rank']:<10} -> {s['verdict']}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
