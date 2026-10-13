"""Helpers shared by pipeline.py, probe_torrents.py and screenshots.py, so
tracker lists, file detection and frame/state bookkeeping cannot drift apart
between scripts.
"""

from __future__ import annotations

import json
from pathlib import Path

# Fresh public trackers: magnets scraped from 1337x carry stale tracker lists
# (dead coppersurfer/rarbg/leechers-paradise/openbittorrent hosts), so every
# aria2c run supplies its own announce set.
FRESH_TRACKERS = [
    "udp://tracker.opentrackr.org:1337/announce",
    "udp://open.demonii.com:1337/announce",
    "udp://open.stealth.si:80/announce",
    "udp://tracker.torrent.eu.org:451/announce",
    "udp://exodus.desync.com:6969/announce",
    "udp://zer0day.ch:1337/announce",
    "udp://tracker.therarbg.to:6969/announce",
    "udp://tracker.publictracker.xyz:6969/announce",
    "udp://tracker.qu.ax:6969/announce",
    "udp://tracker.peerfect.org:6969/announce",
    "udp://tracker.opentrackr.com:6969/announce",
]
BT_TRACKER_FLAG = "--bt-tracker=" + ",".join(FRESH_TRACKERS)

MOVIE_EXTENSIONS = {
    "mp4", "mkv", "avi", "mov", "webm", "m4v", "mpg", "mpeg", "wmv", "flv",
}


def is_movie(path: Path) -> bool:
    return path.suffix.lstrip(".").lower() in MOVIE_EXTENSIONS


def find_video(directory: Path) -> Path | None:
    """Return the largest video file under directory (the feature, not a
    sample or extra)."""
    candidates = [path for path in directory.rglob("*") if path.is_file() and is_movie(path)]
    return max(candidates, key=lambda path: path.stat().st_size, default=None)


def frame_name(frame: int) -> str:
    return f"frame_{frame:06d}.jpg"


def saved_frames(shot_dir: Path) -> int:
    """Count the contiguous run of frame_%06d.jpg files (resumable naming)."""
    count = 0
    while (shot_dir / frame_name(count + 1)).is_file():
        count += 1
    return count


def frames_complete(found: int, expected: int, fps: float) -> bool:
    """True when an extraction produced (nearly) every expected frame.

    `expected` is ceil(container duration * fps), but ffmpeg's fps filter can
    emit one frame fewer, and the container duration often includes a little
    trailing audio past the last video frame. Allow 5 seconds of slack; a
    genuinely incomplete extraction falls far shorter than that.
    """
    return found >= expected - int(5 * fps) - 1


def seek_arg(seconds: float) -> str:
    """Format an ffmpeg -ss value. Fixed-point, never `:.3` (3 significant
    digits), which turned 1234.5 into '1.23e+03' and 345.5 into '346'."""
    return f"{seconds:.3f}"


def load_ranks(state: Path, status: str) -> set[int]:
    """Ranks with at least one state record of the given status."""
    ranks: set[int] = set()
    if state.exists():
        for line in state.read_text(encoding="utf-8").splitlines():
            try:
                record = json.loads(line)
            except json.JSONDecodeError:
                continue
            if record.get("status") == status:
                ranks.add(record["rank"])
    return ranks


def append_state(state: Path, record: dict) -> None:
    with state.open("a", encoding="utf-8") as handle:
        handle.write(json.dumps(record, ensure_ascii=False) + "\n")
