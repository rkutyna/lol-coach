#!/usr/bin/env python3
"""Act on data/review_queue.json — the games ticked in the picker page.

  python tools/run_queue.py               # run it
  python tools/run_queue.py --dry-run      # print the plan, change nothing
  python tools/run_queue.py --stats-only   # run the stats side, skip video reporting

For every game queued with "review": true, this runs whatever stats step is
still missing for it — find_moments.py, then digest.py — by calling those
tools directly rather than reimplementing them. digest is scoped to the
queued games, because the queue is the definition of a batch: batch.json
holds the averages a goal is promoted or held against, so it must cover the
games being reviewed together and nothing else. For every game queued with
"video": true, it refreshes the capture plan and reports what capture would
do (clip counts, estimated freeze time), then prints the exact
`python tools/capture.py <ids> --launch` command.

It NEVER invokes tools/capture.py itself. Capture takes over the League
client for the length of the run and leaves it unusable afterwards, so
starting it has to stay a deliberate action a human takes on purpose — this
tool only ever tells you the command to run.

Finishes by running build_review.py so the picker page reflects the new
state (has_review/has_stats/moments/etc. for the games just processed).
--dry-run prints the same plan but runs nothing, including build_review.py.
"""

from __future__ import annotations

import argparse
import json
import subprocess
import sys
from pathlib import Path

from riot import DATA, ROOT

TOOLS = ROOT / "tools"


def load_queue() -> dict:
    """match_id -> {"review": bool, "video": bool}. Never raises."""
    f = DATA / "review_queue.json"
    if not f.exists():
        return {}
    try:
        games = json.loads(f.read_text()).get("games", {})
        return games if isinstance(games, dict) else {}
    except Exception as e:
        print(f"warning: ignoring {f} — {e}")
        return {}


def existing_match_ids() -> set[str]:
    matches = DATA / "matches"
    if not matches.exists():
        return set()
    return {d.name for d in matches.iterdir() if d.is_dir()}


def load_capture_plan() -> dict:
    f = DATA / "capture_plan.json"
    if not f.exists():
        return {}
    try:
        return json.loads(f.read_text())
    except Exception as e:
        print(f"warning: ignoring {f} — {e}")
        return {}


def run_tool(name: str, args: list[str]) -> int:
    """Run one of our own CLI tools as a subprocess — never capture.py."""
    assert name != "capture.py"
    cmd = [sys.executable, str(TOOLS / name), *args]
    print("$ " + " ".join(cmd))
    return subprocess.run(cmd, cwd=ROOT).returncode


def video_report(match_id: str, plan_doc: dict) -> str:
    plans = {p["match_id"]: p for p in plan_doc.get("plans", [])}
    p = plans.get(match_id)
    if p is None:
        return f"  {match_id}: no capture plan for it yet (needs review/moments first)"
    jobs = p["jobs"]
    freeze = sum(j.get("est_freeze_s", 0) for j in jobs)
    flag = "" if p.get("replay_capturable", True) else "  [REPLAY EXPIRED]"
    return f"  {match_id}: {len(jobs)} clip(s) planned, ~{freeze}s of client freeze{flag}"


def main() -> int:
    ap = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--dry-run", action="store_true", help="print the plan, change nothing")
    ap.add_argument("--stats-only", action="store_true",
                     help="run the stats pipeline only, skip anything involving video")
    args = ap.parse_args()

    # Otherwise our own prints buffer while a subprocess's don't, and the two
    # interleave out of order whenever stdout isn't a tty.
    sys.stdout.reconfigure(line_buffering=True)

    queue = load_queue()
    valid_ids = existing_match_ids()

    unknown = sorted(set(queue) - valid_ids)
    for mid in unknown:
        print(f"warning: {mid} is queued but not under data/matches/ — skipping it")

    review_ids = sorted(mid for mid, sel in queue.items()
                        if mid in valid_ids and sel.get("review"))
    video_ids = sorted(mid for mid, sel in queue.items()
                       if mid in valid_ids and sel.get("video"))

    need_moments = [mid for mid in review_ids
                    if not (DATA / "matches" / mid / "moments.json").exists()]
    need_stats = [mid for mid in review_ids
                  if not (DATA / "matches" / mid / "stats.json").exists()]

    print(f"{len(review_ids)} game(s) queued for review, {len(video_ids)} for video")

    # --- stats side --------------------------------------------------------
    if not review_ids:
        print("nothing queued for review")
    else:
        if need_moments:
            print(f"find_moments needed for: {', '.join(need_moments)}")
        else:
            print("find_moments: nothing missing")
        print(f"digest scoped to the {len(review_ids)} queued game(s) — the queue "
              "defines the batch, so batch.json covers exactly these")

        if not args.dry_run:
            if need_moments:
                run_tool("find_moments.py", need_moments)
            # digest always reruns over the queued set, even when every
            # stats.json already exists: batch.json is the batch average the
            # review is judged against, and the queue is what the batch *is*.
            run_tool("digest.py", sorted(review_ids))

    # --- video side ----------------------------------------------------------
    if args.stats_only:
        print("--stats-only: skipping video reporting")
    elif not video_ids:
        print("nothing queued for video")
    else:
        if not args.dry_run:
            run_tool("capture_plan.py", [])
        plan_doc = load_capture_plan()
        if args.dry_run and not plan_doc:
            print("(no data/capture_plan.json yet — plan below is empty; "
                  "run without --dry-run to refresh it first)")
        print(f"video queued for: {', '.join(video_ids)}")
        for mid in video_ids:
            print(video_report(mid, plan_doc))
        cmd = f"python tools/capture.py {' '.join(video_ids)} --launch"
        print(f"\nrun this yourself when ready (never run automatically):\n  {cmd}")

    # --- finish --------------------------------------------------------------
    if args.dry_run:
        print("\n--dry-run: nothing was changed")
        return 0

    run_tool("build_review.py", [])

    print("\nsummary")
    print(f"  reviewed: {', '.join(review_ids) or '(none)'}")
    print(f"  queued for video: {', '.join(video_ids) or '(none)'}")
    if video_ids and not args.stats_only:
        print(f"  capture command: python tools/capture.py {' '.join(video_ids)} --launch")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
