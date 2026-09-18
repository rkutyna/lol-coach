"""Turn captured PNG frame sequences into clips plus review frames.

Raw PNGs are huge — about 2.8 MB each at 1280x720, so a 12-second capture at
6 fps is ~135 MB. The same clip as h264 is ~1.4 MB, so frames are converted and
deleted immediately. Nothing raw is kept.
"""

from __future__ import annotations

import shutil
import subprocess
from pathlib import Path

# What Claude reads: a few JPEGs per moment, wide enough for the minimap to be
# legible but not so large that a batch blows the token budget.
REVIEW_FPS = 2
REVIEW_WIDTH = 1100
REVIEW_QUALITY = 6      # ffmpeg -q:v, lower is better quality


def _run(args: list[str]) -> None:
    r = subprocess.run(args, capture_output=True, text=True)
    if r.returncode != 0:
        raise RuntimeError(f'{args[0]} failed: {r.stderr.strip()[:300]}')


def frame_dir(capture_root: Path) -> Path | None:
    """Find the dir the client created, e.g. `16-18_NA1-5643527353_01/`."""
    pngs = sorted(capture_root.rglob("*.png"))
    return pngs[0].parent if pngs else None


def to_clip(frames: Path, out_mp4: Path, fps: int) -> dict:
    """Encode a PNG sequence to a silent mp4. Returns basic facts about it."""
    out_mp4.parent.mkdir(parents=True, exist_ok=True)
    count = len(list(frames.glob("*.png")))
    if not count:
        raise RuntimeError(f"no frames in {frames}")

    _run(["ffmpeg", "-hide_banner", "-loglevel", "error", "-y",
          "-framerate", str(fps),
          "-pattern_type", "glob", "-i", str(frames / "*.png"),
          "-c:v", "libx264", "-crf", "26", "-pix_fmt", "yuv420p",
          "-movflags", "+faststart", str(out_mp4)])
    return {"frames": count, "bytes": out_mp4.stat().st_size, "fps": fps}


def to_review_frames(mp4: Path, out_dir: Path, fps: int = REVIEW_FPS) -> list[Path]:
    """Sample a clip into JPEGs for Claude to actually look at."""
    out_dir.mkdir(parents=True, exist_ok=True)
    for old in out_dir.glob("*.jpg"):
        old.unlink()
    _run(["ffmpeg", "-hide_banner", "-loglevel", "error", "-y", "-i", str(mp4),
          "-vf", f"fps={fps},scale={REVIEW_WIDTH}:-2",
          "-q:v", str(REVIEW_QUALITY), str(out_dir / "f%02d.jpg")])
    return sorted(out_dir.glob("*.jpg"))


def process(capture_root: Path, out_mp4: Path, review_dir: Path,
            fps: int, keep_png: bool = False) -> dict:
    """PNG sequence -> mp4 + review JPEGs, deleting the PNGs unless asked."""
    frames = frame_dir(capture_root)
    if frames is None:
        raise RuntimeError(f"capture produced no frames under {capture_root}")

    info = to_clip(frames, out_mp4, fps)
    # Never sample faster than the capture: a 1fps clip resampled at 2fps just
    # duplicates every frame, doubling what Claude has to read for no new detail.
    jpgs = to_review_frames(out_mp4, review_dir, fps=min(REVIEW_FPS, fps))
    info["review_frames"] = len(jpgs)

    if not keep_png:
        shutil.rmtree(capture_root, ignore_errors=True)
        info["png_deleted"] = True
    return info
